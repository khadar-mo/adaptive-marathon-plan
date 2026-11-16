from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import sqlite3
from zipfile import ZipFile

from garmin_fit_sdk import convert_datetime_to_timestamp
import pytest

from fit_fixture import START, make_fit
from test_imports import DAY, DETAIL, HOST, LISTED, digest, history, server, submit


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
