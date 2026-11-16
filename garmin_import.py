"""Import running history by recorded local date from the Garmin Personal API."""
import json
import os
import re
import sqlite3

from fit_data import FitError
from fit_import import index_originals, unpack_original
from garmin_activity import RUN_TYPES, activity_fields, local_timestamp
import garmin_coverage
import runs
from storage import ArchiveChecksumError, ArchivePermissionsError, storage_failure


PAGE_SIZE = 20


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


def cached_source(archive, digest):
    try:
        return archive.read(digest)
    except (ArchiveChecksumError, PermissionError):
        return None


def original_sources(client, store, archive, garmin_id, scan_id):
    sources = []
    for digest, file_format in store.originals(garmin_id):
        try:
            cached = archive.read(digest)
        except OSError:
            # A damaged or inaccessible optional object must not block valid metadata.
            cached = None
        if cached is None:
            sources.append(("original_unavailable", digest, file_format))
        else:
            try:
                file_format, fit = unpack_original(archive, cached)
                return [*sources, ("original", digest, file_format)], None, fit
            except FitError:
                sources.append(("original_error", digest, file_format))
            except OSError:
                sources.append(("original_unavailable", digest, file_format))
    store.write_report("UPDATE garmin_scan_items SET original_requested=1 WHERE scan_id=? AND garmin_id=?",
                       (scan_id, garmin_id))
    try:
        data = client.get(f"/download-service/files/activity/{garmin_id}", "Garmin original download", raw=True)
    except runs.ConnectionProblem as err:
        return sources, f"Original unavailable. {err} Retry these dates to request it again.", None
    if not data:
        return sources, "Original unavailable. Garmin returned an empty file. Retry these dates.", None
    try:
        digest = archive.put(data)
        file_format, fit = unpack_original(archive, data)
    except FitError as err:
        return [*sources, ("original_error", digest, "unknown")], f"Original unavailable. {err} Retry these dates.", None
    except OSError:
        return sources, "Original unavailable. Check the private archive permissions and retry these dates.", None
    sources = [source for source in sources if source[1] != digest]
    return [*sources, ("original", digest, file_format)], None, fit


def same_json(left, right):
    pairs = [(left, right)]
    while pairs:
        left, right = pairs.pop()
        if isinstance(left, dict):
            if not isinstance(right, dict) or left.keys() != right.keys():
                return False
            pairs.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                return False
            pairs.extend(zip(left, right))
        elif isinstance(left, bool) != isinstance(right, bool) or left != right:
            return False
    return True


def import_one(client, store, archive, scan, garmin_id, listed, page_digest, versions, reusable, pending):
    scan_id = scan["id"]
    data = cached_source(archive, versions[0]) if reusable and scan["mode"] != "range" else None
    if data is None:
        data = client.get(f"/activity-service/activity/{garmin_id}", "Garmin activity detail", raw=True)
    detail_digest = archive.put(data)
    detail = decode(data, "detail", dict)
    fields = activity_fields(garmin_id, listed, detail)
    day = fields["local_date"]
    store.write_report("UPDATE garmin_scan_items SET local_date=? WHERE scan_id=? AND garmin_id=?",
                       (day, scan_id, garmin_id))
    status = "saved"
    if fields["sport"] and fields["sport"] not in RUN_TYPES:
        status = "excluded_sport"
    elif day and not scan["first"] <= day <= scan["last"]:
        status = "outside_range"
        if pending:
            needs_retry = (pending["first"] <= day <= pending["last"] or pending["status"] == "retry_elsewhere"
                           or pending["status"] == "saved" and not pending["original_available"])
            status = "retry_elsewhere" if needs_retry else "resolved_outside"
    elif not fields["sport"]:
        raise runs.ConnectionProblem("Activity type is missing. Check the activity in Garmin and retry.")
    elif not day:
        raise runs.ConnectionProblem("Recorded local start is missing or invalid. Check the activity in Garmin and retry.")

    sources = [("detail", detail_digest, "json")]
    if page_digest:
        sources.insert(0, ("list", page_digest, "json"))
    warning = fit = None
    if status == "saved":
        originals, warning, fit = original_sources(client, store, archive, garmin_id, scan_id)
        sources.extend(originals)
    # Keep the wire bytes even when only their JSON serialization changed.
    detail_changed = detail_digest not in versions and not any(
        same_json(decode(raw, "archived detail", dict), detail)
        for version in versions if (raw := cached_source(archive, version)) is not None)
    store.save(scan["import_id"], fields, sources, warning, fit, scan_id=scan_id, detail_sha=detail_digest,
               detail_changed=detail_changed, status=status)


def process_item(client, store, archive, scan, garmin_id, listed, page_digest, pending, from_search=True):
    # If recording the discovery itself fails, the range cannot claim enumeration.
    local = local_timestamp(listed.get("startTimeLocal"))
    versions, reusable = store.observe(scan["id"], garmin_id, local[:10] if local else None, page_digest,
                                       garmin_coverage.record_hash(listed), from_search)
    try:
        import_one(client, store, archive, scan, garmin_id, listed, page_digest, versions, reusable, pending)
    except runs.ConnectionProblem as err:
        store.issue(scan["import_id"], garmin_id, "Activity detail", str(err))
    except (UnicodeError, OverflowError):
        store.issue(scan["import_id"], garmin_id, "Activity detail", "Invalid activity fields. Check Garmin and retry.")
    except (OSError, sqlite3.Error) as exc:
        store.issue(scan["import_id"], garmin_id, "Save activity", storage_failure("Save activity", exc))


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
    operation = "Index archived originals"
    try:
        index_originals(store, archive)
        operation = "Read date ranges"
        scans = store.scans(import_id)
        label = f'{scans[0]["first"]} to {scans[0]["last"]}'
        client = open_garmin()
        for scan in scans:
            label = f'{scan["first"]} to {scan["last"]}'
            operation = "Scan date range"
            scan_range(client, store, archive, scan)
        operation = "Read import report"
        report = store.report(str(import_id))
        if all(s["coverage"] in ("checked", "checked_empty") for s in report["scans"]):
            state = "complete"
        elif any(s["pages_read"] for s in report["scans"]):
            state = "partial"
    except runs.ConnectionProblem as err:
        store.issue(import_id, label, "Activity search", str(err))
    except ArchivePermissionsError as err:
        store.issue(import_id, label, "Private archive", str(err))
    except (OSError, sqlite3.Error) as exc:
        store.issue(import_id, label, "Import storage", storage_failure(operation, exc))
    except Exception:
        store.issue(import_id, label, "Import", "Unexpected import error. Check the saved activities and retry these dates.")
    finally:
        store.finish(import_id, state)
