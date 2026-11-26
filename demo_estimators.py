"""Synthetic G4 trials only. Never import these into an athlete's real history."""
import argparse
from pathlib import Path

from demo_efforts import dense, recording
from demo_recordings import START


CUTOFF = "2026-01-15T00:00:00+00:00"


def examples():
    # The 800 and 5000 m crossings lie one third through the last 10-second
    # bracket. Their achieved elapsed times are (distance - 100) / 3 exactly
    # to binary64 interpolation precision; raw FIT distances remain untouched.
    return {
        "g4-400m-100s.fit": recording(dense([(0, 0), (100, 400)]), start=START),
        "g4-800m-233-third-s.fit": recording(dense([(0, 0), (230, 790), (240, 820)]), start=START + 86400),
        "g4-5000m-1633-third-s.fit": recording(dense([(0, 0), (1630, 4990), (1640, 5020)]), start=START + 172800),
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
