from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import sqlite3
import threading
import asyncio
from zipfile import ZipFile

from garmin_fit_sdk import convert_datetime_to_timestamp
import pytest

from fit_fixture import START, make_fit
from test_imports import DETAIL, LISTED, digest, history, server, submit
from runs import ConnectionProblem
from storage import Store


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def zipped(data):
    with BytesIO() as stream:
        with ZipFile(stream, "w") as container:
            container.writestr("original/ACTIVITY.FIT", data)
        return stream.getvalue()


async def upload(client, *files):
    response = await client.post("/api/imports/fit", files=[("files", file) for file in files])
    assert response.status_code == 202, response.text
    result = await client.get(response.headers["location"])
    assert result.status_code == 200
    return result.json()


async def resolve(client, report, choice):
    issue, = [i for i in report["issues"] if i.get("kind")]
    return await client.post(f'/api/imports/{report["id"]}/issues/{issue["id"]}/resolve', json=choice)


async def test_offline_mixed_batch_preserves_bytes_and_recorded_partial_laps(history, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    good = make_fit(laps=[(1267.19, 371.346, 400.532), (210.75, 60, 67.75)],
                    metrics={"total_distance": 1477.94, "total_timer_time": 431.346, "total_elapsed_time": 468.282})
    bad = good[:-1] + bytes([good[-1] ^ 1])
    files = [("original.fit", good), ("bad.fit", bad), ("bicycle.fit", make_fit(sport=2)),
             ("multi.fit", make_fit(sessions=2)), ("renamed.fit", good)]
    async with server() as client:
        missing = await client.post("/api/imports/fit")
        assert missing.status_code == 422
        assert (await client.post("/api/imports/fit", files={"files": ("x.fit", good)},
                                  headers={"Origin": "https://elsewhere.invalid"})).status_code == 403
        assert not (tmp_path / "runs.sqlite3").exists()
        report = await upload(client, *files)
        assert report["source"] == "fit" and report["requested_from"] is None and report["requested_to"] is None
        assert report["state"] == "partial"
        assert [(i["item"], i["step"]) for i in report["issues"]] == [(n, "Decode FIT") for n, _ in files[1:4]]
        assert [f["filename"] for f in report["files"]] == [n for n, _ in files]
        assert [f["activity_id"] for f in report["files"]] == [1, None, None, None, 1]
        for (_, data), f in zip(files, report["files"]):
            assert f["sha256"] == digest(data)
            archived = tmp_path / "archive" / digest(data)
            assert archived.read_bytes() == data and archived.stat().st_mode & 0o077 == 0
        item, = report["activities"]
        activity = (await client.get(f'/api/activities/{item["id"]}')).json()
        assert activity["garmin_id"] is None and activity["garmin_url"] is None
        assert (activity["distance_m"], activity["elapsed_seconds"], activity["moving_seconds"]) == (1477.94, 468.282, None)
        assert [(l["sequence"], l["distance_m"], l["timer_seconds"], l["elapsed_seconds"]) for l in activity["laps"]] == [
            (1, 1267.19, 371.346, 400.532), (2, 210.75, 60, 67.75)]
        assert "210.75 m" in (await client.get('/activities/1')).text
        assert "Offline FIT upload" in (await client.get('/imports')).text
        renamed = await upload(client, ("separate name.fit", good))
        assert renamed["activities"] == [{**item, "outcome": "unchanged"}] and not renamed["issues"]
        assert (await client.get("/api/activities/2")).status_code == 404
        assert not history.calls


@pytest.mark.parametrize("fit_first", [True, False])
@pytest.mark.parametrize("reencoded", [True, False])
async def test_both_orders_and_matching_complete_device_tuple(history, tmp_path, fit_first, reencoded):
    raw = make_fit(laps=[(320.75, 80, 95)], metrics={"total_distance": 320.75})
    from_garmin = make_fit(laps=[(320.75, 80, 95)], file_number=7, metrics={"total_distance": 320.75}) if reencoded else raw
    history.original = zipped(from_garmin)
    async with server() as client:
        if fit_first:
            a, b = await upload(client, ("manual.fit", raw)), await submit(client)
        else:
            a, b = await submit(client), await upload(client, ("manual.fit", raw))
        assert a["state"] == b["state"] == "complete" and a["activities"][0]["id"] == b["activities"][0]["id"]
        assert (await client.get('/api/activities/2')).status_code == 404
        activity = (await client.get('/api/activities/1')).json()
        assert activity["garmin_id"] == "42" and activity["distance_m"] == 5001.25
        assert activity["metric_sources"]["distance_m"] == "detail"
        assert set(activity["fit_sha256"]) == {digest(raw), digest(from_garmin)}
        assert activity["laps"][0]["distance_m"] == 320.75
        assert (tmp_path / "archive" / digest(history.original)).read_bytes() == history.original
        assert (tmp_path / "archive" / digest(from_garmin)).read_bytes() == from_garmin


@pytest.mark.parametrize("change", [{"manufacturer": 2}, {"product": 999}, {"serial": 123}, {"created": START + 1},
                                    {"serial": None}, {"start": START + 60}])
async def test_weak_or_different_identity_never_auto_links(history, change):
    async with server() as client:
        await upload(client, ("first.fit", make_fit()))
        second = await upload(client, ("similar.fit", make_fit(**change)))
        assert second["state"] == "partial" and not second["activities"] and not second["files"][0]["activity_id"]
        assert [c["id"] for c in second["issues"][0]["candidates"]] == [1]
        assert (await client.get("/api/activities/2")).status_code == 404


@pytest.mark.parametrize("action", ["link", "keep_separate"])
async def test_time_window_review_is_durable_and_retry_is_idempotent(history, action):
    choice = {"action": action, **({"activity_id": 1} if action == "link" else {})}
    async with server() as client:
        await upload(client, ("first.fit", make_fit()))
        other = make_fit(start=START + 60)
        pending = await upload(client, ("second.fit", other))
        again = await upload(client, ("still pending.fit", other))
        assert not pending["activities"] and not again["activities"]
        assert "Link to activity" in (await client.get("/imports")).text
        assert (await resolve(client, pending, {"action": "link", "activity_id": 999})).status_code == 409
        wrong_report = f'/api/imports/{again["id"]}/issues/{pending["issues"][0]["id"]}/resolve'
        assert (await client.post(wrong_report, json=choice)).status_code == 404
        response = await resolve(client, pending, choice)
        assert response.status_code == 200
        target = 1 if action == "link" else 2
        assert response.json()["activity_id"] == target
        assert (await resolve(client, again, choice)).json()["activity_id"] == target
        assert (await client.get(again["report_url"])).json()["files"][0]["activity_id"] == target
    async with server() as client:
        replay = await upload(client, ("renamed.fit", other))
        assert replay["state"] == "complete" and replay["activities"][0]["id"] == target
        assert not replay["issues"]
        assert (await client.get(pending["report_url"])).json()["issues"][0]["resolution"] == action
        assert (await client.get(f"/api/activities/{target + 1}")).status_code == 404


async def test_outside_window_is_distinct_and_undated_requires_valid_confirmation(history):
    async with server() as client:
        await upload(client, ("first.fit", make_fit()))
        undated = make_fit(start=START + 61, offset=None)
        pending = await upload(client, ("undated.fit", undated))
        assert pending["activities"][0]["id"] == 2 and pending["issues"][0]["kind"] == "date"
        row = (await client.get("/api/activities/2")).json()
        assert row["start_local"] is None and row["local_date"] is None
        assert "Undated" in (await client.get("/activities/2")).text
        for bad in ("2024-02-30", "20240229", "2024-2-29", 0):
            assert (await resolve(client, pending, {"action": "set_local_date", "local_date": bad})).status_code == 422
        assert (await client.get("/api/activities/2")).json() == row
        valid = {"action": "set_local_date", "local_date": "2024-02-29"}
        assert (await resolve(client, pending, valid)).status_code == 200
        assert (await resolve(client, pending, valid)).status_code == 200
    async with server() as client:
        retry = await upload(client, ("same.fit", undated))
        assert retry["state"] == "complete" and not retry["issues"]
        confirmed = (await client.get("/api/activities/2")).json()
        assert confirmed["start_local"] is None and confirmed["local_date"] == valid["local_date"]


async def test_activity_local_midnight_and_bad_offset(history):
    start = convert_datetime_to_timestamp(datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc))
    async with server() as client:
        await upload(client, ("boundary.fit", make_fit(start=start, offset=3600)))
        row = (await client.get("/api/activities/1")).json()
        assert row["local_date"] == "2026-11-01" and row["start_utc"].startswith("2026-10-31")
        bogus = await upload(client, ("bad offset.fit", make_fit(start=start + 300, offset=86400)))
        assert bogus["issues"][0]["kind"] == "date" and (await client.get("/api/activities/2")).json()["local_date"] is None


