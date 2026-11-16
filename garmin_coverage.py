"""Durable evidence for Garmin date scans, retries and the normal-sync window."""
from datetime import date, datetime
import hashlib
import json
from zoneinfo import ZoneInfo


BACKFILL = date(2026, 6, 1).toordinal()
MAX_DAY = date.max.toordinal()

SCHEMA = """
CREATE TABLE IF NOT EXISTS garmin_scans (
    id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL REFERENCES imports(id),
    mode TEXT NOT NULL, first TEXT NOT NULL, last TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0, enumerated INTEGER NOT NULL DEFAULT 0,
    unidentified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS garmin_scan_import ON garmin_scans(import_id);
CREATE TABLE IF NOT EXISTS garmin_scan_items (
    scan_id INTEGER NOT NULL REFERENCES garmin_scans(id), garmin_id TEXT NOT NULL,
    local_date TEXT, list_sha TEXT, record_sha TEXT, detail_sha TEXT,
    status TEXT NOT NULL DEFAULT 'missing_activity', from_search INTEGER NOT NULL DEFAULT 1,
    original_requested INTEGER NOT NULL DEFAULT 0, original_available INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(scan_id, garmin_id)
);
CREATE INDEX IF NOT EXISTS garmin_item_history ON garmin_scan_items(garmin_id, scan_id DESC);
"""


def today():
    return datetime.now(ZoneInfo("Europe/London")).date()


def iso(day):
    return date.fromordinal(day).isoformat()


def bounds(first, last):
    return date.fromisoformat(first).toordinal(), date.fromisoformat(last).toordinal()


def search_bounds(first, last):
    start, end = bounds(first, last)
    return iso(max(1, start - 1)), iso(min(MAX_DAY, end + 1))


def difference(ranges, cuts):
    """Subtract sorted inclusive intervals without expanding a backfill into days."""
    result = []
    for start, end in ranges:
        for left, right in cuts:
            if right < start:
                continue
            if left > end:
                break
            if start < left:
                result.append((start, left - 1))
            start = max(start, right + 1)
        if start <= end:
            result.append((start, end))
    return result


def merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def record_hash(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def migrate(db):
    # PR #9 never paged to an empty terminator. Keep its reports and queued failures.
    has_source = any(col["name"] == "source" for col in db.execute("PRAGMA table_info(imports)"))
    garmin_only = " AND source='garmin'" if has_source else ""
    db.execute("""INSERT INTO garmin_scans(import_id, mode, first, last)
                  SELECT id, 'legacy', requested_from, requested_to FROM imports
                  WHERE requested_from IS NOT NULL AND requested_to IS NOT NULL""" + garmin_only + " ORDER BY id")
    db.execute("""INSERT INTO garmin_scan_items(scan_id, garmin_id, local_date, list_sha, status, original_available)
                  SELECT s.id, a.garmin_id, a.local_date,
                         (SELECT sha256 FROM activity_sources WHERE activity_id=a.id AND kind='list'
                          ORDER BY rowid DESC LIMIT 1), 'saved',
                         NOT EXISTS (SELECT 1 FROM import_issues i WHERE i.import_id=s.import_id
                                     AND i.item=a.garmin_id AND i.step='Original download')
                  FROM garmin_scans s JOIN import_activities ia ON ia.import_id=s.import_id
                  JOIN activities a ON a.id=ia.activity_id WHERE a.garmin_id IS NOT NULL""")
    db.execute("""INSERT OR IGNORE INTO garmin_scan_items(scan_id, garmin_id, status)
                  SELECT s.id, i.item, 'missing_activity' FROM garmin_scans s
                  JOIN import_issues i ON i.import_id=s.import_id
                  WHERE i.step IN ('Activity detail', 'Save activity') AND i.severity='error'
                  AND i.item <> '' AND i.item NOT GLOB '*[^0-9]*'""")


def checked_ranges(db, scan):
    if not scan["enumerated"] or scan["unidentified"]:
        return []
    window = observed_window(scan)
    if not window:
        return []
    missing = []
    for row in db.execute("""SELECT DISTINCT local_date FROM garmin_scan_items
                             WHERE scan_id=? AND status IN ('missing_activity', 'pending_identity')""", (scan["id"],)):
        day = row["local_date"]
        if day is None or not scan["first"] <= day <= scan["last"]:
            return []
        missing.append(bounds(day, day))
    return difference(window, sorted(missing))


def observed_window(scan):
    # A successful search today cannot certify future dates from an explicit request.
    observed = datetime.fromisoformat(scan["started_at"]).astimezone(ZoneInfo("Europe/London")).date().isoformat()
    end = min(scan["last"], observed)
    return [bounds(scan["first"], end)] if scan["first"] <= end else []


def outstanding_items(db):
    # Seeing an ID on a widened boundary cannot settle an in-range failure.
    return db.execute("""WITH latest AS (
            SELECT garmin_id, max(scan_id) AS scan_id FROM garmin_scan_items
            WHERE status != 'outside_range' GROUP BY garmin_id)
        SELECT i.*, s.first, s.last FROM latest l JOIN garmin_scan_items i USING(garmin_id, scan_id)
        JOIN garmin_scans s ON s.id=i.scan_id
        WHERE i.status IN ('missing_activity', 'pending_identity', 'retry_elsewhere') OR (i.status='saved' AND i.original_available=0)
        ORDER BY i.scan_id, i.garmin_id""").fetchall()


def item_range(item):
    day = item["local_date"]
    if day and (item["status"] == "retry_elsewhere" or item["first"] <= day <= item["last"]):
        return bounds(day, day)
    return bounds(item["first"], item["last"])


def plan(db, first, last):
    if first is not None:
        return [("range", first, last)]
    verified, pending = [], []
    scans = db.execute("""SELECT s.*, i.state, i.started_at FROM garmin_scans s JOIN imports i ON s.import_id=i.id
                          ORDER BY s.id""")
    for scan in scans:
        window = observed_window(scan)
        if scan["mode"] == "legacy" and scan["state"] == "complete":
            continue
        checked = checked_ranges(db, scan)
        verified = merged(difference(verified, window) + checked)
        pending = merged(difference(pending, window) + difference(window, checked))

    end = today().toordinal()
    start = BACKFILL
    for left, right in verified:
        if left <= BACKFILL <= right:
            start = max(BACKFILL, min(right, end) - 13)
            break
    main = (start, end)
    pending.extend(item_range(item) for item in outstanding_items(db))
    retries = difference(merged(pending), [main])
    return [("normal", iso(start), iso(end)), *(("retry", iso(a), iso(b)) for a, b in retries)]


def legacy_report(first, last):
    return {"mode": "legacy", "from": first, "to": last, "coverage": "unchecked",
            "search_from": first, "search_to": last, "pages_read": None, "enumerated": False,
            "scanned_from": None, "scanned_to": None, "checked_ranges": [], "unique_ids_found": None, "saved_garmin_ids": None,
            "excluded": None, "missing_metadata": None, "originals_requested": None, "originals_available": None}


def scan_report(db, scan):
    if scan["mode"] == "legacy":
        return legacy_report(scan["first"], scan["last"])
    items = db.execute("SELECT * FROM garmin_scan_items WHERE scan_id=?", (scan["id"],)).fetchall()
    saved = [i for i in items if i["status"] == "saved"]
    dates = [i["local_date"] for i in saved if i["local_date"]]
    missing = [i["garmin_id"] for i in items if i["status"] in ("missing_activity", "pending_identity")]
    counted = [i for i in items if i["status"] in ("saved", "missing_activity", "pending_identity")]
    checked = checked_ranges(db, scan)
    if not scan["enumerated"] or not observed_window(scan):
        status = "unchecked"
    elif missing or scan["unidentified"]:
        status = "missing_activity"
    elif checked != [bounds(scan["first"], scan["last"])]:
        status = "unchecked"
    else:
        status = "checked" if saved else "checked_empty"
    search_from, search_to = search_bounds(scan["first"], scan["last"])
    return {
        "mode": scan["mode"], "from": scan["first"], "to": scan["last"], "coverage": status,
        "search_from": search_from, "search_to": search_to, "pages_read": scan["pages"],
        "scanned_from": scan["first"] if scan["enumerated"] else None,
        "scanned_to": scan["last"] if scan["enumerated"] else None, "enumerated": bool(scan["enumerated"]),
        "checked_ranges": [{"from": iso(a), "to": iso(b)} for a, b in checked],
        "unique_ids_found": sum(i["from_search"] for i in counted),
        "saved_garmin_ids": sorted(i["garmin_id"] for i in saved),
        "excluded": sum(i["status"] in ("outside_range", "resolved_outside", "retry_elsewhere", "excluded_sport")
                        for i in items),
        "missing_activity_ids": missing, "unidentified_records": scan["unidentified"],
        "missing_metadata": len(missing) + scan["unidentified"],
        "originals_requested": sum(i["original_requested"] for i in items),
        "originals_available": sum(i["original_available"] for i in saved),
        "missing_original_ids": [i["garmin_id"] for i in saved if not i["original_available"]],
        "earliest_saved": min(dates, default=None), "latest_saved": max(dates, default=None),
    }
