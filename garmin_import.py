"""Import running history by recorded local date from the Garmin Personal API."""
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import os
import re
import sqlite3
from zipfile import ZipFile

import garmin_coverage
import runs
from storage import ArchiveChecksumError, ArchivePermissionsError


PAGE_SIZE = 20
RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "virtual_run", "virtual_running",
             "indoor_track_running", "indoor_running", "ultra_run", "obstacle_run", "street_running"}


def open_garmin():
    if os.environ.get("RUNS_GARMIN_OFFLINE") == "1":
        raise runs.ConnectionProblem("Garmin is offline. Restart with Garmin enabled and retry these dates.")
    try:
        settings = runs.load_settings()
    except (OSError, ValueError):
        raise runs.ConnectionProblem("Garmin credential setup failed. Check your settings and retry.") from None
    return runs.GarminClient(settings)


def decode(data, kind, expected):
    try:
        parsed = json.loads(data)
        if not isinstance(parsed, expected):
            raise ValueError
        return parsed
    except (ValueError, RecursionError):
        raise runs.ConnectionProblem(f"Unexpected Garmin {kind} JSON. Check Garmin availability and retry.") from None


def timestamp(value):
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_timestamp(value):
    if timestamp(value) is not None:
        return value[:10] + " " + value[11:]
    return None


def number(value):
    if type(value) in (int, float) and value >= 0:
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return None


def sport(record, key):
    value = record.get(key)
    key = value.get("typeKey") if isinstance(value, dict) else None
    return key.strip() or None if isinstance(key, str) else None


def activity_fields(garmin_id, listed, detail):
    if str(detail.get("activityId")) != garmin_id:
        raise runs.ConnectionProblem("Detail did not identify the requested activity. Recheck these dates.")
    summary = detail.get("summaryDTO")
    records = [("detail", summary if isinstance(summary, dict) else {}), ("detail", detail), ("list", listed)]
    origins = {}

    def choose(field, upstream_key, parse):
        for source, record in records:
            value = parse(record.get(upstream_key))
            if value is not None:
                origins[field] = source
                return value
        return None

    local = choose("start_local", "startTimeLocal", local_timestamp)
    if local is None:
        raise runs.ConnectionProblem("Recorded local start is missing or invalid. Check the activity in Garmin and retry.")
    utc = choose("start_utc", "startTimeGMT", timestamp)
    if utc and utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    name = detail.get("activityName") or listed.get("activityName")
    activity_sport = sport(detail, "activityTypeDTO") or sport(listed, "activityType")
    if activity_sport is None:
        raise runs.ConnectionProblem("Activity type is missing. Check the activity in Garmin and retry.")
    fields = {
        "garmin_id": garmin_id, "name": name if isinstance(name, str) else None,
        "sport": activity_sport,
        "start_local": local, "local_date": local[:10],
        "start_utc": utc.astimezone(timezone.utc).isoformat() if utc else None,
    }
    for field, key in (("distance_m", "distance"), ("timer_seconds", "duration"),
                       ("elapsed_seconds", "elapsedDuration"), ("moving_seconds", "movingDuration")):
        fields[field] = choose(field, key, number)
    fields["metric_sources"] = json.dumps(origins, sort_keys=True)
    return fields


def fit_envelope(header, length):
    size = int.from_bytes(header[4:8], "little")
    return (len(header) >= 12 and header[0] >= 12 and header[8:12] == b".FIT"
            and size > 0 and length >= header[0] + size + 2)


def original_format(data):
    # Check the container without decoding the FIT session or extracting files to disk.
    if fit_envelope(data[:12], len(data)):
        return "fit"
    try:
        with ZipFile(BytesIO(data)) as zipped:
            members = [member for member in zipped.infolist() if not member.is_dir()]
            if not any(member.file_size for member in members):
                return None
            for member in members:
                if member.filename.lower().endswith(".fit"):
                    with zipped.open(member) as file:
                        if not fit_envelope(file.read(12), member.file_size):
                            return None
            if zipped.testzip() is None:
                return "zip"
    except Exception:
        # ZIP metadata and the different compression codecs have no common
        # decode exception. Keep any unreadable original as a warning.
        return None
    return None