async def test_conflicting_garmin_ids_cannot_split_identical_fit_bytes(history):
    history.original = zipped(make_fit())
    async with server() as client:
        await submit(client)
        history.listed = [{**LISTED, "activityId": 51}]
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        conflict = await submit(client)
        assert not conflict["activities"] and conflict["issues"][0]["kind"] == "match"
        assert (await resolve(client, conflict, {"action": "keep_separate"})).status_code == 409
        assert (await resolve(client, conflict, {"action": "link", "activity_id": 1})).status_code == 200
        replay = await submit(client)
        assert not replay["issues"] and replay["activities"][0]["id"] == 1
        row = (await client.get("/api/activities/1")).json()
        assert row["garmin_id"] == "42" and (await client.get("/api/activities/2")).status_code == 404


async def test_pr9_database_preserves_existing_id_and_archived_member_without_garmin(history, tmp_path, monkeypatch):
    # These are the pre-FIT tables and constraints used by the merged first importer.
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.executescript("""
        CREATE TABLE imports (id INTEGER PRIMARY KEY, requested_from TEXT NOT NULL, requested_to TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL);
        CREATE UNIQUE INDEX one_running_import ON imports(state) WHERE state='running';
        CREATE TABLE activities (id INTEGER PRIMARY KEY, garmin_id TEXT UNIQUE, name TEXT, sport TEXT,
            start_local TEXT NOT NULL, local_date TEXT NOT NULL, start_utc TEXT,
            distance_m REAL, timer_seconds REAL, elapsed_seconds REAL, moving_seconds REAL,
            metric_sources TEXT NOT NULL, original_warning TEXT);
        CREATE TABLE activity_sources (activity_id INTEGER NOT NULL REFERENCES activities(id),
            kind TEXT NOT NULL, sha256 TEXT NOT NULL, format TEXT NOT NULL, acquired_at TEXT NOT NULL,
            PRIMARY KEY (activity_id, kind, sha256));
        INSERT INTO imports VALUES (5, '2024-02-29', '2024-02-29', 'then', 'then', 'complete');
        INSERT INTO activities VALUES (731, '42', 'Legacy', 'running', '2024-02-29 00:15', '2024-02-29',
            NULL, 4500, NULL, NULL, NULL, '{}', NULL);
        """)
        data = make_fit(laps=[(324.5, 80, 120)])
        original = zipped(data)
        directory = tmp_path / "archive"
        directory.mkdir(mode=0o700)
        (directory / digest(original)).write_bytes(original)
        db.execute("INSERT INTO activity_sources VALUES (731, 'original', ?, 'zip', 'then')", (digest(original),))
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    async with server() as client:
        assert (await client.get("/api/imports/5")).json()["source"] == "garmin"
        assert (await client.get("/api/activities/731")).json()["laps"][0]["distance_m"] == 324.5
        replay = await upload(client, ("export.fit", data))
        assert replay["activities"][0]["id"] == 731 and not replay["issues"]
        assert (await client.get("/api/activities/732")).status_code == 404
        assert (await client.get("/api/activities/731")).json()["distance_m"] == 4500
        assert history.calls == []


