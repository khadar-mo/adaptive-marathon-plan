"""Write synthetic G1 examples; upload only into a separate demo database."""
import argparse
from pathlib import Path

from demo_recordings import START, example, examples as recording_examples


def examples():
    result = recording_examples()
    for n, (name, points) in enumerate((
        ("gap-31s.fit", [(0, 0), (1, 3), (32, 6), (33, 9)]),
        ("unknown-30s-plateau.fit", [(0, 0), (1, 3), (31, 3), (32, 6)]),
        ("distance-reversal.fit", [(0, 0), (1, 3), (2, 2), (3, 6)]),
    ), 3):
        start = START + n * 86400
        result[name] = example(start, records=[{"timestamp": start + t, "distance": d} for t, d in points],
                               events=[], laps=[], distance=points[-1][1], elapsed=points[-1][0], timer=points[-1][0])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name, data in examples().items():
        target = args.directory / name
        if target.exists():
            if target.read_bytes() != data:
                raise SystemExit(f"Refusing to replace a different file: {target}")
        else:
            with target.open("xb") as saved:
                saved.write(data)
        print(target)


if __name__ == "__main__":
    main()
