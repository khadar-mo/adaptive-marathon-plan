"""Place fixed running sessions in immutable London calendar windows."""
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, Field, model_validator

import availability
import workout_templates as templates
from workload_inputs import utc


POLICY = "fixed-calendar-fit-v1"
SECOND = timedelta(seconds=1)
LONDON = ZoneInfo(availability.ZONE)


def absolute(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)", value):
        raise ValueError("Use an ISO timestamp with a UTC offset and at most six fractional second digits.")
    return utc(value)


Instant = Annotated[str, AfterValidator(absolute)]


class Session(availability.Input):
    session_id: templates.Key
    template_id: templates.Key
    template_version_id: templates.Hash
    distances_m: dict[templates.Key, templates.Metres] = Field(default_factory=dict, max_length=256)


class Occupied(availability.Input):
    id: templates.Key
    kind: Literal["RUNNING", "NON_RUNNING"]
    start_utc: Instant
    end_utc: Instant

    @model_validator(mode="after")
    def positive(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("Occupied time needs a positive elapsed duration.")
        return self


class Check(availability.Input):
    week: str
    availability_version_id: templates.Hash
    sessions: list[Session] = Field(max_length=128)
    earliest_start_utc: Instant | None = None
    occupied: list[Occupied] = Field(default_factory=list, max_length=256)
    search_budget: int = Field(default=10000, ge=0, le=20000)

    @model_validator(mode="after")
    def identifiers(self):
        availability.week_bounds(self.week)
        if len({s.session_id for s in self.sessions}) != len(self.sessions):
            raise ValueError("Each fixed session needs a distinct session ID.")
        if len({o.id for o in self.occupied}) != len(self.occupied):
            raise ValueError("Each occupied interval needs a distinct ID.")
        return self


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(result[-1][1], end)
        elif start < end:
            result.append((start, end))
    return result


def free_intervals(windows, busy, earliest):
    free = []
    for start, end in merge(windows):
        cursor = max(start, earliest)
        for a, b in busy:
            if a >= end or cursor >= end:
                break
            if b <= cursor:
                continue
            if cursor < a:
                free.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < end:
            free.append((cursor, end))
    return free


def search(sessions, free, days, budget):
    # Each permitted start day limits the start, while its continuous interval
    # may carry the same session across local midnight.
    starts = [(max(a, day_start), min(b, day_end), b, day)
              for a, b in free for day, (day_start, day_end, _) in enumerate(days)
              if a < day_end and b > day_start]
    remaining = [capacity for _, _, capacity in days]
    seconds = {s["session_id"]: s["duration_upper_seconds"] for s in sessions}
    order = sorted(seconds, key=lambda key: (-seconds[key], key))
    capacity = sum((b - a for a, b in free), timedelta()) // SECOND
    if sum(seconds.values()) > capacity:
        return None, "TOTAL_FREE_TIME", 0
    if len(order) > sum(remaining):
        return None, "START_DAY_LIMITS", 0
    visited = 0

    def first_slot(duration, cursor):
        for start, day_end, end, day in starts:
            when = max(start, cursor) if cursor is not None else start
            if remaining[day] and when < day_end and duration <= (end - when) // SECOND:
                return when, when + duration * SECOND, day
        return None

    if any(first_slot(duration, None) is None for duration in set(seconds.values())):
        return None, "NO_CONTINUOUS_SLOT", 0

    def visit(left, cursor):
        nonlocal visited
        if not left:
            return [], "CONCRETE_INTERVALS"
        tried = set()
        for key in left:
            duration = seconds[key]
            if duration in tried:
                continue
            tried.add(duration)
            if visited >= budget:
                return None, "SEARCH_BUDGET"
            visited += 1
            slot = first_slot(duration, cursor)
            if slot is None:
                continue
            a, b, day = slot
            remaining[day] -= 1
            rest, reason = visit([other for other in left if other != key], b)
            remaining[day] += 1
            if rest is not None:
                return [(key, a, b), *rest], reason
            if reason == "SEARCH_BUDGET":
                return None, reason
        return None, "EXHAUSTIVE_ORDER_SEARCH"

    placement, reason = visit(order, None)
    return placement, reason, visited


def check_calendar(db, query):
    """A read-only check against caller-selected immutable revisions in one transaction."""
    first, _, week_start, week_end = availability.week_bounds(query.week)
    earliest = max(week_start, datetime.fromisoformat(query.earliest_start_utc)) if query.earliest_start_utc else week_start
    occupied = sorted(query.occupied, key=lambda item: (item.start_utc, item.end_utc, item.id))
    busy = merge((datetime.fromisoformat(o.start_utc), datetime.fromisoformat(o.end_utc)) for o in occupied)
    used = Counter(datetime.fromisoformat(o.start_utc).astimezone(LONDON).date().isoformat()
                   for o in occupied if o.kind == "RUNNING")
    calendar = availability.read_version(db, query.week, query.availability_version_id)
    sessions, missing = [], []
    for fixed in sorted(query.sessions, key=lambda item: item.session_id):
        row = fixed.model_dump()
        record = templates.read_version(db, fixed.template_id, version_id=fixed.template_version_id)
        if record is None:
            missing.append({"session_id": fixed.session_id, "kind": "TEMPLATE_VERSION_UNAVAILABLE"})
        else:
            dose = templates.account(record["blocks"], fixed.distances_m)
            if not dose["total_m"]:
                raise ValueError("A fixed running session needs positive running distance.")
            high = dose["duration_upper_s"]
            row.update(name=record["name"], total_m=dose["total_m"], work_m=dose["work_m"],
                       duration_upper_s=high, rest_s=dose["rest_s"], blocks=dose["blocks"],
                       duration_upper_seconds=int(Decimal(high).to_integral_value(rounding=ROUND_CEILING)) if high is not None else None)
            if high is None:
                missing.append({"session_id": fixed.session_id, "kind": "MISSING_PACE_EVIDENCE",
                                "block_ids": dose["missing_pace_block_ids"]})
        sessions.append(row)
    result = {"status": "FEASIBILITY_UNCERTAIN", "policy_version": POLICY, "week": query.week,
              "availability_version_id": query.availability_version_id, "sessions": sessions, "placements": [],
              "assumptions": {"earliest_start_utc": query.earliest_start_utc, "effective_start_utc": earliest.isoformat(),
                              "occupied": [o.model_dump() for o in occupied], "search_budget": query.search_budget},
              "search_nodes": 0}
    if calendar is None:
        missing.append({"kind": "AVAILABILITY_VERSION_UNAVAILABLE"})
    if missing:
        return {**result, "reason": "MISSING_INPUT", "missing": missing}
    windows = [(datetime.fromisoformat(w["start_utc"]), datetime.fromisoformat(w["end_utc"])) for w in calendar["union_windows"]]
    midnights = [datetime.combine(first + timedelta(days=i), time(), LONDON).astimezone(timezone.utc) for i in range(8)]
    days = []
    unknown = []
    for i, d in enumerate(calendar["days"]):
        if d["state"] == "UNKNOWN":
            unknown.append(i)
        cap = d["max_sessions"] or 0
        if not d["doubles_allowed"]:
            cap = min(cap, 1)
        days.append((midnights[i], midnights[i + 1], max(0, cap - used[d["date"]])))
    free = free_intervals(windows, busy, earliest)
    result["free_intervals"] = [{"start_utc": a.isoformat(), "end_utc": b.isoformat()} for a, b in free]
    result["start_day_limits"] = [{"date": d["date"], "state": d["state"], "occupied_running_starts": used[d["date"]],
                                   "remaining_starts": day[2] if d["state"] != "UNKNOWN" else None}
                                  for d, day in zip(calendar["days"], days)]
    placement, reason, nodes = search(sessions, free, days, query.search_budget)
    result.update(reason=reason, search_nodes=nodes)
    if placement is not None:
        sources = [(w["id"], datetime.fromisoformat(w["start"]["utc"]), datetime.fromisoformat(w["end"]["utc"]))
                   for d in calendar["days"] for w in d["windows"]]
        fixed = {s["session_id"]: s for s in sessions}
        result["status"] = "FEASIBLE"
        result["placements"] = [{"session_id": key, "template_id": fixed[key]["template_id"],
                                 "template_version_id": fixed[key]["template_version_id"],
                                 "start_utc": a.isoformat(), "end_utc": b.isoformat(),
                                 "start_local_date": a.astimezone(LONDON).date().isoformat(),
                                 "source_window_ids": [sid for sid, start, end in sources if start < b and end > a]}
                                for key, a, b in placement]
    elif reason != "SEARCH_BUDGET" and unknown:
        # Unknown windows may span later dates. Give an optimistic calendar
        # every instant from the first unknown day and the schema maximum
        # of ten starts on each unknown day. Only its failure can prove impossibility.
        optimistic = list(days)
        for i in unknown:
            a, b, _ = days[i]
            optimistic[i] = a, b, max(0, 10 - used[calendar["days"][i]["date"]])
        possible = free_intervals([*windows, (midnights[unknown[0]], week_end)], busy, earliest)
        maybe, proof, more = search(sessions, possible, optimistic, query.search_budget - nodes)
        result["search_nodes"] += more
        result["unknown_dates"] = [calendar["days"][i]["date"] for i in unknown]
        if maybe is None and proof != "SEARCH_BUDGET":
            result.update(status="IMPOSSIBLE", reason=proof, proof_calendar="OPTIMISTIC_UNKNOWN_DAYS")
        else:
            result["reason"] = "UNKNOWN_AVAILABILITY" if maybe is not None else "SEARCH_BUDGET"
    elif reason != "SEARCH_BUDGET":
        result.update(status="IMPOSSIBLE", proof_calendar="DECLARED_WINDOWS")
    return result
