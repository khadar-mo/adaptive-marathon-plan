"""Read optional sensor evidence from the locally archived activity sources."""
import json

from fit_data import read_messages


def heart_rate(value):
    return value if type(value) in (int, float) and 0 < value < 255 else None


def position(record, latitude, longitude, units=1):
    lat, lon = record.get(latitude), record.get(longitude)
    return (type(lat) in (int, float) and type(lon) in (int, float) and
            -90 * units <= lat <= 90 * units and -180 * units <= lon <= 180 * units)


def fit_sensors(data):
    messages = read_messages(data)
    session, = messages["session_mesgs"]  # Only indexed single-session running FITs reach activity detail.
    units = 2**31 / 180  # FIT positions are recorded in semicircles.
    gps = position(session, "start_position_lat", "start_position_long", units) or any(
        position(r, "position_lat", "position_long", units) for r in messages.get("record_mesgs", []))
    return heart_rate(session.get("avg_heart_rate")), gps or None


def json_sensors(data, gid):
    parsed = json.loads(data)
    records = parsed if isinstance(parsed, list) else [parsed]
    for record in records:
        if not isinstance(record, dict) or str(record.get("activityId")) != gid:
            continue
        summary = record.get("summaryDTO")
        summaries = [summary, record] if isinstance(summary, dict) else [record]
        hr = next((v for r in summaries if (v := heart_rate(r.get("averageHR"))) is not None), None)
        gps = any(position(r, "startLatitude", "startLongitude") or position(r, "endLatitude", "endLongitude")
                  or r.get("hasPolyline") is True for r in summaries)
        return hr, gps or None
    return None, None


def sensor_sources(db, activity, sources):
    gid, aid = activity["garmin_id"], activity["id"]
    if gid:
        # An explicit edit can return to an older content hash. Use the selected scan,
        # not archive insertion order, and never borrow another linked Garmin ID's summary.
        latest = db.execute("""SELECT i.detail_sha, i.list_sha FROM garmin_scan_items i
            WHERE i.garmin_id=? AND i.status IN ('saved','excluded_sport','outside_range','resolved_outside','retry_elsewhere')
            AND EXISTS (SELECT 1 FROM activity_sources s WHERE s.activity_id=? AND s.kind='detail' AND s.sha256=i.detail_sha)
            ORDER BY i.scan_id DESC LIMIT 1""", (gid, aid)).fetchone()
        for index, kind in enumerate(("detail", "list")):
            if latest:
                if latest[index]:
                    yield kind, latest[index]
            else:
                # Legacy imports did not record the selected version. Use unambiguous evidence.
                versions = [s["sha256"] for s in sources if s["kind"] == kind]
                if len(versions) == 1:
                    yield kind, versions[0]
        original = db.execute("""SELECT m.fit_sha FROM garmin_original_versions v
            JOIN original_members m ON m.original_sha=v.original_sha
            JOIN activity_sources s ON s.sha256=m.original_sha AND s.activity_id=?
            WHERE v.garmin_id=? ORDER BY v.last_import_id DESC, v.rowid DESC LIMIT 1""", (aid, gid)).fetchone()
        if not original:
            original = db.execute("""SELECT m.fit_sha FROM legacy_garmin_originals l
                JOIN original_members m ON m.original_sha=l.original_sha
                WHERE l.garmin_id=? ORDER BY l.rowid DESC LIMIT 1""", (gid,)).fetchone()
        if original and original[0]:
            yield "fit", original[0]
    for source in sources:
        if source["kind"] == "fit":
            yield "fit", source["sha256"]


def read(db, archive, activity, sources):
    values = {"average_hr_bpm": None, "gps_available": None}
    origins = {}
    for kind, digest in sensor_sources(db, activity, sources):
        try:
            data = archive.read(digest)
            if data is None:
                continue
            pair = fit_sensors(data) if kind == "fit" else json_sensors(data, activity["garmin_id"])
        except (OSError, ValueError, KeyError, RecursionError):
            continue
        for key, value in zip(values, pair):
            if values[key] is None and value is not None:
                values[key], origins[key] = value, kind
        if all(v is not None for v in values.values()):
            break
    return values, origins
