from contextlib import closing
from datetime import date, datetime, timezone
from io import BytesIO
import sqlite3
import threading
import asyncio
from zipfile import ZipFile

from garmin_fit_sdk import convert_datetime_to_timestamp
import pytest

import garmin_coverage
from fit_fixture import START, make_fit
from test_imports import DETAIL, LISTED, digest, history, server, submit
from runs import ConnectionProblem
import storage
from storage import Store


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def zipped(data, name="original/ACTIVITY.FIT"):
    with BytesIO() as stream:
        with ZipFile(stream, "w") as container:
            container.writestr(name, data)
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


@pytest.mark.parametrize("temporarily_missing", [False, True])
async def test_pr9_database_preserves_existing_id_and_archived_member_without_garmin(history, tmp_path, monkeypatch, temporarily_missing):
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
        if temporarily_missing:
            (directory / digest(original)).unlink()
            history.listed[0].pop("startTimeGMT")
            history.detail["summaryDTO"]["distance"] = 4500
            history.original_error = ConnectionProblem("Fixture unavailable.")
            with monkeypatch.context() as online:
                online.setenv("RUNS_GARMIN_OFFLINE", "0")
                missing = await submit(client)
            assert missing["scans"][0]["missing_original_ids"] == ["42"]
            (directory / digest(original)).write_bytes(original)
        prior_calls = list(history.calls)
        replay = await upload(client, ("export.fit", data))
        assert replay["activities"][0]["id"] == 731 and not replay["issues"]
        assert (await client.get("/api/activities/732")).status_code == 404
        assert (await client.get("/api/activities/731")).json()["distance_m"] == 4500
        assert history.calls == prior_calls


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


async def test_weak_retry_preserves_newer_conflicting_original_and_owner(history, tmp_path):
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
        # An unavailable cached source makes the range importer request the newer upstream version.
        (tmp_path / "archive" / digest(history.original)).unlink()
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


async def test_pending_reencodings_with_distinct_tuple_owners_never_share_a_decision(history):
    originals = [make_fit(serial=n) for n in (421, 521)]
    reencoded = [make_fit(serial=n, file_number=11) for n in (421, 521)]
    async with server() as client:
        for gid, data in zip((42, 52), originals):
            history.listed = [{**LISTED, "activityId": gid}]
            history.extra_details[str(gid)] = {**DETAIL, "activityId": gid}
            history.original = zipped(data)
            await submit(client)
        history.listed = [{**LISTED, "activityId": 51}]
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        history.original = zipped(reencoded[0])
        a = await submit(client)
        history.original = zipped(reencoded[1])
        b = await submit(client)
        assert (await resolve(client, b, {"action": "link", "activity_id": 2})).status_code == 200
        assert (await client.get(a["report_url"])).json()["issues"][0]["resolution"] is None
        assert (await resolve(client, a, {"action": "link", "activity_id": 1})).status_code == 200
        for target, data in enumerate(reencoded, 1):
            assert (await upload(client, ("renamed.fit", data)))["activities"][0]["id"] == target


async def test_resolving_changed_originals_honours_choices_made_while_they_were_pending(history):
    files = [make_fit(start=START + 20, serial=None, file_number=i) for i in (11, 12)]
    async with server() as client:
        await upload(client, ("seed.fit", make_fit()))
        reports = []
        for data in files:
            history.original = zipped(data)
            reports.append(await submit(client))
        for target, data in enumerate(files, 2):
            pending = await upload(client, ("manual.fit", data))
            assert (await resolve(client, pending, {"action": "keep_separate"})).json()["activity_id"] == target
        first, second = reports
        assert (await resolve(client, first, {"action": "link", "activity_id": 2})).status_code == 200
        assert (await client.get(second["report_url"])).json()["issues"][0]["resolution"] is None
        assert (await resolve(client, second, {"action": "link", "activity_id": 3})).status_code == 200
        assert (await submit(client))["activities"][0]["id"] == 3
        for target, data in enumerate(files, 2):
            assert (await client.get(f"/api/activities/{target}")).json()["id"] == target
            assert (await upload(client, ("renamed.fit", data)))["activities"][0]["id"] == target
        assert (await client.get("/api/activities/4")).status_code == 404


