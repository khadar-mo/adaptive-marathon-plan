"""Create three synthetic FIT files for the G0 manual inspection walkthrough."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from garmin_fit_sdk import Encoder, convert_datetime_to_timestamp


START = convert_datetime_to_timestamp(datetime(2026, 1, 10, 9, tzinfo=timezone.utc))


def example(start, *, records, events, laps, distance, elapsed, timer):
    encoder = Encoder()
    encoder.on_mesg(0, {"type": 4, "manufacturer": 1, "product": 1234,
                        "serial_number": 7654321, "time_created": start})
    for fields in records:
        encoder.on_mesg(20, fields)
    for fields in events:
        encoder.on_mesg(21, fields)
    for fields in laps:
        encoder.on_mesg(19, fields)
    encoder.on_mesg(18, {"sport": 1, "start_time": start, "timestamp": start + elapsed,
                         "total_distance": distance, "total_elapsed_time": elapsed, "total_timer_time": timer})
    encoder.on_mesg(34, {"timestamp": start + elapsed, "local_timestamp": start + elapsed,
                         "num_sessions": 1})
    return bytes(encoder.close())


def examples():
    rich = example(START,
        records=[{"timestamp": START + i, "distance": 3 * (i if i <= 180 else max(180, i - 30)),
                  "heart_rate": 140 + i % 10, "cadence": 85, "altitude": 20, "speed": 0 if 180 <= i <= 210 else 3}
                 for i in range(401)],
        events=[{"timestamp": START + t, "event": 0, "event_type": k}
                for t, k in ((0, 0), (180, 4), (210, 0), (400, 4))],
        laps=[{"message_index": n, "start_time": START + a, "timestamp": START + b,
               "total_distance": d, "total_elapsed_time": b - a, "total_timer_time": timer}
              for n, a, b, d, timer in ((0, 0, 210, 540, 180), (1, 210, 400, 570, 190))],
        distance=1110, elapsed=400, timer=370)
    no_events = example(START + 86400,
        records=[{"timestamp": START + 86400 + i, "distance": 3 * i} for i in range(205)],
        events=[], laps=[], distance=612, elapsed=204, timer=204)
    summary = example(START + 172800, records=[], events=[], laps=[], distance=2000, elapsed=600, timer=600)
    return {"sample-rich.fit": rich, "no-timer-events.fit": no_events, "summary-only.fit": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory for synthetic FIT exports (no real activity data).")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name, data in examples().items():
        target = args.directory / name
        if target.exists():
            if target.read_bytes() != data:
                raise SystemExit(f"Refusing to replace a different file: {target}")
        else:
            with target.open("xb") as output:
                output.write(data)
        print(target)


if __name__ == "__main__":
    main()
