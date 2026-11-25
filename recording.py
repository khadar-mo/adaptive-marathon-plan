"""Read-only G0 inspection of one archived FIT; no trace correction or PB eligibility."""
from importlib.metadata import version
import math

from garmin_fit_sdk import Profile

from fit_data import DECODE_OPTIONS, absolute_time, measurements, read_messages
from garmin_activity import archived_fit, original_fit


INSPECTION_VERSION = "g0-fit-inspection-v1"
LIMITATIONS = [
    "Records are shown in file order. No samples are sorted, filled, removed or corrected.",
    "Coverage describes fields supplied by this file, not trace quality or proof of continuous movement.",
    "Missing timer events do not mean there were no pauses. Recorded events may also omit stops.",
    "This inspection does not calculate or validate fastest efforts.",
]


class UnknownRecording(LookupError):
    pass


def utc(value):
    parsed = absolute_time(value)
    return parsed.isoformat() if parsed else None


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def json_safe(value):
    """Keep unusual decoded values inspectable without emitting invalid JSON."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def label(kind, code):
    return Profile["types"].get(kind, {}).get(code, "unknown") if type(code) is int else "unknown"


def sample(index, fields):
    return {"record_index": index, "timestamp_utc": utc(fields.get("timestamp")),
            "distance_m": finite(fields.get("distance")), "heart_rate_bpm": finite(fields.get("heart_rate")),
            "elevation_m": finite(fields.get("enhanced_altitude", fields.get("altitude"))),
            "speed_m_per_s": finite(fields.get("enhanced_speed", fields.get("speed"))),
            "cadence_rpm": finite(fields.get("cadence")), "temperature_c": finite(fields.get("temperature")),
            "position_lat_semicircles": finite(fields.get("position_lat")),
            "position_long_semicircles": finite(fields.get("position_long")), "fields": json_safe(fields)}


def event(index, fields):
    return {"event_index": index, "timestamp_utc": utc(fields.get("timestamp")),
            "event": label("event", fields.get("event")),
            "event_type": label("event_type", fields.get("event_type")), "fields": json_safe(fields)}


def coverage(values):
    count = sum(v is not None for v in values)
    total = len(values)
    return {"status": "NOT_PROVIDED" if count == 0 else "RECORDED" if count == total else "PARTIAL",
            "present": count, "total": total, "missing": total - count}


def inspect(data, offset, limit):
    messages = read_messages(data)
    sessions = messages.get("session_mesgs", [])
    file_ids = messages.get("file_id_mesgs", [])
    if len(sessions) != 1 or len(file_ids) != 1 or sessions[0].get("sport") != 1:
        raise ValueError("Expected an indexed single-session running FIT.")
    session = sessions[0]
    samples = [sample(i, fields) for i, fields in enumerate(messages.get("record_mesgs", []))]
    heart_rates = [{"hr_index": i, "timestamp_utc": utc(fields.get("timestamp")),
                   "filtered_bpm": json_safe(fields.get("filtered_bpm")), "fields": json_safe(fields)}
                  for i, fields in enumerate(messages.get("hr_mesgs", []))]
    events = [event(i, fields) for i, fields in enumerate(messages.get("event_mesgs", []))]
    timers = [e for e in events if e["fields"].get("event") == 0]
    laps = [{"lap_index": i, "start_utc": utc(fields.get("start_time")),
             "end_utc": utc(fields.get("timestamp")), **measurements(fields), "fields": json_safe(fields)}
            for i, fields in enumerate(messages.get("lap_mesgs", []))]
    channels = {key: coverage([s[key] for s in samples]) for key in (
        "timestamp_utc", "distance_m", "heart_rate_bpm", "elevation_m", "speed_m_per_s", "cadence_rpm", "temperature_c")}
    channels["gps"] = coverage([True if s["position_lat_semicircles"] is not None and
                                s["position_long_semicircles"] is not None else None for s in samples])
    # Presence of an event is distinct from a fully interpretable timestamp/type.
    timer_status = ("NOT_PROVIDED" if not timers else "RECORDED" if all(
        e["timestamp_utc"] is not None and e["event_type"] != "unknown" for e in timers) else "PARTIAL")
    return {
        "header": {"start_utc": utc(session.get("start_time")), "end_utc": utc(session.get("timestamp")),
                   "sport": label("sport", session.get("sport")), **measurements(session),
                   "file_id": json_safe(file_ids[0]), "session": json_safe(session),
                   "device_info": json_safe(messages.get("device_info_mesgs", [])),
                   "time_correlations": json_safe(messages.get("timestamp_correlation_mesgs", [])),
                   "activity_summaries": json_safe(messages.get("activity_mesgs", []))},
        "coverage": {"samples": channels, "event_count": len(events), "timer_event_count": len(timers),
                     "heart_rate_message_count": len(heart_rates),
                     "heart_rate_message_timestamp_utc": coverage([h["timestamp_utc"] for h in heart_rates]),
                     "heart_rate_message_bpm": coverage([h["filtered_bpm"] for h in heart_rates]),
                     "timer_events": timer_status, "lap_count": len(laps),
                     "lap_start_utc": coverage([lap["start_utc"] for lap in laps]),
                     "lap_end_utc": coverage([lap["end_utc"] for lap in laps])},
        "samples": {"total": len(samples), "offset": offset, "limit": limit, "rows": samples[offset:offset + limit]},
        "events": events, "laps": laps, "heart_rate_messages": heart_rates,
    }


def sources(store, activity):
    """Only return recordings owned by this activity, including older linked versions."""
    with store.history() as db:
        entries = {row[0]: {"fit_sha256": row[0], "sources": []} for row in db.execute(
            "SELECT sha256 FROM fit_sources WHERE activity_id=? ORDER BY sha256", (activity["id"],))}
        for source in activity["sources"]:
            sha = source["sha256"] if source["kind"] == "fit" else None
            if source["kind"] in ("original", "original_unavailable"):
                member = db.execute("SELECT fit_sha FROM original_members WHERE original_sha=?",
                                    (source["sha256"],)).fetchone()
                sha = member[0] if member else None
            if sha:
                entries.setdefault(sha, {"fit_sha256": sha, "sources": []})["sources"].append(source)
        preferred = original_fit(db, activity["garmin_id"], activity["id"]) if activity["garmin_id"] else None
    return sorted(entries.values(), key=lambda r: (r["fit_sha256"] != (preferred[0] if preferred else None),
                                                  r["fit_sha256"]))


def read(store, activity_id, source=None, *, offset=0, limit=200):
    activity = store.activity(activity_id)
    if activity is None:
        return None
    recordings = sources(store, activity)
    selected = next((r for r in recordings if r["fit_sha256"] == source), None) if source else next(iter(recordings), None)
    if source and selected is None:
        raise UnknownRecording("That recording does not belong to this activity.")
    result = {"activity_id": activity["id"], "garmin_ids": activity["garmin_ids"], "recordings": recordings,
              "selected_fit_sha256": selected["fit_sha256"] if selected else None,
              "provenance": {"inspection_version": INSPECTION_VERSION, "decoder": "garmin-fit-sdk",
                             "decoder_version": version("garmin-fit-sdk"), "decode_options": dict(DECODE_OPTIONS),
                             "sample_indices": "zero-based record_mesgs order",
                             "heart_rate_indices": "zero-based hr_mesgs order",
                             "event_indices": "zero-based event_mesgs order", "lap_indices": "zero-based lap_mesgs order"},
              "state": "UNAVAILABLE", "limitations": list(LIMITATIONS), "header": None, "coverage": None,
              "samples": {"total": 0, "offset": offset, "limit": limit, "rows": []},
              "events": [], "laps": [], "heart_rate_messages": []}
    if selected is None:
        result["limitations"].insert(0, "No original FIT is indexed for this activity. Upload its exported FIT from Imports.")
        return result
    data = None
    # A missing extracted member may still be recovered read-only from its original ZIP.
    for outer in [None, *(s["sha256"] for s in selected["sources"] if s["kind"] != "fit")]:
        try:
            data = archived_fit(store.archive, selected["fit_sha256"], outer)
            if data is not None:
                break
        except (OSError, ValueError):
            continue
    if data is None:
        result["limitations"].insert(0, "Original FIT unavailable or its checksum failed. Re-import the original file to restore it.")
        return result
    try:
        parsed = inspect(data, offset, limit)
    except (ValueError, KeyError):
        result["limitations"].insert(0, "The archived FIT could not be decoded. Re-export the original file and import it again.")
        return result
    return {**result, **parsed, "state": "AVAILABLE"}