async def test_recognize_nearby_garmin_run_using_its_fit_start(history):
    del history.listed[0]["startTimeGMT"]
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client)
        assert not pending["activities"] and pending["issues"][0]["kind"] == "match"
        assert [c["id"] for c in pending["issues"][0]["candidates"]] == [1]


async def test_unavailable_original_does_not_discard_unresolved_identity(history):
    history.original = zipped(make_fit())
    async with server() as client:
        await submit(client)
        history.listed = [{**LISTED, "activityId": 51}]
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        conflict = await submit(client)
        history.original_error = ConnectionProblem("Fixture unavailable.")
        retry = await submit(client)
        assert retry["state"] == "partial" and not retry["activities"]
        assert {i["step"] for i in retry["issues"]} == {"Original download", "Activity identity"}
        assert (await client.get("/api/activities/2")).status_code == 404
        assert (await resolve(client, conflict, {"action": "link", "activity_id": 1})).status_code == 200
        assert (await client.get(retry["report_url"])).json()["state"] == "complete"


@pytest.mark.parametrize("target", [1, 2])
@pytest.mark.parametrize("same_bytes", [True, False])
async def test_resolve_existing_manual_and_garmin_rows_without_losing_links(history, tmp_path, target, same_bytes):
    manual = make_fit(laps=[(183.43, 40, 50)], metrics={"total_timer_time": 222, "total_moving_time": 111})
    for key in ("startTimeGMT", "duration", "movingDuration"):
        history.listed[0].pop(key)
    history.original_error = ConnectionProblem("Fixture original is offline.")
    async with server() as client:
        early = await upload(client, ("manual.fit", manual))
        later = await submit(client)
        assert early["activities"][0]["id"] == 1 and later["activities"][0]["id"] == 2
        history.original_error = None
        history.original = zipped(manual if same_bytes else make_fit(file_number=12, laps=[(183.43, 40, 50)]))
        conflict = await submit(client)
        assert {c["id"] for c in conflict["issues"][0]["candidates"]} == {1, 2}
        assert (await resolve(client, conflict, {"action": "link", "activity_id": target})).status_code == 200
        for old in (1, 2):
            row = (await client.get(f"/api/activities/{old}")).json()
            assert row["id"] == target and row["garmin_id"] == "42" and row["laps"][0]["distance_m"] == 183.43
            assert (row["timer_seconds"], row["moving_seconds"]) == (222, 111)
            assert row["metric_sources"]["timer_seconds"] == "fit"
        for report in (early, later, conflict):
            assert (await client.get(report["report_url"])).json()["activities"][0]["id"] == target
        assert (await upload(client, ("renamed.fit", manual)))["activities"][0]["id"] == target
        assert (await submit(client))["activities"][0]["id"] == target
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db:
            assert db.execute("SELECT count(*) FROM activities").fetchone() == (1,)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert (await upload(client, ("distinct.fit", make_fit(start=START + 86400))))["activities"][0]["id"] == 3


