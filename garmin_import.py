"""Import an explicitly selected date range from the Garmin Personal API."""
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import os
import re
import sqlite3
from zipfile import is_zipfile

import runs


PAGE_SIZE = 20
RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "virtual_run", "virtual_running",
             "indoor_running", "ultra_run", "obstacle_run", "street_running"}


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
    return value.get("typeKey") if isinstance(value, dict) and isinstance(value.get("typeKey"), str) else None


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

    local = choose("start_local", "startTimeLocal", timestamp)
    if local is None:
        raise runs.ConnectionProblem("Recorded local start is missing or invalid. Check the activity in Garmin and retry.")
    utc = choose("start_utc", "startTimeGMT", timestamp)
    if utc and utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    name = detail.get("activityName") or listed.get("activityName")
    fields = {
        "garmin_id": garmin_id, "name": name if isinstance(name, str) else None,
        "sport": sport(detail, "activityTypeDTO") or sport(listed, "activityType") or "running",
        "start_local": local.isoformat(sep=" "), "local_date": local.date().isoformat(),
        "start_utc": utc.astimezone(timezone.utc).isoformat() if utc else None,
    }
    for field, key in (("distance_m", "distance"), ("timer_seconds", "duration"),
                       ("elapsed_seconds", "elapsedDuration"), ("moving_seconds", "movingDuration")):
        fields[field] = choose(field, key, number)
    fields["metric_sources"] = json.dumps(origins, sort_keys=True)
    return fields


def original_source(client, store, archive, garmin_id):
    for digest, file_format in store.originals(garmin_id):
        if archive.contains(digest):
            return ("original", digest, file_format), None
    try:
        data = client.get(f"/download-service/files/activity/{garmin_id}", "Garmin original download", raw=True)
    except runs.ConnectionProblem as err:
        return None, f"Original unavailable. {err} Retry these dates to request it again."
    if not data:
        return None, "Original unavailable. Garmin returned an empty file. Retry these dates."
    digest = archive.put(data)
    if is_zipfile(BytesIO(data)):
        return ("original", digest, "zip"), None
    if len(data) >= 12 and data[0] >= 12 and data[8:12] == b".FIT":
        return ("original", digest, "fit"), None
    return ("original_error", digest, "unknown"), "Original unavailable. Garmin returned an unrecognised file. Retry these dates."


def import_one(client, store, archive, import_id, garmin_id, listed, page_digest, first, last):
    data = client.get(f"/activity-service/activity/{garmin_id}", "Garmin activity detail", raw=True)
    detail_digest = archive.put(data)
    detail = decode(data, "detail", dict)
    fields = activity_fields(garmin_id, listed, detail)
    if not first <= fields["local_date"] <= last or fields["sport"] not in RUN_TYPES:
        return
    sources = [("list", page_digest, "json"), ("detail", detail_digest, "json")]
    original, warning = original_source(client, store, archive, garmin_id)
    if original:
        sources.append(original)
    store.save(import_id, fields, sources, warning)


def run_import(store, archive, import_id, first, last):
    range_label = f"{first} to {last}"
    state = "failed"
    try:
        client = open_garmin()
        data = client.search({"activityType": "running", "startDate": first, "endDate": last,
                              "start": 0, "limit": PAGE_SIZE}, raw=True)
        page_digest = archive.put(data)
        page = decode(data, "activity list", list)
        state = "complete"
        seen = set()
        for listed in page:
            garmin_id = str(listed.get("activityId")) if isinstance(listed, dict) else ""
            if not re.fullmatch(r"[1-9][0-9]*", garmin_id):
                store.issue(import_id, range_label, "Activity list", "Missing or invalid Garmin ID. Recheck these dates.")
                state = "partial"
                continue
            if garmin_id in seen:
                continue
            seen.add(garmin_id)
            try:
                import_one(client, store, archive, import_id, garmin_id, listed, page_digest, first, last)
            except runs.ConnectionProblem as err:
                store.issue(import_id, garmin_id, "Activity detail", str(err))
                state = "partial"
            except (UnicodeError, OverflowError):
                store.issue(import_id, garmin_id, "Activity detail", "Invalid activity fields. Check Garmin and retry.")
                state = "partial"
            except (OSError, sqlite3.Error):
                store.issue(import_id, garmin_id, "Save activity", "Check the private archive and SQLite storage, then retry.")
                state = "partial"
        if len(page) >= PAGE_SIZE:
            store.issue(import_id, range_label, "Activity search", "The first page was full. Recheck a smaller date range.")
            state = "partial"
    except runs.ConnectionProblem as err:
        state = "failed"
        store.issue(import_id, range_label, "Activity search", str(err))
    except (OSError, sqlite3.Error):
        state = "failed"
        store.issue(import_id, range_label, "Import storage", "Check the private archive and SQLite storage, then retry.")
    except Exception:
        # Background exceptions cannot be returned to the already accepted request.
        # Keep the report actionable without logging upstream payloads or credentials.
        state = "failed"
        store.issue(import_id, range_label, "Import", "Unexpected import error. Check the saved activities and retry these dates.")
    store.finish(import_id, state)
