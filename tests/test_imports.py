import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
from io import BytesIO
import json
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import httpx
import pytest

import app
import garmin_import
import runs


pytestmark = pytest.mark.anyio
DAY = {"from": "2024-02-29", "to": "2024-02-29"}
SUBMIT = "/api/imports/garmin"
HOST = "http://127.0.0.1:8001"
LISTED = {"activityId": 42, "activityName": "Fixture", "activityType": {"typeKey": "running"},
          "startTimeLocal": "2024-02-29 00:15:38", "startTimeGMT": "2024-02-28 23:15:38",
          "duration": 1500, "elapsedDuration": 1800, "distance": 4900, "movingDuration": 1475}
DETAIL = {"activityId": 42, "activityName": "Track <fixture>", "activityTypeDTO": {"typeKey": "track_running"},
          "summaryDTO": {"distance": 5001.25, "elapsedDuration": 1801.1}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def server():
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url=HOST) as client:
            yield client


class FixtureGarmin:
    """Script the wire bytes while exercising the real token, request and JSON code."""
    def __init__(self):
        self.calls = []
        self.listed = [LISTED.copy()]
        self.detail = {**DETAIL, "summaryDTO": DETAIL["summaryDTO"].copy()}
        with BytesIO() as output:
            with ZipFile(output, "w") as original:
                original.writestr("fixture.fit", b"synthetic file bytes")
            self.original = output.getvalue()
        self.original_error = None
        self.detail_error = None
        self.before_detail = lambda: None

    @staticmethod
    def wire(record):
        return (json.dumps(record, indent=2) + "\n").encode()

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        assert url.netloc == "fixture.invalid"
        path = url.path
        self.calls.append(path)
        if path.endswith("/token"):
            assert request.get_header("Authorization") == "ApiKey synthetic-key"
            return BytesIO(b"fixture-bearer")
        assert request.get_header("Authorization") == "Bearer fixture-bearer"
        if path.endswith("/search/activities"):
            query = parse_qs(url.query)
            assert query == {"startDate": [DAY["from"]], "endDate": [DAY["to"]], "activityType": ["running"],
                             "limit": [str(garmin_import.PAGE_SIZE)], "start": ["0"]}
            return BytesIO(self.wire(self.listed))
        if path.startswith("/activity-service/activity/"):
            self.before_detail()
            if self.detail_error:
                raise self.detail_error
            return BytesIO(self.wire(self.detail))
        assert path == "/download-service/files/activity/42"
        if self.original_error:
            raise self.original_error
        return BytesIO(self.original)


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("RUNS_ARCHIVE_PATH", str(tmp_path / "archive"))
    monkeypatch.delenv("RUNS_GARMIN_OFFLINE", raising=False)
    monkeypatch.setattr(runs, "__file__", str(tmp_path / "runs.py"))
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "synthetic-key")
    monkeypatch.setattr(runs, "GARMIN", "https://fixture.invalid")
    upstream = FixtureGarmin()
    monkeypatch.setattr(runs, "build_opener", lambda *args: upstream)
    return upstream


async def submit(client):
    result = await client.post(SUBMIT, json=DAY, headers={"Origin": HOST})
    assert result.status_code == 202
    assert result.headers["location"] == result.json()["report_url"]
    saved = await client.get(result.headers["location"])
    assert saved.status_code == 200
    return saved.json()


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def test_saved_bytes_identity_detail_and_cold_offline_reads(history, tmp_path, monkeypatch):
    async with server() as client:
        assert "Sync Garmin" in (await client.get("/imports")).text
        assert (await client.get("/api/imports")).json() == []
        assert not (tmp_path / "runs.sqlite3").exists()
        report = await submit(client)
        assert report["state"] == "complete" and (report["requested_from"], report["requested_to"]) == tuple(DAY.values())
        assert report["started_at"] and report["finished_at"] and not report["issues"]
        item, = report["activities"]
        assert item["garmin_id"] == "42" and item["outcome"] == "inserted"
        local_id = item["id"]
        detail_path = f"/api/activities/{local_id}"
        detail = (await client.get(detail_path)).json()
        assert (detail["id"], detail["garmin_id"], detail["garmin_url"]) == (
            local_id, "42", "https://connect.garmin.com/modern/activity/42")
        assert (detail["local_date"], detail["start_local"], detail["start_utc"]) == (
            "2024-02-29", "2024-02-29 00:15:38", "2024-02-28T23:15:38+00:00")
        assert (detail["distance_m"], detail["timer_seconds"], detail["elapsed_seconds"],
                detail["moving_seconds"]) == (5001.25, 1500, 1801.1, 1475)
        assert detail["metric_sources"]["timer_seconds"] == "list" and detail["metric_sources"]["distance_m"] == "detail"
        assert detail["sport"] == "track_running"
        raw = {"list": history.wire(history.listed), "detail": history.wire(history.detail), "original": history.original}
        assert len(detail["sources"]) == 3
        for source in detail["sources"]:
            expected = raw[source["kind"]]
            assert source["sha256"] == digest(expected)
            file = tmp_path / "archive" / source["sha256"]
            assert file.read_bytes() == expected and file.stat().st_mode & 0o077 == 0
        assert (tmp_path / "archive").stat().st_mode & 0o077 == 0
        assert (tmp_path / "runs.sqlite3").stat().st_mode & 0o077 == 0
        again = await submit(client)
        assert again["activities"] == [{**item, "outcome": "unchanged"}]
        assert history.calls.count("/download-service/files/activity/42") == 1
        assert [r["id"] for r in (await client.get("/api/imports")).json()] == [again["id"], report["id"]]

    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    monkeypatch.setattr(runs, "load_settings", lambda: pytest.fail("offline read loaded credentials"))
    calls = list(history.calls)
    async with server() as client:
        for _ in range(2):
            assert (await client.get(report["report_url"])).json() == report
            assert f'/activities/{local_id}' in (await client.get(report["html_url"])).text
            assert (await client.get(detail_path)).json() == detail
            html = (await client.get(f"/activities/{local_id}")).text
            assert all(value in html for value in ("Track &lt;fixture&gt;", "5.00 km", "25:00", "30:01",
                                                    "24:35", detail["start_local"], detail["garmin_url"], detail_path))
        assert history.calls == calls