@pytest.mark.parametrize("chosen", [0, 1])
async def test_latest_compatible_garmin_metrics_and_laps_survive_either_resolution_order(history, chosen, tmp_path):
    files = [make_fit(start=START + 20, serial=None, file_number=n, laps=[(100 + n, 10 + n, 20 + n)]) for n in (1, 2)]
    async with server() as client:
        await upload(client, ("seed.fit", make_fit()))
        reports = []
        for distance, data in zip((4000, 8000), files):
            history.detail["summaryDTO"]["distance"] = distance
            history.original = zipped(data)
            reports.append(await submit(client))
        assert (await resolve(client, reports[chosen], {"action": "keep_separate"})).json()["activity_id"] == 2
        for report in reports:
            assert (await client.get(report["report_url"])).json()["state"] == "complete"
        for data in files:
            assert (await upload(client, ("renamed.fit", data)))["activities"][0]["id"] == 2
        row = (await client.get("/api/activities/2")).json()
        assert row["distance_m"] == 8000 and row["laps"][0]["distance_m"] == 102
        downloads = history.calls.count("/download-service/files/activity/42")
        await submit(client)
        assert history.calls.count("/download-service/files/activity/42") == downloads
        after = (await client.get("/api/activities/2")).json()
        assert after["distance_m"] == 8000 and after["laps"][0]["distance_m"] == 102
        preferred = tmp_path / "archive" / digest(history.original)
        preferred.unlink()
        history.original_error = ConnectionProblem("Fixture offline.")
        unavailable = await submit(client)
        assert unavailable["scans"][0]["originals_requested"] == 1 and unavailable["scans"][0]["missing_original_ids"] == ["42"]
        assert (await client.get("/api/activities/2")).json()["laps"][0]["distance_m"] == 102
        preferred.write_bytes(history.original)
        restored = await submit(client)
        assert restored["scans"][0]["originals_requested"] == 0 and restored["scans"][0]["originals_available"] == 1
        assert (await client.get("/api/activities/2")).json()["laps"][0]["distance_m"] == 102


async def test_normal_sync_holds_only_the_unresolved_garmin_day(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 7, 1))
    start = convert_datetime_to_timestamp(datetime(2026, 6, 11, 12, tzinfo=timezone.utc))
    fits = make_fit(start=start, created=start)
    history.listed[0].update(startTimeLocal="2026-06-11 13:00:00", startTimeGMT="2026-06-11 12:00:00")
    history.original = zipped(make_fit(start=start + 15, serial=None))
    async with server() as client:
        upload_report = await upload(client, ("export.fit", fits))
        assert upload_report["scans"] == [] and not history.calls
        pending = await submit(client, {})
        scan, = pending["scans"]
        assert (scan["from"], scan["to"], scan["coverage"]) == ("2026-06-01", "2026-07-01", "missing_activity")
        assert scan["unique_ids_found"] == 1 and scan["missing_activity_ids"] == ["42"]
        assert scan["checked_ranges"] == [{"from": "2026-06-01", "to": "2026-06-10"},
                                          {"from": "2026-06-12", "to": "2026-07-01"}]
        assert pending["state"] == "partial" and not pending["activities"]
        history.listed = []
        retry = await submit(client, {})
        assert retry["state"] == "partial" and retry["scans"][0]["unique_ids_found"] == 0
        assert (await resolve(client, pending, {"action": "link", "activity_id": 1})).status_code == 200
        for report in (pending, retry):
            reopened = (await client.get(report["report_url"])).json()
            scan, = reopened["scans"]
            assert reopened["state"] == "complete" and scan["coverage"] == "checked"
            assert reopened["activities"][0]["id"] == 1 and sum(reopened["counts"].values()) == 1
            assert scan["originals_available"] == 1 and scan["missing_activity_ids"] == []
        assert (await submit(client, {}))["requested_from"] == "2026-06-18"
        assert (await upload(client, ("renamed.fit", fits)))["activities"][0]["id"] == 1
        assert (await client.get("/api/activities/2")).status_code == 404


async def test_identity_resolution_does_not_complete_future_dates(history):
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client, {"from": "2024-02-29", "to": "2099-01-01"})
        assert pending["state"] == "partial" and pending["scans"][0]["missing_activity_ids"] == ["42"]
        assert (await resolve(client, pending, {"action": "keep_separate"})).status_code == 200
        reopened = (await client.get(pending["report_url"])).json()
        assert reopened["state"] == "partial" and reopened["scans"][0]["coverage"] == "unchecked"
        assert reopened["scans"][0]["missing_activity_ids"] == [] and reopened["activities"][0]["id"] == 2