def cached_source(archive, digest):
    try:
        return archive.read(digest)
    except ArchiveChecksumError:
        return None


def original_sources(client, store, archive, garmin_id, scan_id):
    sources = []
    for digest, file_format in store.originals(garmin_id):
        cached = cached_source(archive, digest)
        if cached is not None:
            if original_format(cached) == file_format:
                return [*sources, ("original", digest, file_format)], None
            sources.append(("original_error", digest, file_format))
    store.write_report("UPDATE garmin_scan_items SET original_requested=1 WHERE scan_id=? AND garmin_id=?",
                       (scan_id, garmin_id))
    try:
        data = client.get(f"/download-service/files/activity/{garmin_id}", "Garmin original download", raw=True)
    except runs.ConnectionProblem as err:
        return sources, f"Original unavailable. {err} Retry these dates to request it again."
    if not data:
        return sources, "Original unavailable. Garmin returned an empty file. Retry these dates."
    digest = archive.put(data)
    file_format = original_format(data)
    if file_format:
        return [*sources, ("original", digest, file_format)], None
    return [*sources, ("original_error", digest, "unknown")], "Original unavailable. Check the file in Garmin and retry these dates."


def same_json(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_json(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


def import_one(client, store, archive, scan, garmin_id, listed, page_digest, previous_sha, reusable, pending):
    scan_id = scan["id"]
    listed_sport = sport(listed, "activityType")
    if scan["mode"] != "range" and not previous_sha and not pending and listed_sport and listed_sport not in RUN_TYPES:
        store.write_report("UPDATE garmin_scan_items SET status='excluded_sport' WHERE scan_id=? AND garmin_id=?",
                           (scan_id, garmin_id))
        return

    prior_data = cached_source(archive, previous_sha) if previous_sha else None
    data = prior_data if reusable and scan["mode"] != "range" else None
    if data is None:
        data = client.get(f"/activity-service/activity/{garmin_id}", "Garmin activity detail", raw=True)
    detail_digest = archive.put(data)
    detail = decode(data, "detail", dict)
    fields = activity_fields(garmin_id, listed, detail)
    day = fields["local_date"]
    store.write_report("UPDATE garmin_scan_items SET local_date=? WHERE scan_id=? AND garmin_id=?",
                       (day, scan_id, garmin_id))
    status = "saved"
    if fields["sport"] not in RUN_TYPES:
        status = "excluded_sport"
    elif not scan["first"] <= day <= scan["last"]:
        status = "outside_range"
        if pending:
            needs_retry = (pending["first"] <= day <= pending["last"] or pending["status"] == "retry_elsewhere"
                           or pending["status"] == "saved" and not pending["original_available"])
            status = "retry_elsewhere" if needs_retry else "resolved_outside"

    sources = [("detail", detail_digest, "json")]
    if page_digest:
        sources.insert(0, ("list", page_digest, "json"))
    warning = None
    if status == "saved":
        originals, warning = original_sources(client, store, archive, garmin_id, scan_id)
        sources.extend(originals)
    # Keep the wire bytes even when only their JSON serialization changed.
    detail_changed = (detail_digest != previous_sha
                      and (prior_data is None or not same_json(decode(prior_data, "archived detail", dict), detail)))
    store.save(scan["import_id"], fields, sources, warning, scan_id=scan_id, detail_sha=detail_digest,
               detail_changed=detail_changed, status=status)


def process_item(client, store, archive, scan, garmin_id, listed, page_digest, pending, from_search=True):
    # If recording the discovery itself fails, the range cannot claim enumeration.
    local = local_timestamp(listed.get("startTimeLocal"))
    previous_sha, reusable = store.observe(scan["id"], garmin_id, local[:10] if local else None, page_digest,
                                           garmin_coverage.record_hash(listed), from_search)
    try:
        import_one(client, store, archive, scan, garmin_id, listed, page_digest, previous_sha, reusable, pending)
    except runs.ConnectionProblem as err:
        store.issue(scan["import_id"], garmin_id, "Activity detail", str(err))
    except (UnicodeError, OverflowError):
        store.issue(scan["import_id"], garmin_id, "Activity detail", "Invalid activity fields. Check Garmin and retry.")
    except (OSError, sqlite3.Error):
        store.issue(scan["import_id"], garmin_id, "Save activity", "Check the private archive and SQLite storage, then retry.")


def valid_id(item):
    value = str(item.get("activityId")) if isinstance(item, dict) else ""
    return value if re.fullmatch(r"[1-9][0-9]*", value) else None


def scan_range(client, store, archive, scan):
    import_id, scan_id = scan["import_id"], scan["id"]
    first, last = scan["first"], scan["last"]
    label = f"{first} to {last}"
    start, end = garmin_coverage.search_bounds(first, last)
    pending = {i["garmin_id"]: i for i in store.retry_items(first, last)}
    seen, invalid = set(), set()
    offset = 0
    while True:
        # Search every sport so subtypes omitted by Garmin's parent filter are still seen.
        try:
            data = client.search({"startDate": start, "endDate": end, "start": offset, "limit": PAGE_SIZE}, raw=True)
            page_digest = archive.put(data)
            page = decode(data, "activity list", list)
        except runs.ConnectionProblem as err:
            store.issue(import_id, label, "Activity search", f"Offset {offset}. {err}")
            return
        store.write_report("UPDATE garmin_scans SET pages=pages+1 WHERE id=?", (scan_id,))
        if not page:
            break
        progress = False
        for listed in page:
            garmin_id = valid_id(listed)
            if garmin_id is None:
                fingerprint = garmin_coverage.record_hash(listed)
                if fingerprint not in invalid:
                    invalid.add(fingerprint)
                    progress = True
                    store.write_report("UPDATE garmin_scans SET unidentified=unidentified+1 WHERE id=?", (scan_id,))
                    store.issue(import_id, label, "Activity list", f"Missing or invalid Garmin ID at offset {offset}. Recheck these dates.")
            elif garmin_id not in seen:
                seen.add(garmin_id)
                progress = True
                process_item(client, store, archive, scan, garmin_id, listed, page_digest, pending.get(garmin_id))
        if not progress:
            store.issue(import_id, label, "Activity search", f"Offset {offset} repeated previously returned records. Recheck these dates.")
            return
        offset += len(page)

    # A failed ID may disappear from search. Use its saved list evidence to retry it directly.
    for garmin_id, item in pending.items():
        if garmin_id in seen:
            continue
        listed = {"activityId": garmin_id}
        page_digest = item["list_sha"]
        if page_digest:
            raw = cached_source(archive, page_digest)
            if raw is not None:
                listed = next((row for row in decode(raw, "archived list", list) if valid_id(row) == garmin_id), listed)
            else:
                page_digest = None
        process_item(client, store, archive, scan, garmin_id, listed, page_digest, item, from_search=False)
    store.write_report("UPDATE garmin_scans SET enumerated=1 WHERE id=?", (scan_id,))


def run_import(store, archive, import_id):
    state = "failed"
    label = f"Import {import_id}"
    try:
        scans = store.scans(import_id)
        label = f'{scans[0]["first"]} to {scans[0]["last"]}'
        client = open_garmin()
        for scan in scans:
            label = f'{scan["first"]} to {scan["last"]}'
            scan_range(client, store, archive, scan)
        report = store.report(str(import_id))
        if all(s["coverage"] in ("checked", "checked_empty") for s in report["scans"]):
            state = "complete"
        elif any(s["pages_read"] for s in report["scans"]):
            state = "partial"
    except runs.ConnectionProblem as err:
        store.issue(import_id, label, "Activity search", str(err))
    except ArchivePermissionsError as err:
        store.issue(import_id, label, "Private archive", str(err))
    except (OSError, sqlite3.Error):
        store.issue(import_id, label, "Import storage", "Check the private archive and SQLite storage, then retry.")
    except Exception:
        store.issue(import_id, label, "Import", "Unexpected import error. Check the saved activities and retry these dates.")
    finally:
        store.finish(import_id, state)
