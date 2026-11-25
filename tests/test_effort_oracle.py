"""Independent rational polygon/QC oracle; no production search or QC helpers."""
from datetime import timedelta
from fractions import Fraction
import random

import pytest

from fit_data import absolute_time
from fit_fixture import START
from test_efforts import pure


def reference(points, target, lower):
    points = [(Fraction(t), Fraction(d)) for t, d in points]
    target = Fraction(target)
    low, high = points[0][1], points[-1][1] - target
    if high < low:
        return None, None
    breaks = {low, high}
    for _, d in points:
        for x in (d, d - target):
            if low <= x <= high:
                breaks.add(x)
    # Time membership changes where the forward trace crosses the requested
    # clock. A plateau's coincident distance does not delay its first arrival.
    if lower is not None:
        for (ta, da), (tb, db) in zip(points, points[1:]):
            if ta <= lower <= tb:
                x = da + (lower - ta) * (db - da) / (tb - ta) - target
                if low <= x <= high:
                    breaks.add(x)

    bad = []
    for (ta, da), (tb, db) in zip(points, points[1:]):
        if tb - ta > 30 or (db - da) / (tb - ta) > 12:
            bad.append((ta, tb))
    i = 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and points[j + 1][1] == points[i][1]:
            j += 1
        if points[j][0] - points[i][0] >= 10:
            bad.append((points[i][0], points[j][0]))
        i = j + 1

    def crossing(x, departure):
        matches = [i for i, (_, d) in enumerate(points) if d == x]
        if matches:
            i = matches[-1] if departure else matches[0]
            return points[i][0], i, i
        for i, ((ta, da), (tb, db)) in enumerate(zip(points, points[1:])):
            if da < x < db:
                return ta + (x - da) * (tb - ta) / (db - da), i, i + 1
        raise AssertionError(x)

    def performance(x):
        start, sa, sb = crossing(x, True)
        end, ea, eb = crossing(x + target, False)
        eligible = (end > start and eb - sa + 1 >= 4
                    and points[sb][0] - points[sa][0] <= 15
                    and points[eb][0] - points[ea][0] <= 15
                    and not any(max(start, a) < min(end, b) for a, b in bad)
                    and (lower is None or end >= lower))
        return end - start, eligible

    ordered = sorted(breaks)
    attained = [value for x in ordered for value, eligible in [performance(x)] if eligible]
    limits = []
    for left, right in zip(ordered, ordered[1:]):
        q1, q3 = (3 * left + right) / 4, (left + 3 * right) / 4
        (v1, ok1), (v3, ok3) = performance(q1), performance(q3)
        assert ok1 == ok3, 'Reference partition must cover every QC/window change'
        if ok1:
            slope = (v3 - v1) / (q3 - q1)
            limits.extend((v1 + slope * (left - q1), v3 + slope * (right - q3)))
            if slope == 0:
                attained.append(v1)
    best = min(attained) if attained else None
    infimum = min(limits) if limits else None
    if infimum is not None and (best is None or infimum < best):
        return None, infimum
    return best, None


def test_independent_rational_qc_cell_and_recency_oracle():
    rng = random.Random(1702)
    for _ in range(100):
        points = [(0, 0)]
        for _ in range(12):
            t, d = points[-1]
            points.append((t + rng.choice((1, 2, 2, 5, 31)), d + rng.randrange(36)))
        for target in (15, points[-1][1] // 2, points[-1][1]):
            for lower in (None, Fraction(rng.randrange(2 * points[-1][0] + 3), 2)):
                expected, limit = reference(points, target, lower)
                cutoff = (absolute_time(START) + timedelta(seconds=float(lower))).isoformat() if lower is not None else None
                actual = pure(points, [target], min_end=cutoff)["distances"][0]
                context = (points, target, lower, expected, limit, actual["state"])
                if expected is not None:
                    assert actual["best"] is not None, context
                    assert actual["best"]["elapsed_seconds"] == pytest.approx(float(expected), abs=1e-9), context
                else:
                    assert actual["best"] is None, context
                if limit is not None:
                    assert actual["unattained_limit"] is not None, context
                    assert actual["unattained_limit"]["elapsed_seconds"] == pytest.approx(float(limit), abs=1e-9), context
                else:
                    assert actual["unattained_limit"] is None, context