async def test_excluded_garmin_edit_preserves_recorded_fit_measurements(history):
    history.listed[0].pop("movingDuration")
    history.original = zipped(make_fit(metrics={"total_moving_time": 111}))
    async with server() as client:
        first = await submit(client)
        path = f'/api/activities/{first["activities"][0]["id"]}'
        history.detail["activityTypeDTO"] = {"typeKey": "cycling"}
        changed = await submit(client)
        assert changed["scans"][0]["excluded"] == 1 and not changed["activities"]
        row = (await client.get(path)).json()
        assert row["sport"] == "cycling" and row["moving_seconds"] == 111 and row["metric_sources"]["moving_seconds"] == "fit"


@pytest.mark.parametrize("edit,reason", [("sport", "excluded_sport"), ("date", "resolved_outside")])
async def test_recheck_retires_stale_decision_without_creating_a_run(history, edit, reason):
    history.listed[0].pop("startTimeGMT")
    history.original = zipped(make_fit(start=START + 10, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client)
        if edit == "sport":
            history.detail["activityTypeDTO"] = {"typeKey": "cycling"}
        else:
            history.detail["summaryDTO"]["startTimeLocal"] = "2024-03-01 00:15:38"
        excluded = await submit(client)
        assert excluded["scans"][0]["excluded"] == 1
        assert (await resolve(client, pending, {"action": "keep_separate"})).status_code == 409
        old = (await client.get(pending["report_url"])).json()
        assert old["state"] == "complete" and old["scans"][0]["coverage"] == "checked_empty"
        assert old["issues"][0]["resolution"] == reason and not old["activities"]
        assert "/activities/None" not in (await client.get(old["html_url"])).text
        assert (await client.get("/api/activities/2")).status_code == 404
        history.detail = {**DETAIL, "summaryDTO": DETAIL["summaryDTO"].copy()}
        renewed = await submit(client)
        assert renewed["state"] == "partial" and renewed["issues"][0]["resolution"] is None
        assert (await resolve(client, renewed, {"action": "keep_separate"})).json()["activity_id"] == 2


async def test_adjacent_range_preserves_a_valid_review_and_its_latest_detail(history):
    history.listed[0].pop("startTimeGMT")
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client)
        history.detail["summaryDTO"]["distance"] = 6000
        neighbor = await submit(client, {"from": "2024-03-01", "to": "2024-03-01"})
        assert neighbor["scans"][0]["excluded"] == 1
        assert (await resolve(client, pending, {"action": "keep_separate"})).json()["activity_id"] == 2
        assert (await client.get("/api/activities/2")).json()["distance_m"] == 6000


async def test_linked_garmin_id_reuses_its_own_zip_and_appears_in_scan_receipts(history):
    raw = make_fit()
    history.original = zipped(raw)
    async with server() as client:
        await submit(client)
        history.listed = [{**LISTED, "activityId": 51}]
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        history.original = zipped(raw, "second.fit")
        pending = await submit(client)
        assert (await resolve(client, pending, {"action": "link", "activity_id": 1})).status_code == 200
        history.original_error = ConnectionProblem("Fixture unavailable.")
        replay = await submit(client)
        scan, = replay["scans"]
        assert scan["saved_garmin_ids"] == ["51"] and replay["activities"][0]["id"] == 1
        assert scan["originals_requested"] == 0 and scan["missing_original_ids"] == []
        assert history.calls.count("/download-service/files/activity/51") == 1
        assert "51" in (await client.get(replay["html_url"])).text
        assert len((await submit(client, {}))["scans"]) == 1


