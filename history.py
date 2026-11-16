"""Calendar periods and measurements shared by the local HTML and JSON views."""
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
import json

import garmin_coverage as coverage
from garmin_import import RUN_TYPES, number


MEASURES = ("distance_m", "timer_seconds", "elapsed_seconds", "moving_seconds")
UNAVAILABLE = "Not available"


def calendar_period(day, unit):
    if unit == "week":
        monday = day.toordinal() - day.weekday()
        return date.fromordinal(monday), date.fromordinal(min(monday + 6, date.max.toordinal()))
    return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])


def duration(seconds):
    if seconds is None:
        return UNAVAILABLE
    whole = int(Decimal(str(seconds)).to_integral_value(rounding=ROUND_HALF_UP))
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02}:{second:02}" if hours else f"{minute}:{second:02}"


def kilometres(metres):
    if metres is None:
        return UNAVAILABLE
    with localcontext(rounding=ROUND_HALF_UP):
        return f"{Decimal(str(metres)) / 1000:.2f} km"


def pace(seconds, metres):
    if not seconds or not metres:
        return None
    value = Decimal(str(seconds)) * 1000 / Decimal(str(metres))
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def measurements(row):
    raw = {key: number(row[key]) for key in MEASURES}
    display = {key: duration(raw[key]) for key in MEASURES if key != "distance_m"}
    display["distance_m"] = kilometres(raw["distance_m"])
    for kind in ("timer", "moving"):
        key = kind + "_pace_seconds_per_km"
        value = pace(raw[kind + "_seconds"], raw["distance_m"])
        raw[key] = value
        if value is None:
            display[key] = UNAVAILABLE
        else:
            minutes, seconds = divmod(value, 60)
            display[key] = f"{minutes}:{seconds:02}/km"
    return {**raw, "display": display}


def summarize(rows):
    totals = {}
    for key in MEASURES:
        known = [Decimal(str(row[key])) for row in rows if row[key] is not None]
        total = sum(known, Decimal(0))
        # Preserve the unrounded subtotal. Only the display string uses display precision.
        value = int(total) if total == total.to_integral_value() else float(total)
        display = kilometres(total) if key == "distance_m" else duration(total)
        totals[key] = {"value": value, "missing_count": len(rows) - len(known),
                       "display": UNAVAILABLE if rows and not known else display}
    longest = max((r["distance_m"] for r in rows if r["distance_m"] is not None), default=None)
    return {"run_count": len(rows), "totals": totals, "longest_distance_m": longest,
            "longest_run_ids": [r["id"] for r in rows if longest is not None and r["distance_m"] == longest]}


def period_coverage(first, last, verified):
    window = [coverage.bounds(first, last)]
    gaps = coverage.difference(window, verified)
    def ranges(intervals):
        return [{"from": coverage.iso(a), "to": coverage.iso(b)} for a, b in intervals]
    return {"complete": not gaps, "checked_ranges": ranges(coverage.difference(window, gaps)), "gaps": ranges(gaps)}


def grouped(rows, first, last, unit):
    groups = defaultdict(list)
    for row in rows:
        groups[calendar_period(date.fromisoformat(row["local_date"]), unit)].append(row)
    return [{"from": max(first, start.isoformat()), "to": min(last, end.isoformat()),
             "activity_ids": [r["id"] for r in members], **summarize(members)}
            for (start, end), members in sorted(groups.items())]


def read(store, first, last):
    """Read rows, identity links and coverage from one SQLite snapshot."""
    rows, undated, verified = [], [], []
    with store.history() as db:
        if db is not None:
            verified, _ = coverage.coverage_ranges(db)
            # A corrected activity can need a retry outside its original scan.
            required = [coverage.item_range(item) for item in coverage.outstanding_items(db) if item["status"] != "saved"]
            verified = coverage.difference(verified, coverage.merged(required))
            aliases = defaultdict(list)
            for gid, owner in db.execute("SELECT garmin_id, activity_id FROM garmin_aliases ORDER BY garmin_id"):
                aliases[owner].append(gid)
            activities = db.execute("""SELECT id, garmin_id, name, sport, start_local, local_date, start_utc,
                        distance_m, timer_seconds, elapsed_seconds, moving_seconds, metric_sources
                    FROM activities WHERE local_date BETWEEN ? AND ? OR local_date IS NULL
                    ORDER BY start_utc IS NULL, start_utc DESC, id DESC""", (first, last))
            for activity in activities:
                row = dict(activity)
                # Every admitted FIT session is running. Garmin activities can later change sport.
                if row["garmin_id"] and row["sport"] not in RUN_TYPES:
                    continue
                row["metric_sources"] = json.loads(row["metric_sources"])
                row["garmin_ids"] = list(dict.fromkeys(g for g in [row["garmin_id"], *aliases[row["id"]]] if g))
                row.update(measurements(row))
                (rows if row["local_date"] else undated).append(row)
    return {"from": first, "to": last, **summarize(rows), "coverage": period_coverage(first, last, verified),
            "activities": rows, "undated_activities": undated,
            "weeks": grouped(rows, first, last, "week"), "months": grouped(rows, first, last, "month")}
