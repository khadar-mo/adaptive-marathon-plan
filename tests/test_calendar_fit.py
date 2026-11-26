"""Fixed-session placement through the availability, template and diagnostic HTTP routes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from calendar_fit import search
from storage import Archive
from test_availability import available, submission as week_input, window
from test_health import serve_database
from test_workout_templates import block, pace, submission as template_input


WEEK = "2026-11-23"


@pytest.fixture
def client(tmp_path):
    with serve_database(tmp_path / "runs.sqlite3") as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            yield http


def post(client, path, value):
    response = client.post(path, json=value)
    assert response.status_code == 200, response.text
    return response.json()


def save_week(client, doc):
    first = doc["days"][0]["date"]
    return post(client, "/api/availability/" + first, doc)["record"]


def save_session(client, key, blocks):
    doc = template_input(request_id="template-" + key, name=key, blocks=blocks)
    saved = post(client, "/api/workout-templates/" + key, doc)["record"]
    return {"session_id": key, "template_id": key, "template_version_id": saved["version_id"]}


def fixed_block(key, kind, metres, **extra):
    return block(key, kind, metres, pace=pace(low="300", high="300"), **extra)


def request_for(week, sessions, **extra):
    return {"week": week["week"], "availability_version_id": week["version_id"], "sessions": sessions, **extra}


def timed(client, key, low, high=None):
    return save_session(client, key, [block("run", "EASY_VOLUME", 1000,
                                            pace=pace(low=str(low), high=str(low if high is None else high)))])


def monday(client, times, limit=1):
    doc = available(week_input(WEEK), 0, [window(WEEK + "T" + a, WEEK + "T" + b) for a, b in times],
                    max_sessions=limit, doubles_allowed=limit > 1)
    return save_week(client, doc)


def test_w2_five_to_three_declared_days_check_exact_unchanged_sessions(client):
    three = week_input(WEEK)
    for i, end in [(0, "08:16:16"), (2, "08:05:00"), (4, "08:20:00")]:
        day = three["days"][i]["date"]
        available(three, i, [window(day + "T06:00", day + "T" + end)])
    five = deepcopy(three)
    for i in (1, 3):
        day = five["days"][i]["date"]
        available(five, i, [window(day + "T09:00", day + "T12:00")])
    original = save_week(client, five)
    three.update(request_id="reduce-to-three", expected_revision=original["version_id"])
    revised = save_week(client, three)
    sessions = [
        save_session(client, "tempo", [fixed_block("wu", "WARM_UP", 5000),
                                       fixed_block("tempo", "WORK", 6000, protected=True, rest_after_s="75.125"),
                                       fixed_block("cd", "COOL_DOWN", 16000)]),
        save_session(client, "easy", [fixed_block("easy", "EASY_VOLUME", 25000)]),
        save_session(client, "long", [fixed_block("long", "EASY_VOLUME", 28000)])]
    request = request_for(revised, sessions)
    answer = post(client, "/api/calendar-fit", request)
    assert answer["status"] == "FEASIBLE"
    assert answer["fixed_total_m"] == 80000
    doses = {s["session_id"]: (s["total_m"], s["work_m"], s["duration_upper_seconds"]) for s in answer["sessions"]}
    assert doses == {"tempo": (27000, 6000, 8176), "easy": (25000, 0, 7500), "long": (28000, 0, 8400)}
    assert [(s["session_id"], s["start_utc"], s["end_utc"]) for s in answer["placements"]] == [
        ("tempo", "2026-11-23T06:00:00+00:00", "2026-11-23T08:16:16+00:00"),
        ("easy", "2026-11-25T06:00:00+00:00", "2026-11-25T08:05:00+00:00"),
        ("long", "2026-11-27T06:00:00+00:00", "2026-11-27T08:20:00+00:00")]
    assert post(client, "/api/calendar-fit", {**request, "sessions": list(reversed(sessions))}) == answer
    for calendar in (original, revised):
        assert client.get(f"/api/availability/{WEEK}/versions/{calendar['version_id']}").json() == calendar


@pytest.mark.parametrize(("windows", "duration", "expected"), [
    ([("08:00", "09:00"), ("08:30", "09:30")], 5400, "FEASIBLE"),
    ([("08:00", "09:00"), ("08:30", "09:30")], 5401, "IMPOSSIBLE"),
    ([("08:00", "08:30"), ("08:30", "09:00")], 3600, "FEASIBLE"),
    ([("08:00", "08:30"), ("08:30:00.000001", "09:01")], 3600, "IMPOSSIBLE"),
    ([("08:00:00.000001", "09:00")], 3600, "IMPOSSIBLE"),
    ([("08:00", "08:59:59")], 3600, "IMPOSSIBLE"),
])
def test_continuity_and_exact_boundaries(client, windows, duration, expected):
    q = request_for(monday(client, windows), [timed(client, "fixed", duration)])
    answer = post(client, "/api/calendar-fit", q)
    assert answer["status"] == expected
    if expected == "FEASIBLE":
        assert answer["placements"][0]["source_window_ids"] == [WEEK + ":0", WEEK + ":1"]
    else:
        assert answer["placements"] == []
        assert answer["reason"] in {"TOTAL_FREE_TIME", "NO_CONTINUOUS_SLOT"}
        assert answer["proof"]["required_whole_seconds"] == duration


@pytest.mark.parametrize(("limit", "kind", "expected"), [
    (1, "RUNNING", "IMPOSSIBLE"), (1, "NON_RUNNING", "FEASIBLE"), (2, "RUNNING", "FEASIBLE")])
def test_prior_running_starts_use_day_count_even_outside_window(client, limit, kind, expected):
    row = monday(client, [("08:00", "09:00")], limit=limit)
    busy = [{"id": "completed", "kind": kind, "start_utc": WEEK + "T05:00Z", "end_utc": WEEK + "T06:00Z"},
            {"id": "meeting", "kind": "NON_RUNNING", "start_utc": WEEK + "T08:30Z", "end_utc": WEEK + "T08:40Z"}]
    q = request_for(row, [timed(client, "remaining", 1200)], occupied=busy, earliest_start_utc=WEEK + "T08:20Z")
    answer = post(client, "/api/calendar-fit", q)
    assert answer["status"] == expected
    assert answer["start_day_limits"][0]["occupied_running_starts"] == int(kind == "RUNNING")
    assert [slot["start_utc"] for slot in answer["placements"]] == ([WEEK + "T08:40:00+00:00"] if expected == "FEASIBLE" else [])
    assert answer["sessions"][0]["total_m"] == 1000


def test_overnight_marriage_of_continuous_time_and_actual_start_date(client):
    doc = available(week_input(WEEK), 0, [window(WEEK + "T23:30", "2026-11-24T02:00")])
    row = save_week(client, doc)
    fixed = timed(client, "overnight", 3600)
    q = request_for(row, [fixed])
    result = post(client, "/api/calendar-fit", q)
    assert result["placements"][0]["end_utc"] == "2026-11-24T00:30:00+00:00"
    assert post(client, "/api/calendar-fit", {**q, "earliest_start_utc": "2026-11-24T00:00Z"})["status"] == "IMPOSSIBLE"
    available(doc, 1, [window("2026-11-24T07:00", "2026-11-24T07:01")])
    doc.update(request_id="allow-tuesday-start", expected_revision=row["version_id"])
    row = save_week(client, doc)
    # Occupied time started Monday but extends into Tuesday. Tuesday's start remains available.
    busy = [{"id":"last-night", "kind":"RUNNING", "start_utc":WEEK+"T23:30Z", "end_utc":"2026-11-24T00:20Z"}]
    result = post(client, "/api/calendar-fit", request_for(row, [fixed], occupied=busy))
    assert result["placements"][0]["start_utc"] == "2026-11-24T00:20:00+00:00"
    assert [r["occupied_running_starts"] for r in result["start_day_limits"][:2]] == [1, 0]


@pytest.mark.parametrize(("week", "bounds", "folds", "utc_start"), [
    ("2026-03-23", ("2026-03-29T00:30", "2026-03-29T02:30"), (None, None), "2026-03-29T00:30:00+00:00"),
    ("2026-10-19", ("2026-10-25T01:30", "2026-10-25T01:30"), (0, 1), "2026-10-25T00:30:00+00:00")])
def test_dst_gap_and_fold_use_elapsed_seconds(client, week, bounds, folds, utc_start):
    w = window(*bounds, start_fold=folds[0], end_fold=folds[1])
    row = save_week(client, available(week_input(week), 6, [w]))
    result = post(client, "/api/calendar-fit", request_for(row, [timed(client, "hour", 3600)]))
    assert result["placements"][0]["start_utc"] == utc_start
    assert result["proof"]["available_whole_seconds"] == 3600
    result = post(client, "/api/calendar-fit", request_for(row, [timed(client, "extra", 3601)]))
    assert (result["status"], result["reason"]) == ("IMPOSSIBLE", "TOTAL_FREE_TIME")


def test_range_missing_revision_missing_pace_and_partial_calendar(client):
    row = monday(client, [("08:00", "08:00:10")])
    uncertain = timed(client, "range", "7.1", "10.001")
    q = request_for(row, [uncertain])
    answer = post(client, "/api/calendar-fit", q)
    assert (answer["status"], answer["reason"], answer["conservative_fit"]["status"]) == (
        "FEASIBILITY_UNCERTAIN", "PACE_RANGE_STRADDLES_CALENDAR", "UNSATISFIED_IN_DECLARED_WINDOWS")
    assert not answer["placements"]
    fixed = timed(client, "too-long", 11, 20)
    result = post(client, "/api/calendar-fit", request_for(row, [fixed]))
    assert result["status"] == "IMPOSSIBLE" and result["proof"]["duration_basis"] == "FASTEST_SUPPORTED"
    unpaced = save_session(client, "no-pace", [block("work", "WORK", 1000)])
    result = post(client, "/api/calendar-fit", request_for(row, [unpaced]))
    assert result["missing"] == [{"session_id": "no-pace", "kind": "MISSING_PACE_EVIDENCE", "block_ids": ["work"]}]
    missing = {**uncertain, "template_version_id": "0"*64}
    result = post(client, "/api/calendar-fit", request_for(row, [missing], availability_version_id="0"*64))
    assert {m["kind"] for m in result["missing"]} == {"TEMPLATE_VERSION_UNAVAILABLE", "AVAILABILITY_VERSION_UNAVAILABLE"}
    doc = week_input(WEEK)
    doc.update(request_id="partial", expected_revision=row["version_id"])
    doc["days"][3].update(state="UNKNOWN", max_sessions=None)
    partial = save_week(client, doc)
    q = request_for(partial, [fixed])
    assert post(client, "/api/calendar-fit", q)["reason"] == "UNKNOWN_AVAILABILITY"
    late = {**q, "earliest_start_utc": "2026-12-01T00:00:00Z"}
    ans = post(client, "/api/calendar-fit", late)
    assert ans["status"] == "IMPOSSIBLE" and ans["proof_calendar"] == "OPTIMISTIC_UNKNOWN_DAYS"


def test_budget_exhaustion_and_exhaustive_nonpacking_proof(client):
    row = monday(client, [("08:00", "08:00:05"), ("09:00", "09:00:05")], limit=4)
    a, b = timed(client, "four", 4), timed(client, "two", 2)
    three = [a, {**a, "session_id": "four-copy"}, b]
    request = request_for(row, three)
    limited = post(client, "/api/calendar-fit", {**request, "search_budget": 1})
    assert (limited["status"], limited["reason"], limited["search_nodes"]) == ("FEASIBILITY_UNCERTAIN", "SEARCH_BUDGET", 1)
    full = post(client, "/api/calendar-fit", request)
    assert (full["status"], full["reason"]) == ("IMPOSSIBLE", "EXHAUSTIVE_ORDER_SEARCH")
    assert full["proof"]["available_whole_seconds"] == full["proof"]["required_whole_seconds"] == 10
    assert not full["placements"]


def test_missing_pace_cannot_mask_impossible_calendar_structure(client):
    fixed = save_session(client, "unpaced", [block("run", "EASY_VOLUME", 1000)])
    closed = save_week(client, week_input(WEEK))
    q = request_for(closed, [fixed])
    result = post(client, "/api/calendar-fit", q)
    assert result["status"] == "IMPOSSIBLE" and result["reason"] == "TOTAL_FREE_TIME"
    assert result["proof"]["available_whole_seconds"] == result["proof"]["maximum_starts"] == 0
    assert result["proof"]["duration_basis"] == "STRUCTURAL_MINIMUM"
    assert result["missing"] == [{"session_id":"unpaced", "kind":"MISSING_PACE_EVIDENCE", "block_ids":["run"]}]
    unknown_cap = post(client, "/api/calendar-fit", {**q, "preferences_version_id":"0"*64})
    assert unknown_cap["status"] == "IMPOSSIBLE"
    assert {r["kind"] for r in unknown_cap["missing"]} == {"MISSING_PACE_EVIDENCE", "PREFERENCES_VERSION_UNAVAILABLE"}
    doc = week_input(WEEK, request_id="one-slot", expected_revision=closed["version_id"])
    available(doc, 0, [window(WEEK+"T08:00", WEEK+"T09:00")])
    row = save_week(client, doc)
    q = request_for(row, [fixed])
    assert post(client, "/api/calendar-fit", q)["status"] == "FEASIBILITY_UNCERTAIN"
    rested = save_session(client, "rests", [block("run", "EASY_VOLUME", 1000, rest_after_s="3600")])
    result = post(client, "/api/calendar-fit", request_for(row, [rested]))
    assert result["status"] == "IMPOSSIBLE" and result["proof"]["required_whole_seconds"] == 3601
    q["occupied"] = [{"id":"early-run", "kind":"RUNNING", "start_utc":WEEK+"T06:00Z", "end_utc":WEEK+"T07:00Z"}]
    result = post(client, "/api/calendar-fit", q)
    assert result["status"] == "IMPOSSIBLE" and result["reason"] == "START_DAY_LIMITS" and result["missing"]


def test_pace_and_unknown_days_are_independently_explained(client):
    doc = available(week_input(WEEK), 0, [window(WEEK+"T08:00", WEEK+"T08:00:10")])
    sunday = doc["days"][6]["date"]
    doc["days"][6].update(state="UNKNOWN", max_sessions=None)
    row = save_week(client, doc)
    occupied = [{"id":"away", "kind":"NON_RUNNING", "start_utc":sunday+"T00:00Z", "end_utc":"2026-11-30T00:00Z"}]
    q = request_for(row, [timed(client, "nine-eleven", 9, 11)], occupied=occupied)
    ans = post(client, "/api/calendar-fit", q)
    assert (ans["status"], ans["reason"], ans["unknown_dates"]) == ("FEASIBILITY_UNCERTAIN", "PACE_RANGE_STRADDLES_CALENDAR", [sunday])
    assert ans["conservative_fit"]["status"] == "UNSATISFIED_IN_DECLARED_WINDOWS" and not ans["placements"]
    # With no Monday fit even at the fastest pace, Sunday's declaration can
    # matter precisely when its time is not already occupied.
    q["sessions"] = [timed(client, "eleven-twelve", 11, 12)]
    assert post(client, "/api/calendar-fit", q)["status"] == "IMPOSSIBLE"
    assert post(client, "/api/calendar-fit", {**q, "occupied":[]})["reason"] == "UNKNOWN_AVAILABILITY"


def test_immutable_template_and_preference_replay(client):
    row = monday(client, [("08:00", "09:00")], limit=2)
    fixed = save_session(client, "fixed", [fixed_block("easy", "EASY_VOLUME", 1000, min_m=900, max_m=1100)])
    q = request_for(row, [fixed, {**fixed, "session_id": "repeat", "distances_m": {"easy": 1001}}])
    original = post(client, "/api/calendar-fit", q)
    assert original["status"] == "FEASIBLE" and [s["total_m"] for s in original["sessions"]] == [1000, 1001]
    path = "/api/workout-templates/fixed"
    first = client.get(path).json()["record"]
    edit = template_input(request_id="new-version", expected_revision=first["version_id"], blocks=[fixed_block("new", "WORK", 2000)])
    second = post(client, path, edit)["record"]
    pref = client.get("/api/runner-settings/preferences").json()["current"]
    data = {"request_id":"new-cap", "expected_revision":pref["version_id"], "actor":"Fixture", "reason":"One start",
            "action":"SET_PREFERENCES", "max_sessions_per_day":1, "strength_exercise_choices":None, "special_constraints":None}
    cap = post(client, "/api/runner-settings/preferences", data)["record"]
    assert post(client, "/api/calendar-fit", q) == original
    result = post(client, "/api/calendar-fit", {**q, "preferences_version_id": cap["version_id"]})
    assert result["status"] == "IMPOSSIBLE" and result["reason"] == "START_DAY_LIMITS"
    assert client.get(path).json()["record"] == second
    assert client.get(path + "/versions/" + first["version_id"]).json() == first
    for bad in ({**q, "sessions": [fixed, fixed]}, {**q, "sessions": [{**fixed, "distances_m":{"easy": 1101}}]},
                {**q, "earliest_start_utc": "2026-11-23T08:00:00.0000001Z"},
                {**q, "earliest_start_utc": "2026-11-23T08:00:00"}, {**q, "search_budget": True},
                {**q, "occupied":[{"id":"x","kind":"RUNNING","start_utc":WEEK+"T09:00Z","end_utc":WEEK+"T08:00Z"}]}):
        assert client.post("/api/calendar-fit", json=bad).status_code == 422


def test_genuine_older_database_and_archived_actual_are_read_only(tmp_path):
    fixture = json.loads(gzip.decompress((Path(__file__).parent / "fixtures" / "fueling-g0-4a0630f.json.gz").read_bytes()))
    path = tmp_path / "runs.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(fixture["sql"])
    archive = Archive(tmp_path / "runs-archive")
    raw = bytes.fromhex(fixture["raw_hex"])
    sha = archive.put(raw)
    with serve_database(path) as port, httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
        old = http.get("/api/activities/1").json()
        before = path.read_bytes()
        assert http.get("/calendar-fit", params={"week": WEEK}).status_code == 200
        result = post(http, "/api/calendar-fit", {"week":WEEK, "availability_version_id":"0"*64, "sessions":[]})
        assert result["status"] == "FEASIBILITY_UNCERTAIN" and path.read_bytes() == before
        row = monday(http, [("08:00", "09:00")])
        q = request_for(row, [timed(http, "hour", 3600)])
        saved = path.read_bytes()
        assert post(http, "/api/calendar-fit", q)["status"] == "FEASIBLE"
        assert path.read_bytes() == saved and http.get("/api/activities/1").json() == old
        assert archive.read(sha) == raw


def test_exact_search_matches_all_starts_on_small_calendars():
    # Independent oracle assigns jobs in input order to every possible second,
    # including earlier starts after later jobs have already been assigned.
    start = datetime(2026, 11, 23, 23, 59, 57, tzinfo=timezone.utc)
    ticks = [start + timedelta(seconds=i) for i in range(7)]
    for mask in range(64):
        free = []
        for i in range(6):
            if mask & (1 << i):
                if free and free[-1][1] == ticks[i]:
                    free[-1] = free[-1][0], ticks[i + 1]
                else:
                    free.append((ticks[i], ticks[i + 1]))
        for limits in ((1,1), (2,1), (0,2)):
            days = [(ticks[0], ticks[3], limits[0]), (ticks[3], ticks[6], limits[1])]
            for lengths in ((1,), (2,2), (3,2,1), (1,2,1)):
                def place(i, available_bits, capacity):
                    if i == len(lengths):
                        return True
                    for at in range(7 - lengths[i]):
                        day = int(at >= 3)
                        bits = ((1 << lengths[i]) - 1) << at
                        if available_bits & bits != bits or not capacity[day]:
                            continue
                        updated = list(capacity)
                        updated[day] -= 1
                        if place(i + 1, available_bits ^ bits, updated):
                            return True
                    return False

                fixed = [{"session_id": str(i), "duration_upper_seconds": n} for i, n in enumerate(lengths)]
                slots, reason, _, _ = search(fixed, free, days, 10000)
                assert reason != "SEARCH_BUDGET"
                assert (slots is not None) == place(0, mask, limits), (mask, limits, lengths)