@pytest.mark.parametrize("body", [
    b'{}', b'null', b'[]', b'{', b'{"from":"2024-02-29"}', b'{"to":"2024-02-29"}',
    b'{"from":"2024-02-30","to":"2024-03-01"}', b'{"from":"2024-03-01","to":"2024-02-29"}',
    b'{"from":"20240229","to":"2024-02-29"}', b'{"from":0,"to":null}', b'{"anything":"else"}',
])
async def test_invalid_dates_precede_every_write(history, tmp_path, body):
    async with server() as client:
        result = await client.post(SUBMIT, content=body, headers={"Content-Type": "application/json"})
        assert result.status_code == 422
        assert not history.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("headers", [
    {"Origin": "null"}, {"Origin": "https://example.org"}, {"Origin": "http://127.0.0.1:8002"},
    {"Sec-Fetch-Site": "cross-site"}, {"Referer": "http://localhost.example.org/"},
    {"Host": "attacker.invalid", "Origin": "http://attacker.invalid"},
])
async def test_only_local_browser_writes(history, tmp_path, headers):
    async with server() as client:
        assert (await client.post(SUBMIT, json=DAY, headers=headers)).status_code in (400, 403)
        assert not history.calls and not list(tmp_path.iterdir())


async def test_unknown_local_ids_are_not_found(history):
    async with server() as client:
        await submit(client)
        for prefix in ("/api/activities", "/activities", "/api/imports", "/imports"):
            for unknown in ("999", "garmin-id", "18446744073709551616"):
                assert (await client.get(f"{prefix}/{unknown}")).status_code == 404


async def test_missing_original_warns_and_recheck_recovers_without_a_duplicate(history):
    history.original_error = runs.ProviderError(404, "Garmin original download: HTTP 404. Retry.")
    async with server() as client:
        first = await submit(client)
        item, = first["activities"]
        local_id = item["id"]
        assert first["state"] == "complete"
        warning, = first["issues"]
        assert warning["severity"] == "warning" and warning["item"] == "42" and "retry" in warning["message"].lower()
        assert "Original unavailable" in (await client.get(first["html_url"])).text
        assert "Original unavailable" in (await client.get(f"/activities/{local_id}")).text
        before = (await client.get(f"/api/activities/{local_id}")).json()
        assert before["distance_m"] == 5001.25 and before["warnings"]
        history.original_error = None
        history.detail["summaryDTO"]["distance"] = 5100
        second = await submit(client)
        assert second["activities"] == [{**item, "outcome": "updated"}] and not second["issues"]
        after = (await client.get(f"/api/activities/{local_id}")).json()
        assert not after["warnings"] and after["distance_m"] == 5100
        assert [s["kind"] for s in after["sources"]].count("detail") == 2
        assert (await client.get(first["report_url"])).json() == first


async def test_optional_measurements_do_not_inherit_other_durations(history):
    history.listed[0].pop("movingDuration")
    history.listed[0].pop("duration")
    history.detail["summaryDTO"].update({"duration": -1, "movingDuration": True, "distance": "bad"})
    async with server() as client:
        report = await submit(client)
        local_id = report["activities"][0]["id"]
        activity = (await client.get(f"/api/activities/{local_id}")).json()
        assert activity["timer_seconds"] is None and activity["moving_seconds"] is None
        assert activity["distance_m"] == 4900 and activity["elapsed_seconds"] == 1801.1
        assert (await client.get(f"/activities/{local_id}")).text.count("Not available") == 2


