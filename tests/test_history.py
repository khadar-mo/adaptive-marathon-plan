from contextlib import closing
from datetime import date
import sqlite3

import pytest

import garmin_coverage
import runs
import storage
from fit_fixture import START, make_fit
from test_fit_import import resolve, upload, zipped
from test_imports import DAY, OCTOBER, add_run, anyio_backend, digest, history, server, submit


pytestmark = pytest.mark.anyio
WEEK = {"from": "2026-10-19", "to": "2026-10-25"}


def fixed_today(monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 16))
    monkeypatch.setattr(storage, "now", lambda: "2026-11-16T12:00:00+00:00")


async def period(client, params=None):
    response = await client.get("/api/history", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_october_and_full_week_offline_through_normal_sync_repeat(history, monkeypatch):
    fixed_today(monkeypatch)
    history.listed = []
    for gid, local, utc, sport, distance in [
        (31, "2026-10-01", "2026-09-30", "trail_running", 5000),
        (32, "2026-10-19", "2026-10-19", "treadmill_running", 1004),
        (33, "2026-10-25", "2026-10-25", "track_running", 1004),
        (34, "2026-10-25", "2026-10-25", "running", None),
        (35, "2026-11-01", "2026-10-31", "virtual_run", 6000),
        (36, "2026-10-23", "2026-10-23", "cycling", 20000),
    ]:
        row = add_run(history, gid, local, sport, utc)
        row.pop("distance")
        history.extra_details[str(gid)]["summaryDTO"]["distance"] = distance
    # Two occurrences of the same recorded wall time as London turns the clocks back.
    for index, utc in [(2, "00:35:00"), (3, "01:35:00")]:
        history.listed[index].update(startTimeLocal="2026-10-25 01:35:00", startTimeGMT="2026-10-25 " + utc)
    async with server() as client:
        assert (await submit(client, OCTOBER))["state"] == "complete"
        oct_data = await period(client, OCTOBER)
        assert oct_data["run_count"] == 4 and {g for r in oct_data["activities"] for g in r["garmin_ids"]} == {"31","32","33","34"}
        assert oct_data["totals"]["distance_m"] == {"value": 7008, "display": "7.01 km", "missing_count": 1}
        assert oct_data["coverage"] == {"complete": True, "checked_ranges": [OCTOBER], "gaps": []}
        backfill = await submit(client, {})
        assert (backfill["requested_from"], backfill["requested_to"]) == ("2026-06-01", "2026-11-16")
        assert backfill["counts"]["inserted"] == 1
        repeat = await submit(client, {})
        assert repeat["requested_from"] == "2026-11-03" and repeat["counts"]["inserted"] == 0
        assert await period(client, OCTOBER) == oct_data

    calls = list(history.calls)
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    monkeypatch.setattr(runs, "load_settings", lambda: pytest.fail("view loaded provider credentials"))
    async with server() as client:
        week = await period(client, {"period": "week", "date": "2026-10-25"})
        assert week == await period(client, WEEK)
        assert (week["from"], week["to"], week["run_count"]) == (*WEEK.values(), 3)
        assert [r["garmin_id"] for r in week["activities"]] == ["34", "33", "32"]
        assert week["totals"]["distance_m"] == {"value": 2008, "display": "2.01 km", "missing_count": 1}
        assert week["totals"]["timer_seconds"] == {"value": 4500, "display": "1:15:00", "missing_count": 0}
        assert week["longest_distance_m"] == 1004 and week["longest_run_ids"] == [r["id"] for r in week["activities"][1:]]
        assert week["weeks"][0]["activity_ids"] == [r["id"] for r in week["activities"]]
        assert oct_data["months"][0]["run_count"] == 4 and [w["run_count"] for w in oct_data["weeks"]] == [1, 3]
        html = (await client.get("/history", params=WEEK)).text
        for row in week["activities"]:
            link = f'/activities/{row["id"]}'
            assert link in html and (await client.get("/api" + link)).json()["garmin_id"] == row["garmin_id"]
        assert all(v in html for v in ("Known distance", "2.01 km", "1 missing", "coverage complete"))
        november = await period(client)
        assert (november["from"], november["to"]) == ("2026-11-01", "2026-11-30")
        assert [r["garmin_id"] for r in november["activities"]] == ["35"]
        assert november["coverage"]["gaps"] == [{"from": "2026-11-17", "to": "2026-11-30"}]
        assert await period(client, {"period": "month", "date": "2026-10-25"}) == oct_data
        assert history.calls == calls


async def test_paused_run_laps_and_metric_sources_on_both_views(history):
    history.detail["summaryDTO"].update(distance=5000, elapsedDuration=1800, averageHR=153)
    history.listed[0].update(averageHR=120)
    del history.listed[0]["movingDuration"]
    fit = make_fit(metrics={"avg_heart_rate": 144}, laps=[(4200, 1260, 1450), (800, 240, 350)],
                   messages=[(20, {"position_lat": 1000, "position_long": -2000})])
    history.original = zipped(fit)
    async with server() as client:
        await submit(client)
        data = await period(client, DAY)
        item, = data["activities"]
        detail = (await client.get(f'/api/activities/{item["id"]}')).json()
        assert detail["display"] == item["display"] == {
            "distance_m": "5.00 km", "timer_seconds": "25:00", "elapsed_seconds": "30:00",
            "moving_seconds": "Not available", "timer_pace_seconds_per_km": "5:00/km",
            "moving_pace_seconds_per_km": "Not available",
        }
        assert (detail["average_hr_bpm"], detail["gps_available"]) == (153, True)
        assert (detail["metric_sources"]["average_hr_bpm"], detail["metric_sources"]["gps_available"]) == ("detail", "fit")
        assert [l["display"]["elapsed_seconds"] for l in detail["laps"]] == ["24:10", "5:50"]
        # Re-select an earlier archived detail after an intervening edit.
        history.detail["summaryDTO"].update(averageHR=133, startLatitude=30, startLongitude=20)
        await submit(client)
        edited = (await client.get("/api/activities/1")).json()
        assert edited["average_hr_bpm"] == 133 and edited["metric_sources"]["gps_available"] == "detail"
        history.detail["summaryDTO"] = {"distance": 5000, "elapsedDuration": 1800, "averageHR": 153}
        await submit(client)
        assert (await client.get("/api/activities/1")).json()["average_hr_bpm"] == 153
        history.detail["summaryDTO"].pop("averageHR")
        await submit(client)
        listed = (await client.get("/api/activities/1")).json()
        assert listed["average_hr_bpm"] == 120 and listed["metric_sources"]["average_hr_bpm"] == "list"


async def test_fit_only_undated_unresolved_and_nonrunning_edits(history, monkeypatch):
    fixed_today(monkeypatch)
    history.listed = []
    async with server() as client:
        empty = await submit(client, OCTOBER)
        assert empty["state"] == "complete" and "No runs returned" in (await client.get("/history", params=OCTOBER)).text
        undated = await upload(client, ("undated.fit", make_fit(offset=None, metrics={"avg_heart_rate": 140})))
        pending = await upload(client, ("near.fit", make_fit(start=START+30)))
        data = await period(client, OCTOBER)
        assert data["run_count"] == 0 and [r["id"] for r in data["undated_activities"]] == [1]
        assert pending["state"] == "partial" and not pending["activities"]
        assert "Undated runs" in (await client.get("/history", params=OCTOBER)).text
        assert (await resolve(client, undated, {"action": "set_local_date", "local_date": "2026-10-20"})).status_code == 200
        dated = await period(client, WEEK)
        assert dated["run_count"] == 1 and not dated["undated_activities"] and dated["longest_run_ids"] == []
        fit = (await client.get("/api/activities/1")).json()
        assert fit["average_hr_bpm"] == 140 and fit["metric_sources"]["average_hr_bpm"] == "fit"
        add_run(history, 81, "2026-10-23")
        await submit(client, WEEK)
        assert (await period(client, WEEK))["run_count"] == 2
        history.extra_details["81"]["activityTypeDTO"]["typeKey"] = "cycling"
        await submit(client, WEEK)
        assert (await period(client, WEEK))["run_count"] == 1


async def test_recheck_with_missing_metadata_reopens_only_affected_dates(history, monkeypatch):
    fixed_today(monkeypatch)
    history.listed = []
    add_run(history, 72, "2026-10-21")
    history.original_error = runs.ConnectionProblem("Synthetic missing original")
    async with server() as client:
        await submit(client, OCTOBER)
        assert (await period(client, WEEK))["coverage"]["complete"]
        assert "Original unavailable" in (await client.get("/activities/1")).text
        history.detail_error = runs.ConnectionProblem("Synthetic missing detail")
        await submit(client, WEEK)
        result = await period(client, OCTOBER)
        assert result["run_count"] == 1
        assert result["coverage"]["gaps"] == [{"from": "2026-10-21", "to": "2026-10-21"}]
        assert "coverage incomplete" in (await client.get("/history", params=WEEK)).text
        history.detail_error = None
        await submit(client, {})
        assert (await period(client, OCTOBER))["coverage"]["complete"]


async def test_distances_before_rounding_and_linked_ids_count_once(history):
    history.listed = []
    for gid, day, metres in [(80, "2026-10-19", 1004.0), (81, "2026-10-20", 1004.01)]:
        add_run(history, gid, day)
        history.extra_details[str(gid)]["summaryDTO"]["distance"] = metres
    async with server() as client:
        await submit(client, WEEK)
        week = await period(client, WEEK)
        assert [r["display"]["distance_m"] for r in week["activities"]] == ["1.00 km", "1.00 km"]
        assert week["totals"]["distance_m"] == {"value": 2008.01, "display": "2.01 km", "missing_count": 0}
        assert week["longest_run_ids"] == [week["activities"][0]["id"]]
        # A second Garmin ID supplies identical FIT bytes. Link the pending ID explicitly.
        history.listed = []
        add_run(history, 82, "2026-10-20")
        history.original = make_fit(serial=81)
        pending = await submit(client, WEEK)
        assert (await period(client, WEEK))["run_count"] == 2
        assert (await resolve(client, pending, {"action": "link", "activity_id": week["longest_run_ids"][0]})).status_code == 200
        linked = await period(client, WEEK)
        assert linked["run_count"] == 2 and linked["activities"][0]["garmin_ids"] == ["81", "82"]
        assert linked["totals"] == week["totals"]
        alias = history.extra_details["82"]
        alias["activityTypeDTO"]["typeKey"] = "cycling"
        await submit(client, WEEK)
        assert (await period(client, WEEK))["activities"][0]["garmin_ids"] == ["81"]
        alias["activityTypeDTO"]["typeKey"] = "trail_running"
        alias["summaryDTO"].update(distance=2625, startTimeLocal="2026-10-29 11:30:00")
        await submit(client, WEEK)
        assert (await period(client, WEEK))["activities"][0]["garmin_ids"] == ["81"]
        # Reclassifying the primary must not hide the still-running alias or use cycling metrics.
        add_run(history, 81, "2026-10-20", "cycling")
        await submit(client, OCTOBER)
        assert (await period(client, WEEK))["run_count"] == 1
        whole = await period(client, OCTOBER)
        assert whole["run_count"] == 2
        alternate, = [r for r in whole["activities"] if r["garmin_id"] == "82"]
        assert (alternate["garmin_ids"], alternate["local_date"], alternate["distance_m"]) == (["82"], "2026-10-29", 2625)
        detail = (await client.get(f'/api/activities/{alternate["id"]}')).json()
        assert detail["display"] == alternate["display"] and detail["garmin_url"].endswith("/82")


async def test_partial_week_drilldown_matches_its_displayed_dates(history):
    history.listed = []
    add_run(history, 50, "2026-09-30")
    add_run(history, 51, "2026-10-01")
    async with server() as client:
        await submit(client, {"from": "2026-09-30", "to": "2026-10-31"})
        html = (await client.get("/history", params=OCTOBER)).text
        assert 'href="/history?from=2026-10-01&amp;to=2026-10-04">2026-10-01 to 2026-10-04</a> · 1 run' in html
        detail = await period(client, {"from": "2026-10-01", "to": "2026-10-04"})
        assert [r["garmin_id"] for r in detail["activities"]] == ["51"]
        assert (await client.get("/favicon.ico")).status_code == 204


async def test_reclassified_primary_opens_the_running_alias_recorded_laps(history, tmp_path):
    history.listed = []
    add_run(history, 91, "2026-10-20")
    history.original = zipped(make_fit(laps=[(5000, 1500, 1800)], metrics={"total_moving_time": 1499}))
    async with server() as client:
        await submit(client, WEEK)
        history.listed = []
        add_run(history, 92, "2026-10-21")
        history.listed[0].pop("movingDuration")
        alias_fit = make_fit(laps=[(3000, 900, 1000), (2000, 600, 800)], metrics={"avg_heart_rate": 144})
        history.original = zipped(alias_fit)
        pending = await submit(client, WEEK)
        assert (await resolve(client, pending, {"action": "link", "activity_id": 1})).status_code == 200
        assert [l["distance_m"] for l in (await client.get("/api/activities/1")).json()["laps"]] == [5000]
        add_run(history, 91, "2026-10-20", "cycling")
        await submit(client, WEEK)
        # The original ZIP is sufficient even if its extracted member later disappears.
        (tmp_path / "archive" / digest(alias_fit)).unlink()
        detail = (await client.get("/api/activities/1")).json()
        assert detail["garmin_id"] == "92" and [l["distance_m"] for l in detail["laps"]] == [3000, 2000]
        assert detail["average_hr_bpm"] == 144 and detail["moving_seconds"] is None
        html = (await client.get("/activities/1")).text
        assert "3000 m" in html and "2000 m" in html and "5000 m" not in html
        (tmp_path / "archive" / digest(history.original)).unlink()
        missing = (await client.get("/api/activities/1")).json()
        assert missing["laps"] == [] and missing["average_hr_bpm"] is None
        assert any("Original unavailable" in warning for warning in missing["warnings"])


async def test_lap_only_gps_and_timing_discrepancies(history):
    lap = {"total_distance": 5000, "total_timer_time": 1500, "total_elapsed_time": 1800,
           "end_position_lat": 1000, "end_position_long": -2000}
    history.original = zipped(make_fit(messages=[(19, lap)]))
    history.detail["summaryDTO"].update(distance=5000, duration=1400, elapsedDuration=1610)
    async with server() as client:
        await submit(client)
        run = (await client.get("/api/activities/1")).json()
        assert run["gps_available"] is True and run["metric_sources"]["gps_available"] == "fit"
        assert (run["timer_seconds"], run["elapsed_seconds"]) == (1400, 1610)
        assert (run["laps"][0]["timer_seconds"], run["laps"][0]["elapsed_seconds"]) == (1500, 1800)
        assert run["warnings"] == ["Recorded lap timer times differ from the Garmin activity timer time.",
                                  "Recorded lap elapsed times differ from the Garmin activity elapsed time."]


async def test_legacy_sensor_uses_selected_detail_acquisition_offline(history, tmp_path, monkeypatch):
    history.listed[0]["averageHR"] = 100
    async with server() as client:
        for hr, stamp in [(140, "10:00:00"), (155, "11:00:00")]:
            monkeypatch.setattr(storage, "now", lambda stamp=stamp: "2026-11-16T" + stamp + "+00:00")
            history.detail["summaryDTO"]["averageHR"] = hr
            await submit(client)
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.execute("DROP TABLE garmin_scan_items")
        db.execute("DROP TABLE garmin_scans")
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    async with server() as client:
        reread = (await client.get("/api/activities/1")).json()
        assert reread["average_hr_bpm"] == 155 and reread["metric_sources"]["average_hr_bpm"] == "detail"


@pytest.mark.parametrize("metres,timer,moving,pace,moving_pace", [
    (0, 10, 20, None, None), (1000, 0, -2, None, None),
    (-1, float("nan"), float("inf"), None, None), (1000, 300.5, 299.49, 301, 299),
    (1000, 4200, None, 4200, None),
])
async def test_invalid_and_half_second_inputs(history, metres, timer, moving, pace, moving_pace):
    history.detail["summaryDTO"].update(distance=metres, duration=timer, movingDuration=moving)
    for key in ("distance", "duration", "movingDuration"):
        history.listed[0].pop(key, None)
    async with server() as client:
        await submit(client)
        activity, = (await period(client, DAY))["activities"]
        assert activity["timer_pace_seconds_per_km"] == pace and activity["moving_pace_seconds_per_km"] == moving_pace
        assert activity["distance_m"] == (metres if metres >= 0 else None)
        if pace == 4200:
            assert activity["display"]["timer_pace_seconds_per_km"] == "70:00/km"


async def test_empty_invalid_dates_and_unreadable_dashboard(history, tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    async with server() as client:
        assert (await client.get("/")).headers["location"] == "/history"
        for query in ("from=2026-10-01", "to=2026-10-31", "from=2026-11-01&to=2026-10-31",
                      "from=2026-02-30&to=2026-03-01", "date=20260201", "date=", "period=year",
                      "period=week&period=month", "from=2026-10-01&to=2026-10-31&period=week"):
            for path in ("/api/history", "/history"):
                assert (await client.get(f"{path}?{query}")).status_code == 422
        for anchor in ("0001-01-01", "9999-12-31", "2024-02-29"):
            assert (await period(client, {"period": "week", "date": anchor}))["run_count"] == 0
        assert (await period(client, {"date": "2024-02-29"}))["to"] == "2024-02-29"
        assert not list(tmp_path.iterdir()) and not history.calls
        (tmp_path / "runs.sqlite3").write_bytes(b"broken database" * 40)
        for path in ("/history", "/api/history"):
            assert (await client.get(path)).status_code == 503
