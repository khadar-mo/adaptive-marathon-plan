"""Synthetic recordings generated from made-up values with the Garmin FIT encoder."""
from datetime import datetime, timezone

from garmin_fit_sdk import Encoder, convert_datetime_to_timestamp


START = convert_datetime_to_timestamp(datetime(2024, 2, 28, 23, 15, 38, tzinfo=timezone.utc))


def make_fit(*, start=START, offset=3600, sport=1, sessions=1, laps=(), metrics=None,
             manufacturer=1, product=1234, serial=7654321, created=START, file_number=None, messages=()):
    encoder = Encoder()
    file_id = {"type": 4, "manufacturer": manufacturer, "product": product,
               "serial_number": serial, "time_created": created, "number": file_number}
    encoder.on_mesg(0, {k: v for k, v in file_id.items() if v is not None})
    for distance, timer, elapsed in laps:
        encoder.on_mesg(19, {"total_distance": distance, "total_timer_time": timer, "total_elapsed_time": elapsed})
    for number, message in messages:
        encoder.on_mesg(number, message)
    for _ in range(sessions):
        encoder.on_mesg(18, {"sport": sport, "start_time": start, **(metrics or {})})
    activity = {"timestamp": start + 1800, "num_sessions": sessions}
    if offset is not None:
        activity["local_timestamp"] = activity["timestamp"] + offset
    encoder.on_mesg(34, activity)
    return bytes(encoder.close())
