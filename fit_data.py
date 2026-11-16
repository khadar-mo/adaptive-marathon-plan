"""Decode a runner's original FIT bytes, whether uploaded or wrapped in a Garmin ZIP."""
from datetime import timedelta
from io import BytesIO
import json
import math
from zipfile import ZipFile

from garmin_fit_sdk import Decoder, Profile, Stream, convert_timestamp_to_datetime


class FitError(ValueError):
    pass


def fit_member(data):
    if data[8:12] == b".FIT":
        return "fit", data
    try:
        with ZipFile(BytesIO(data)) as zipped:
            members = [m for m in zipped.infolist() if not m.is_dir() and m.filename.lower().endswith(".fit")]
            if len(members) != 1:
                raise FitError("Expected one FIT member in the Garmin ZIP.")
            return "zip", zipped.read(members[0])
    except FitError:
        raise
    except Exception:
        # ZIP headers, member names and compression codecs raise different errors.
        raise FitError("Could not read the original FIT or ZIP.") from None


def absolute_time(value):
    # The FIT profile reserves earlier values for times relative to device power-on.
    if type(value) is int and 0x10000000 <= value < 0xffffffff:
        return convert_timestamp_to_datetime(value)
    return None


def measurements(message):
    fields = {}
    for key, source in (("distance_m", "total_distance"), ("timer_seconds", "total_timer_time"),
                        ("elapsed_seconds", "total_elapsed_time"), ("moving_seconds", "total_moving_time")):
        value = message.get(source)
        fields[key] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    return fields


def decode_fit(data):
    decoder = Decoder(Stream.from_byte_array(data))
    messages, errors = decoder.read(convert_datetimes_to_dates=False, convert_types_to_strings=False,
                                   expand_sub_fields=False, merge_heart_rates=False)
    if errors:
        raise FitError("Damaged FIT. Export the original file again and retry.")
    sessions = messages.get("session_mesgs", [])
    activities = messages.get("activity_mesgs", [])
    if len(sessions) > 1 or any(a.get("type") == 1 or a.get("num_sessions", 1) != 1 for a in activities):
        raise FitError("Multisport or multiple sessions. Export a single running activity.")
    file_ids = messages.get("file_id_mesgs", [])
    if len(file_ids) != 1 or file_ids[0].get("type") != 4 or len(sessions) != 1:
        raise FitError("Expected one activity file with one recorded session. Check the export and retry.")
    session, file_id = sessions[0], file_ids[0]
    if session.get("sport") != 1:
        raise FitError("Unsupported sport. Choose a single-session running FIT.")

    start = absolute_time(session.get("start_time"))
    # A timestamp correlation at the start is direct evidence; otherwise use
    # the offset recorded with the activity summary. Never apply the host zone.
    correlations = [m for m in messages.get("timestamp_correlation_mesgs", [])
                    if start and absolute_time(m.get("timestamp")) == start and absolute_time(m.get("local_timestamp"))]
    offsets = set()
    for sample in correlations or activities:
        utc, local = absolute_time(sample.get("timestamp")), absolute_time(sample.get("local_timestamp"))
        if utc and local:
            offsets.add(local - utc)
    local_start = None
    if start and len(offsets) == 1:
        offset = offsets.pop()
        if abs(offset) <= timedelta(hours=14):
            local_start = (start + offset).replace(tzinfo=None).isoformat(sep=" ")

    device = [file_id.get(k) for k in ("manufacturer", "product", "serial_number", "time_created")]
    identity = None
    if start and all(type(v) is int for v in device) and absolute_time(device[-1]):
        identity = json.dumps([*device, start.isoformat()])
    sub_sport = session.get("sub_sport")
    subtype = Profile["types"]["sub_sport"].get(sub_sport) if type(sub_sport) is int else None
    fields = {"garmin_id": None, "name": None, "sport": subtype if subtype and subtype != "generic" else "running",
              "start_utc": start.isoformat() if start else None, "start_local": local_start,
              "local_date": local_start[:10] if local_start else None, **measurements(session)}
    fields["metric_sources"] = json.dumps({k: "fit" for k, v in fields.items() if v is not None}, sort_keys=True)
    laps = messages.get("lap_mesgs", [])
    if all(type(lap.get("message_index")) is int for lap in laps):
        laps = sorted(laps, key=lambda lap: lap["message_index"] & 0x0fff)
    elif all(absolute_time(lap.get("start_time")) for lap in laps):
        laps = sorted(laps, key=lambda lap: lap["start_time"])
    return {"identity": identity, "fields": fields, "laps": [measurements(lap) for lap in laps]}