@pytest.mark.parametrize("archive_present", [True, False])
async def test_resolution_keeps_acquisition_time_and_retries_only_missing_originals(history, monkeypatch, tmp_path, archive_present):
    first_seen = "2026-07-02T12:00:00+00:00"
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        monkeypatch.setattr(storage, "now", lambda: first_seen)
        await submit(client)
        monkeypatch.setattr(storage, "now", lambda: "2026-07-03T12:00:00+00:00")
        history.original_error = ConnectionProblem("Fixture offline.")
        if not archive_present:
            (tmp_path / "archive" / digest(history.original)).unlink()
        retry = await submit(client)
        monkeypatch.setattr(storage, "now", lambda: "2026-07-04T12:00:00+00:00")
        assert (await resolve(client, retry, {"action": "keep_separate"})).json()["activity_id"] == 2
        scan, = (await client.get(retry["report_url"])).json()["scans"]
        assert scan["originals_requested"] == 1 and scan["originals_available"] == int(archive_present)
        assert scan["missing_original_ids"] == ([] if archive_present else ["42"])
        row = (await client.get("/api/activities/2")).json()
        assert bool(row["warnings"]) == (not archive_present)
        original, = [s for s in row["sources"] if s["kind"] == "original"]
        assert original["acquired_at"] == first_seen
        assert len((await submit(client, {}))["scans"]) == (1 if archive_present else 2)


async def test_linked_id_without_its_own_original_still_requests_it(history, monkeypatch):
    history.listed = [{**LISTED, "activityId": 51}]
    history.extra_details["51"] = {**DETAIL, "activityId": 51}
    history.original_error = ConnectionProblem("Fixture unavailable.")
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client)
        history.listed = [LISTED.copy()]
        history.original_error = None
        history.original = zipped(make_fit())
        # The scan also retries the held ID 51 even when it is absent from the search.
        original_open = history.open
        def unavailable_alias(request, timeout):
            if request.full_url.endswith("/download-service/files/activity/51"):
                raise ConnectionProblem("Fixture unavailable.")
            return original_open(request, timeout)
        with monkeypatch.context() as patch:
            patch.setattr(history, "open", unavailable_alias)
            await submit(client)
        assert (await resolve(client, pending, {"action": "link", "activity_id": 1})).status_code == 200
        history.listed = [{**LISTED, "activityId": 51}]
        history.original_error = ConnectionProblem("Fixture unavailable.")
        missing = await submit(client)
        scan, = missing["scans"]
        assert scan["saved_garmin_ids"] == ["51"] and scan["originals_requested"] == 1 and scan["missing_original_ids"] == ["51"]
        history.original_error = None
        history.original = zipped(make_fit(file_number=9), "alternate.fit")
        repaired = await submit(client)
        assert repaired["scans"][0]["originals_requested"] == 1 and not repaired["scans"][0]["missing_original_ids"]
        sources = (await client.get("/api/activities/1")).json()["sources"]
        assert digest(history.original) in [s["sha256"] for s in sources if s["kind"] == "original"]


async def test_resolution_verifies_the_original_when_the_decision_is_made(history, tmp_path):
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        pending = await submit(client)
        (tmp_path / "archive" / digest(history.original)).unlink()
        assert (await resolve(client, pending, {"action": "keep_separate"})).json()["activity_id"] == 2
        reopened = (await client.get(pending["report_url"])).json()
        assert reopened["scans"][0]["originals_available"] == 0 and reopened["scans"][0]["missing_original_ids"] == ["42"]
        assert any(i["step"] == "Original download" for i in reopened["issues"])
        assert (await client.get("/api/activities/2")).json()["warnings"]
        repaired = await submit(client, {})
        assert len(repaired["scans"]) == 2 and repaired["scans"][1]["originals_requested"] == 1
        assert not (await client.get("/api/activities/2")).json()["warnings"]


async def test_pending_day_moves_with_an_in_range_garmin_edit(history):
    history.listed[0].pop("startTimeGMT")
    history.original = zipped(make_fit(start=START + 15, serial=None))
    async with server() as client:
        await upload(client, ("manual.fit", make_fit()))
        broad = await submit(client, {"from": "2024-02-29", "to": "2024-03-01"})
        history.detail["summaryDTO"]["startTimeLocal"] = "2024-03-01 00:15:38"
        narrow = await submit(client, {"from": "2024-03-01", "to": "2024-03-01"})
        assert narrow["state"] == "partial"
        held = (await client.get(broad["report_url"])).json()["scans"][0]
        assert held["checked_ranges"] == [{"from": "2024-02-29", "to": "2024-02-29"}]
        assert (await resolve(client, broad, {"action": "keep_separate"})).json()["activity_id"] == 2
        for report in (broad, narrow):
            scan = (await client.get(report["report_url"])).json()["scans"][0]
            assert scan["earliest_saved"] == scan["latest_saved"] == "2024-03-01"
        assert (await client.get("/api/activities/2")).json()["local_date"] == "2024-03-01"