async def test_confirmed_and_garmin_supplied_dates_reconcile_old_reports(history):
    async with server() as client:
        first = await upload(client, ("undated.fit", make_fit(offset=None)))
        confirmed = {"action": "set_local_date", "local_date": "2024-03-01"}
        await resolve(client, first, confirmed)
        await upload(client, ("reencoded.fit", make_fit()))
        row = (await client.get("/api/activities/1")).json()
        assert row["local_date"] == "2024-03-01" and row["start_local"] is None
        other = make_fit(start=START + 400, offset=None)
        pending = await upload(client, ("later.fit", other))
        history.original = zipped(other)
        await submit(client)
        saved = (await client.get(pending["report_url"])).json()
        assert saved["state"] == "complete" and saved["issues"][0]["resolution"] == "recorded_date"


async def test_resolving_before_worker_finishes_keeps_report_complete(history, monkeypatch):
    finishing, release = threading.Event(), threading.Event()
    original = Store.finish

    def pause(store, import_id, state):
        finishing.set()
        assert release.wait(5)
        original(store, import_id, state)

    monkeypatch.setattr(Store, "finish", pause)
    async with server() as client:
        request = asyncio.create_task(upload(client, ("no offset.fit", make_fit(offset=None))))
        try:
            assert await asyncio.to_thread(finishing.wait, 5)
            report = (await client.get("/api/imports/1")).json()
            assert (await resolve(client, report, {"action": "set_local_date", "local_date": "2024-03-01"})).status_code == 200
        finally:
            release.set()
        assert (await request)["state"] == "complete"


@pytest.mark.parametrize("marker", ["start_time", "message_index"])
async def test_wire_order_and_timestamp_correlation(history, marker):
    def lap(index, distance):
        return (19, {marker: START + 60 * index if marker == "start_time" else index, "total_distance": distance})

    data = make_fit(offset=None, messages=[lap(1, 751), lap(0, 1000),
                    (162, {"timestamp": START, "local_timestamp": START + 3600})])
    async with server() as client:
        report = await upload(client, ("summary first.fit", data))
        assert report["state"] == "complete" and not report["issues"]
        row = (await client.get("/api/activities/1")).json()
        assert row["local_date"] == "2024-02-29" and [lap["distance_m"] for lap in row["laps"]] == [1000, 751]