async def test_concurrent_submission_returns_the_active_report(history):
    entered, release = threading.Event(), threading.Event()

    def wait_for_request():
        entered.set()
        assert release.wait(10)

    history.before_detail = wait_for_request
    async with server() as client:
        first = asyncio.create_task(client.post(SUBMIT, json=DAY))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            active = (await client.get("/api/imports")).json()
            assert len(active) == 1 and active[0]["state"] == "running"
            conflict = await client.post(SUBMIT, json=DAY)
            assert conflict.status_code == 409 and conflict.json()["id"] == active[0]["id"]
            assert (await client.get(conflict.json()["report_url"])).json()["state"] == "running"
        finally:
            release.set()
        assert (await first).status_code == 202
        assert len((await client.get("/api/imports")).json()) == 1


async def test_restart_marks_only_unfinished_attempt_interrupted_and_allows_retry(history, tmp_path):
    async with server() as client:
        good = await submit(client)
    # Snapshot the durable state after acceptance and before background completion.
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        unfinished = db.execute("""INSERT INTO imports(requested_from, requested_to, state, started_at)
                                   VALUES (?, ?, 'running', '2024-03-01T06:30:00+00:00')""", tuple(DAY.values())).lastrowid
    async with server() as client:
        interrupted = (await client.get(f"/api/imports/{unfinished}")).json()
        assert interrupted["state"] == "interrupted" and interrupted["finished_at"]
        assert "Recheck these dates" in (await client.get(interrupted["html_url"])).text
        assert (await client.get(good["report_url"])).json() == good
        assert (await submit(client))["activities"][0]["id"] == good["activities"][0]["id"]


async def test_failed_source_link_rolls_back_activity_edit_and_retry_converges(history, tmp_path):
    async with server() as client:
        good = await submit(client)
        item = good["activities"][0]
        path = f'/api/activities/{item["id"]}'
        before = (await client.get(path)).json()
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
            db.execute("""CREATE TRIGGER interrupt_link BEFORE INSERT ON activity_sources
                          WHEN NEW.kind = 'detail' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
        history.detail["summaryDTO"]["distance"] = 5500
        failed = await submit(client)
        assert failed["state"] == "partial" and not failed["activities"]
        assert failed["issues"][0]["item"] == "42" and failed["issues"][0]["step"] == "Save activity"
        assert (await client.get(path)).json() == before
        assert (tmp_path / "archive" / digest(history.wire(history.detail))).read_bytes() == history.wire(history.detail)
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
            db.execute("DROP TRIGGER interrupt_link")
        retried = await submit(client)
        assert retried["activities"] == [{**item, "outcome": "updated"}]
        assert (await client.get(path)).json()["distance_m"] == 5500


async def test_bad_detail_never_replaces_a_good_row_and_other_activities_survive(history):
    async with server() as client:
        good = await submit(client)
        saved = good["activities"][0]
        path = f'/api/activities/{saved["id"]}'
        before = (await client.get(path)).json()
        history.listed = [{"activityId": 51}, *history.listed, *history.listed]
        partial = await submit(client)
        # The server returned the detail for 42 in response to the request for 51.
        assert partial["state"] == "partial" and partial["issues"][0]["item"] == "51"
        assert partial["activities"] == [{**saved, "outcome": "updated"}]
        assert history.calls.count("/activity-service/activity/42") == 2
        history.detail_error = runs.ConnectionProblem("Garmin activity detail timed out. Check your connection and retry.")
        history.listed = [LISTED]
        failed = await submit(client)
        assert not failed["activities"] and "retry" in failed["issues"][0]["message"]
        after = (await client.get(path)).json()
        assert after["distance_m"] == before["distance_m"] and after["id"] == before["id"]
        history.detail_error = None
        assert (await submit(client))["state"] == "complete"


async def test_original_format_and_optional_explicit_moving_time(history, tmp_path):
    history.original = b"<html>private provider response</html>"
    async with server() as client:
        refused = await submit(client)
        assert refused["state"] == "complete" and refused["issues"][0]["step"] == "Original download"
        assert "private provider response" not in json.dumps(refused)
        assert (tmp_path / "archive" / digest(history.original)).read_bytes() == history.original
        history.original = bytes([14, 16]) + bytes(6) + b".FIT" + bytes(4)
        history.detail["summaryDTO"].update({"movingDuration": 1440, "duration": 0})
        retried = await submit(client)
        assert retried["state"] == "complete" and not retried["issues"]
        row = (await client.get(f'/api/activities/{retried["activities"][0]["id"]}')).json()
        assert (row["moving_seconds"], row["timer_seconds"], row["elapsed_seconds"]) == (1440, 0, 1801.1)
        assert {s["format"] for s in row["sources"] if s["kind"] == "original"} == {"fit"}


async def test_offline_submission_fails_cleanly_and_empty_retry_is_durable(history, monkeypatch):
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    async with server() as client:
        offline = await submit(client)
        assert offline["state"] == "failed" and not history.calls
        assert offline["issues"][0]["item"] == "2024-02-29 to 2024-02-29"
        assert "retry" in offline["issues"][0]["message"].lower()
        monkeypatch.delenv("RUNS_GARMIN_OFFLINE")
        history.listed = []
        empty = await submit(client)
        assert empty["state"] == "complete" and not empty["activities"] and not empty["issues"]
        assert (await client.get(offline["report_url"])).json() == offline
