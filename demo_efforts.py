"""Made-up G2 exports; use a separate demo database, never real athlete history."""
from pathlib import Path
import argparse

from demo_recordings import START, example


def recording(points, *, start=START, events=()):
    return example(start, records=[{"timestamp": start + t, "distance": d} for t, d in points],
        events=[{"timestamp": start + t, "event": 0, "event_type": k} for t, k in events], laps=[],
        distance=points[-1][1], elapsed=points[-1][0], timer=points[-1][0])


def dense(knots, step=5):
    points = [knots[0]]
    for (ta, da), (tb, db) in zip(knots, knots[1:]):
        points.extend((t, da + (db - da) * (t - ta) / (tb - ta)) for t in range(ta + step, tb, step))
        points.append((tb, db))
    return points


def examples():
    return {
        "15k-with-24-minute-5k.fit": recording(dense([(0, 0), (900, 3000), (2340, 8000), (4440, 15000)])),
        "whole-5k-25-minutes.fit": recording(dense([(0, 0), (1500, 5000)]), start=START + 86400),
        "unknown-12-second-plateau.fit": recording(dense([(0, 0), (150, 500), (162, 500), (312, 1000)]), start=START + 172800),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name, raw in examples().items():
        target = args.directory / name
        if target.exists():
            if target.read_bytes() != raw:
                raise SystemExit(f"Refusing to overwrite a different file: {target}")
        else:
            with target.open("xb") as output:
                output.write(raw)
        print(target)


if __name__ == "__main__":
    main()