async def test_garmin_edits_during_pending_match_keep_fresh_headlines_and_previous_fit(history):
    manual = make_fit()
    history.original = zipped(make_fit(start=START + 20, serial=None, laps=[(333, 80, 90)]))
    history.detail["summaryDTO"]["distance"] = 5000
    old_detail = digest(history.wire(history.detail))
    async with server() as client:
        await upload(client, ("manual.fit", manual))
        first = await submit(client)
        assert not first["activities"]
        history.detail["summaryDTO"]["distance"] = 6000
        new_detail = digest(history.wire(history.detail))
        history.original_error = ConnectionProblem("Fixture original is offline.")
        retry = await submit(client)
        assert not retry["activities"]
        assert (await resolve(client, retry, {"action": "keep_separate"})).status_code == 200
        row = (await client.get("/api/activities/2")).json()
        assert row["distance_m"] == 6000 and row["laps"][0]["distance_m"] == 333
        assert {s["sha256"] for s in row["sources"] if s["kind"] == "detail"} == {old_detail, new_detail}


async def test_weak_retry_preserves_newer_conflicting_original_and_owner(history):
    def select_garmin(gid, data):
        history.listed = [{**LISTED, "activityId": gid}]
        history.extra_details[str(gid)] = {**DETAIL, "activityId": gid}
        history.original = zipped(data)

    x, y = make_fit(), make_fit(start=START + 600, serial=52, created=START + 600)
    async with server() as client:
        select_garmin(42, x)
        await submit(client)
        select_garmin(52, y)
        await submit(client)
        select_garmin(51, x)
        first = await submit(client)
        assert (await resolve(client, first, {"action": "link", "activity_id": 1})).status_code == 200
        select_garmin(51, y)
        changed = await submit(client)
        assert not changed["activities"]
        history.original_error = ConnectionProblem("Fixture original is offline.")
        weak = await submit(client)
        assert not weak["activities"] and weak["state"] == "partial"
        assert (await resolve(client, weak, {"action": "link", "activity_id": 2})).status_code == 200
        assert (await submit(client))["activities"][0]["id"] == 2
        assert (await client.get("/api/activities/1")).json()["garmin_id"] == "42"
        assert (await client.get("/api/activities/2")).json()["garmin_id"] == "52"
        assert (await client.get("/api/activities/3")).status_code == 404


async def test_reencoded_pending_originals_share_the_resolved_activity(history):
    first = make_fit(start=START + 20, serial=None, file_number=1)
    second = make_fit(start=START + 20, serial=None, file_number=2)
    async with server() as client:
        await upload(client, ("early.fit", make_fit()))
        history.original = zipped(first)
        first_outer = digest(history.original)
        old = await submit(client)
        history.original = zipped(second)
        second_outer = digest(history.original)
        latest = await submit(client)
        assert not old["activities"] and not latest["activities"]
        choice = await resolve(client, latest, {"action": "keep_separate"})
        assert choice.status_code == 200 and choice.json()["activity_id"] == 2
        assert (await client.get(old["report_url"])).json()["issues"][0]["activity_id"] == 2
        for name, data in (("first renamed.fit", first), ("second renamed.fit", second)):
            replay = await upload(client, (name, data))
            assert not replay["issues"] and replay["activities"][0]["id"] == 2
        row = (await client.get("/api/activities/2")).json()
        assert set(row["fit_sha256"]) == {digest(first), digest(second)}
        assert {s["sha256"] for s in row["sources"] if s["kind"] == "original"} == {first_outer, second_outer}
        assert (await client.get("/api/activities/3")).status_code == 404


async def test_unresolved_versions_with_different_known_owners_need_independent_choices(history):
    x, y = make_fit(), make_fit(serial=678, start=START + 800)
    async with server() as client:
        for gid, data in ((42, x), (52, y)):
            history.listed = [{**LISTED, "activityId": gid}]
            history.extra_details[str(gid)] = {**DETAIL, "activityId": gid}
            history.original = zipped(data)
            await submit(client)
        history.listed = [{**LISTED, "activityId": 51}]
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        history.original = zipped(x)
        first = await submit(client)
        history.original = zipped(y)
        second = await submit(client)
        assert (await resolve(client, second, {"action": "link", "activity_id": 2})).status_code == 200
        still_pending = (await client.get(first["report_url"])).json()
        assert still_pending["state"] == "partial" and still_pending["issues"][0]["resolution"] is None
        assert (await resolve(client, still_pending, {"action": "link", "activity_id": 1})).status_code == 200
        for target, data in ((1, x), (2, y)):
            assert (await upload(client, ("renamed.fit", data)))["activities"][0]["id"] == target
        assert (await client.get("/api/activities/3")).status_code == 404
