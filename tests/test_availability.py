"""Public calendar contracts use independently specified clock and interval results."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
import json
import sqlite3

import pytest

import availability
from storage import Store
from test_imports import history, server
from test_fit_import import upload
from test_canonical import trace_fit

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def submission(week="2026-07-06", **kw):
    first = date.fromisoformat(week)
    days = [{"date": (first + timedelta(days=i)).isoformat(), "state": "UNAVAILABLE",
             "max_sessions": 0, "doubles_allowed": False, "windows": []} for i in range(7)]
    return {"request_id": "calendar-1", "expected_revision": None, "actor": "Synthetic runner", "days": days, **kw}


def window(start, end, *, start_fold=None, end_fold=None, start_offset=None, end_offset=None):
    return {"start": {"local": start, "fold": start_fold, "offset": start_offset},
            "end": {"local": end, "fold": end_fold, "offset": end_offset}}


def available(data, index, windows, **kw):
    data["days"][index].update({"state": "AVAILABLE", "max_sessions": 1, "windows": windows, **kw})
    return data


async def save(client, week, value, status=200):
    r = await client.post("/api/availability/" + week, json=value)
    assert r.status_code == status, r.text
    return r.json()


async def test_absent_partial_empty_and_no_passive_save(history, tmp_path):
    async with server() as client:
        original = await client.get("/api/availability/2026-07-06")
        assert original.json()["submission_state"] == "NOT_ENTERED"
        assert original.json()["state"] == "UNKNOWN" and original.json()["current"] is None
        assert (await client.get("/availability?week=2026-07-06")).status_code == 200
        assert not (tmp_path / "runs.sqlite3").exists()
        data = submission()
        data["days"][0].update(state="UNKNOWN", max_sessions=None)
        partial = (await save(client, "2026-07-06", data))["record"]
        assert partial["state"] == "PARTIAL" and partial["union_elapsed_seconds"] == 0
        empty = (await save(client, "2026-07-06", submission(request_id="calendar-2", expected_revision=partial["version_id"])))["record"]
        assert empty["state"] == "EMPTY" and empty["submission_state"] == "RECORDED"
        assert empty["planner_action"] == "CALENDAR_REVISION_ONLY"
        before = (tmp_path / "runs.sqlite3").read_bytes()
        assert "explicitly empty week" in (await client.get("/availability?week=2026-07-06")).text
        assert (await client.get("/api/availability/2026-07-06")).json()["current"] == empty
        assert (tmp_path / "runs.sqlite3").read_bytes() == before


async def test_overlap_union_retains_originals_and_explicit_doubles(history):
    data = available(submission(), 1, [window("2026-07-07T10:00", "2026-07-07T11:00"),
                                       window("2026-07-07T10:30", "2026-07-07T12:00"),
                                       window("2026-07-07T12:00", "2026-07-07T12:30")],
                     max_sessions=2, doubles_allowed=True)
    async with server() as client:
        row = (await save(client, "2026-07-06", data))["record"]
        assert row["union_windows"] == [{"start_utc": "2026-07-07T09:00:00+00:00", "end_utc": "2026-07-07T11:30:00+00:00",
                                          "source_window_ids": ["2026-07-07:0", "2026-07-07:1", "2026-07-07:2"], "elapsed_seconds": 9000.0}]
        assert len(row["days"][1]["windows"]) == 3 and row["days"][1]["max_sessions"] == 2
        assert row["submitted"]["days"] == data["days"]
        assert row["daily_limit_basis"] == "SESSION_START_LOCAL_DATE"


@pytest.mark.parametrize(("week", "start", "end", "sf", "ef", "so", "eo", "expected_start", "expected_end", "seconds", "week_seconds"), [
    ("2026-03-23", "2026-03-29T00:30", "2026-03-29T02:30", None, None, None, None,
     "2026-03-29T00:30:00+00:00", "2026-03-29T01:30:00+00:00", 3600, 167 * 3600),
    ("2026-10-19", "2026-10-25T01:30", "2026-10-25T01:30", 0, 1, None, None,
     "2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00", 3600, 169 * 3600),
    ("2026-10-19", "2026-10-25T01:45", "2026-10-25T01:15", None, None, "+01:00", "+00:00",
     "2026-10-25T00:45:00+00:00", "2026-10-25T01:15:00+00:00", 1800, 169 * 3600),
])
async def test_dst_utc_bounds_preserved_offsets_and_folds(history, week, start, end, sf, ef, so, eo, expected_start, expected_end, seconds, week_seconds):
    from datetime import datetime
    data = available(submission(week), 6, [window(start, end, start_fold=sf, end_fold=ef, start_offset=so, end_offset=eo)])
    async with server() as client:
        row = (await save(client, week, data))["record"]
        interval, = row["union_windows"]
        assert (interval["start_utc"], interval["end_utc"], interval["elapsed_seconds"]) == (expected_start, expected_end, seconds)
        assert (datetime.fromisoformat(row["week_end_utc"]) - datetime.fromisoformat(row["week_start_utc"])).total_seconds() == week_seconds
        assert row["days"][6]["windows"][0]["start"]["submitted"] == data["days"][6]["windows"][0]["start"]
        assert row["days"][6]["windows"][0]["end"]["submitted"] == data["days"][6]["windows"][0]["end"]


@pytest.mark.parametrize(("week", "item", "message"), [
    ("2026-03-23", window("2026-03-29T01:30", "2026-03-29T03:00"), "does not exist"),
    ("2026-10-19", window("2026-10-25T01:30", "2026-10-25T03:00"), "occurs twice"),
    ("2026-10-19", window("2026-10-25T01:30", "2026-10-25T03:00", start_fold=0, start_offset="+00:00"), "does not match"),
    ("2026-07-06", window("2026-07-12T10:00", "2026-07-12T11:00", start_fold=1), "does not match"),
    ("2026-07-06", window("2026-07-12T10:00", "2026-07-12T11:00", start_offset="+00:00"), "does not match"),
    ("2026-07-06", window("2026-07-12T23:00", "2026-07-13T00:01"), "inside the selected"),
    ("2026-07-06", window("2026-07-12T10:00", "2026-07-12T09:00"), "positive duration"),
])
async def test_invalid_clock_and_interval_does_not_write(history, tmp_path, week, item, message):
    async with server() as client:
        result = await save(client, week, available(submission(week), 6, [item]), 422)
        assert message in result["detail"]
        assert not (tmp_path / "runs.sqlite3").exists()


async def test_overnight_window_and_exact_week_end(history):
    data = available(submission(), 5, [window("2026-07-11T23:30", "2026-07-12T01:30")])
    available(data, 6, [window("2026-07-12T23:00", "2026-07-13T00:00")])
    async with server() as client:
        row = (await save(client, "2026-07-06", data))["record"]
        assert row["union_elapsed_seconds"] == 10800
        assert row["union_windows"][-1]["end_utc"] == row["week_end_utc"]
        assert row["days"][5]["windows"][0]["start"]["local"][:10] == "2026-07-11"


async def test_idempotency_stale_edits_history_restart_and_scope(history, tmp_path, monkeypatch):
    clock = ["2026-11-26T01:00:00.123456+00:00"]
    monkeypatch.setattr(availability, "now", lambda: clock[0])
    data = submission(actor='<script>bad()</script>')
    async with server() as client:
        first = (await save(client, "2026-07-06", data))["record"]
        clock[0] = "2026-11-26T02:00:00.654321+00:00"
        next_data = available(submission(request_id="calendar-2", expected_revision=first["version_id"]), 0, [window("2026-07-06T18:00", "2026-07-06T19:00")])
        second = (await save(client, "2026-07-06", next_data))["record"]
        retry = await save(client, "2026-07-06", data)
        assert retry["replayed"] and retry["record"] == first
        assert second["previous_revision_id"] == first["version_id"] and second["revision"] == 2
        assert first["received_at"] < second["known_at"]
        await save(client, "2026-07-06", {**next_data, "reason": "changed same id"}, 409)
        await save(client, "2026-07-06", {**data, "request_id": "stale"}, 409)
        assert (await client.get(f'/api/availability/2026-07-06/versions/{first["version_id"]}')).json() == first
        assert (await client.get(f'/api/availability/2026-07-13/versions/{first["version_id"]}')).status_code == 404
        html = (await client.get(f'/availability?week=2026-07-06&version={first["version_id"]}')).text
        assert 'id="availability-editor"' not in html and '&lt;script&gt;bad()&lt;/script&gt;' in html
        assert '<script>bad()</script>' not in html
    # Read the same immutable payload through a new Store, without resolving a head.
    with Store(tmp_path / "runs.sqlite3").history() as db:
        assert availability.read_version(db, "2026-07-06", first["version_id"]) == first
        assert availability.read_current(db, "2026-07-06") == second


@pytest.mark.parametrize("mutation", ["missing_day", "duplicate_day", "wrong_day", "boolean_cap", "fractional_cap", "doubles_without_cap", "cap_without_doubles", "unknown_with_window", "blank_actor", "spoofed_time"])
async def test_strict_input(history, mutation):
    data = available(submission(), 0, [window("2026-07-06T10:00", "2026-07-06T11:00")])
    if mutation == "missing_day": data["days"].pop()
    if mutation == "duplicate_day": data["days"][6] = deepcopy(data["days"][0])
    if mutation == "wrong_day": data["days"][0]["windows"][0]["start"]["local"] = "2026-07-07T10:00"
    if mutation == "boolean_cap": data["days"][0]["max_sessions"] = True
    if mutation == "fractional_cap": data["days"][0]["max_sessions"] = 1.5
    if mutation == "doubles_without_cap": data["days"][0]["doubles_allowed"] = True
    if mutation == "cap_without_doubles": data["days"][0]["max_sessions"] = 2
    if mutation == "unknown_with_window": data["days"][0].update(state="UNKNOWN", max_sessions=None)
    if mutation == "blank_actor": data["actor"] = "   "
    if mutation == "spoofed_time": data["known_at"] = "2000-01-01T00:00:00Z"
    async with server() as client:
        await save(client, "2026-07-06", data, 422)
        assert (await client.get("/api/availability/2026-07-06")).json()["versions"] == []


async def test_existing_training_database_gets_are_read_only_and_upgrade_preserves_actuals(history, tmp_path):
    async with server() as client:
        await upload(client, ("old-format.fit", trace_fit()))
        old_actual = (await client.get("/api/activities/1/canonical")).json()
        dbfile = tmp_path / "runs.sqlite3"
        # This is the immediately preceding format: all training tables, no availability table.
        with sqlite3.connect(dbfile) as db:
            db.execute("DROP TABLE availability_revisions")
        before = dbfile.read_bytes()
        assert (await client.get("/api/availability/2026-07-06")).json()["submission_state"] == "NOT_ENTERED"
        assert (await client.get("/availability?week=2026-07-06")).status_code == 200
        assert dbfile.read_bytes() == before
        await save(client, "2026-07-06", submission())
        assert (await client.get("/api/activities/1/canonical")).json() == old_actual
        assert (await client.get("/api/activities/1")).json()["distance_m"] == 9.75


async def test_concurrent_retry_commits_once_and_competing_edits_conflict(history, tmp_path):
    store = Store(tmp_path / "runs.sqlite3")
    def enter(value):
        try: return availability.save(store, "2026-07-06", value)
        except availability.Conflict: return "conflict"
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(enter, [submission()] * 4))
    assert sum(not r["replayed"] for r in results) == 1
    first = results[0]["record"]["version_id"]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(enter, [submission(request_id="a", expected_revision=first), submission(request_id="b", expected_revision=first)]))
    assert results.count("conflict") == 1
    assert len(availability.read(store, "2026-07-06")["versions"]) == 2


@pytest.mark.parametrize("raw", [b'null', b'[]', b'{"days":[],"days":[]}', b'{"max_sessions":NaN}', b'\xff'])
async def test_malformed_json_is_rejected(history, raw):
    async with server() as client:
        assert (await client.post("/api/availability/2026-07-06", content=raw)).status_code == 422


async def test_bad_week_version_and_origin(history):
    async with server() as client:
        for week in ("2026-07-07", "2026-02-30", "9999-12-27", "not-a-date"):
            assert (await client.get("/api/availability/" + week)).status_code == 422
        assert (await client.get("/api/availability/2026-07-06/versions/bad")).status_code == 422
        assert (await client.post("/api/availability/2026-07-06", json=submission(), headers={"Origin": "https://elsewhere.example"})).status_code == 403
