"""Garmin headline fields and the local evidence for explicitly linked activity IDs."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from fit_data import decode_fit, fit_member
from fit_store import merge_fields
import runs


RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "virtual_run", "virtual_running",
             "indoor_track_running", "indoor_running", "ultra_run", "obstacle_run", "street_running"}


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
    utc = choose("start_utc", "startTimeGMT", timestamp)
    if utc and utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    name = detail.get("activityName") or listed.get("activityName")
    activity_sport = sport(detail, "activityTypeDTO") or sport(listed, "activityType")
    fields = {
        "garmin_id": garmin_id, "name": name if isinstance(name, str) else None,
        "sport": activity_sport,
        "start_local": local, "local_date": local[:10] if local else None,
        "start_utc": utc.astimezone(timezone.utc).isoformat() if utc else None,
    }
    for field, key in (("distance_m", "distance"), ("timer_seconds", "duration"),
                       ("elapsed_seconds", "elapsedDuration"), ("moving_seconds", "movingDuration")):
        fields[field] = choose(field, key, number)
    fields["metric_sources"] = json.dumps(origins, sort_keys=True)
    return fields


def legacy_selected_source(db, garmin_id, kind):
    """A source acquired exclusively during the final legacy save identifies its selected version."""
    selected = db.execute("""WITH attempts AS (
            SELECT i.id, i.started_at, i.finished_at, a.id AS activity_id FROM imports i
            JOIN import_activities ia ON ia.import_id=i.id JOIN activities a ON a.id=ia.activity_id
            JOIN garmin_scans g ON g.import_id=i.id AND g.mode='legacy' WHERE a.garmin_id=?
        ), last_save AS (SELECT * FROM attempts ORDER BY id DESC LIMIT 1)
        SELECT s.sha256 FROM last_save l JOIN activity_sources s ON s.activity_id=l.activity_id
        WHERE s.kind=? AND s.acquired_at BETWEEN l.started_at AND l.finished_at
        AND NOT EXISTS (SELECT 1 FROM attempts p WHERE p.id!=l.id AND p.started_at<=s.acquired_at
                        AND (p.finished_at IS NULL OR s.acquired_at<=p.finished_at))""", (garmin_id, kind)).fetchall()
    return selected[0][0] if len(selected) == 1 else None


def json_record(data, gid):
    parsed = json.loads(data)
    records = parsed if isinstance(parsed, list) else [parsed]
    return next((r for r in records if isinstance(r, dict) and str(r.get("activityId")) == gid), {})


def active_aliases(db, activity_id=None):
    """Latest classified observation of each linked ID. A failed fetch keeps the last good one."""
    result = defaultdict(list)
    for row in db.execute("""SELECT a.activity_id, i.* FROM garmin_aliases a JOIN garmin_scan_items i
            ON i.garmin_id=a.garmin_id AND i.scan_id=(
                SELECT max(s.scan_id) FROM garmin_scan_items s WHERE s.garmin_id=a.garmin_id
                AND s.status IN ('saved','outside_range','resolved_outside','retry_elsewhere','excluded_sport'))
            WHERE (? IS NULL OR a.activity_id=?) AND i.status!='excluded_sport' AND i.local_date IS NOT NULL
            ORDER BY a.garmin_id""", (activity_id, activity_id)):
        result[row["activity_id"]].append(row)
    return result


def original_fit(db, gid, aid):
    original = db.execute("""SELECT m.fit_sha, m.original_sha FROM garmin_original_versions v
        JOIN original_members m ON m.original_sha=v.original_sha
        JOIN activity_sources s ON s.sha256=m.original_sha AND s.activity_id=?
        WHERE v.garmin_id=? ORDER BY v.last_import_id DESC, v.rowid DESC LIMIT 1""", (aid, gid)).fetchone()
    if not original:
        original = db.execute("""SELECT m.fit_sha, m.original_sha FROM legacy_garmin_originals l
            JOIN original_members m ON m.original_sha=l.original_sha
            WHERE l.garmin_id=? ORDER BY l.rowid DESC LIMIT 1""", (gid,)).fetchone()
    return tuple(original) if original and original[0] else None


def archived_fit(archive, member_sha, original_sha=None):
    try:
        member = archive.read(member_sha)
        if member is not None:
            return member
    except OSError:
        pass
    if original_sha and original_sha != member_sha:
        original = archive.read(original_sha)
        if original:
            _, member = fit_member(original)
            if hashlib.sha256(member).hexdigest() == member_sha:
                return member
    return None


def archived_record(archive, digest, gid):
    try:
        data = digest and archive.read(digest)
        return json_record(data, gid) if data else {}
    except (OSError, ValueError, RecursionError):
        return {}


def running_headline(db, archive, row, aliases, *, include_laps=False):
    """Prefer the primary running headline. If reclassified, use a still-running linked recording."""
    if not aliases or not row["garmin_id"] or row["sport"] in RUN_TYPES:
        return row
    selected = aliases[0]
    gid = selected["garmin_id"]
    listed = archived_record(archive, selected["list_sha"], gid)
    detail = archived_record(archive, selected["detail_sha"], gid)
    fields = activity_fields(gid, listed, detail or {"activityId": gid})
    fields["local_date"] = selected["local_date"]
    fields["sport"] = fields["sport"] or "running"
    original = original_fit(db, gid, row["id"])
    fit = None
    try:
        member = original and archived_fit(archive, *original)
        if member:
            fit = decode_fit(member)
            fields = merge_fields(fields, fit["fields"])
    except (OSError, ValueError):
        pass
    if include_laps:
        # Two explicitly linked recordings can have different lap buttons and pause times.
        fields["laps"] = [{**lap, "sequence": n, "source": "fit"} for n, lap in enumerate(fit["laps"], 1)] if fit else []
        fields["original_warning"] = None if fit else "Original unavailable. Recheck these dates to request it again."
    return {**row, **fields}
