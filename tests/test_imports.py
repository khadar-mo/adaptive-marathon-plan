import asyncio
from contextlib import asynccontextmanager, closing
from datetime import date
import hashlib
from io import BytesIO
import json
import re
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_LZMA, ZipFile

import httpx
import pytest

import app
from fit_fixture import make_fit
import garmin_coverage
import garmin_import
import runs
from storage import Store


pytestmark = pytest.mark.anyio
DAY = {"from": "2024-02-29", "to": "2024-02-29"}
SUBMIT = "/api/imports/garmin"
HOST = "http://127.0.0.1:8001"
LISTED = {"activityId": 42, "activityName": "Fixture", "activityType": {"typeKey": "running"},
          "startTimeLocal": "2024-02-29 00:15:38", "startTimeGMT": "2024-02-28 23:15:38",
          "duration": 1500, "elapsedDuration": 1800, "distance": 4900, "movingDuration": 1475}
DETAIL = {"activityId": 42, "activityName": "Track <fixture>", "activityTypeDTO": {"typeKey": "track_running"},
          "summaryDTO": {"distance": 5001.25, "elapsedDuration": 1801.1}}
FIT = make_fit()


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
        self.extra_details = {}
        with BytesIO() as output:
            with ZipFile(output, "w") as original:
                original.writestr("fixture.fit", FIT)
            self.original = output.getvalue()
        self.default_original = self.original
        self.original_error = None
        self.detail_error = None
        self.before_detail = lambda: None
        self.searches = []
        self.search = self.search_page

    def search_page(self, query):
        start, end = query["startDate"], query["endDate"]
        matches = [row for row in self.listed if start <= row.get("startTimeGMT", row.get("startTimeLocal", start))[:10] <= end]
        offset = int(query["start"])
        return matches[offset:offset + int(query["limit"])]

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
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            self.searches.append(query)
            page = self.search(query)
            return BytesIO(page if isinstance(page, bytes) else self.wire(page))
        if path.startswith("/activity-service/activity/"):
            self.before_detail()
            if self.detail_error:
                raise self.detail_error
            return BytesIO(self.wire(self.extra_details.get(path.rsplit("/", 1)[-1], self.detail)))
        assert path in {f"/download-service/files/activity/{key}" for key in ("42", *self.extra_details)}
        if self.original_error:
            raise self.original_error
        gid = path.rsplit("/", 1)[-1]
        if gid != "42" and self.original == self.default_original:
            return BytesIO(make_fit(serial=int(gid)))
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


async def submit(client, dates=DAY):
    result = await client.post(SUBMIT, json=dates, headers={"Origin": HOST})
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
            assert all(value in html for value in ("Track &lt;fixture&gt;", detail["garmin_url"], detail_path))
            assert dict(re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", html)) == {
                "Recorded local start": detail["start_local"], "Distance": "5.00 km",
                "Watch timer": "25:00", "Elapsed time": "30:01", "Moving time": "24:35",
            }
        assert history.calls == calls


@pytest.mark.parametrize("body", [
    b'{"from":null,"to":null}', b'null', b'[]', b'{', b'{"from":"2024-02-29"}', b'{"to":"2024-02-29"}',
    b'{"from":"2024-02-30","to":"2024-03-01"}', b'{"from":"2024-03-01","to":"2024-02-29"}',
    b'{"from":"20240229","to":"2024-02-29"}', b'{"from":0,"to":null}', b'{"anything":"else"}',
    b'{"from":1e999,"to":"2024-02-29"}', br'{"from":"\ud800","to":"2024-02-29"}',
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
            for unknown in ("999", "garmin-id", "1e0", "18446744073709551616", "9" * 4301):
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
        history.listed = [{**LISTED, "activityId": 51, "activityName": "Another run"}, *history.listed, *history.listed]
        partial = await submit(client)
        # The server returned the detail for 42 in response to the request for 51.
        assert partial["state"] == "partial" and partial["issues"][0]["item"] == "51"
        assert "did not identify the requested activity" in partial["issues"][0]["message"]
        assert "/download-service/files/activity/51" not in history.calls
        assert partial["activities"] == [{**saved, "outcome": "unchanged"}]
        assert history.calls.count("/activity-service/activity/42") == 2
        history.detail_error = runs.ConnectionProblem("Garmin activity detail timed out. Check your connection and retry.")
        history.listed = [LISTED]
        failed = await submit(client)
        assert not failed["activities"] and "retry" in failed["issues"][0]["message"]
        after = (await client.get(path)).json()
        assert after["distance_m"] == before["distance_m"] and after["id"] == before["id"]
        history.detail_error = None
        history.extra_details["51"] = {**DETAIL, "activityId": 51}
        retried = await submit(client)
        assert retried["state"] == "complete" and {a["garmin_id"] for a in retried["activities"]} == {"42", "51"}


async def test_original_format_and_optional_explicit_moving_time(history, tmp_path):
    history.original = b"<html>private provider response</html>"
    async with server() as client:
        refused = await submit(client)
        assert refused["state"] == "complete" and refused["issues"][0]["step"] == "Original download"
        assert "private provider response" not in json.dumps(refused)
        assert (tmp_path / "archive" / digest(history.original)).read_bytes() == history.original
        history.original = FIT
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


async def test_first_import_protects_existing_database_and_live_sidecars(history, tmp_path):
    path = tmp_path / "runs.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE earlier_data (id INTEGER)")
        files = [path, tmp_path / "runs.sqlite3-wal", tmp_path / "runs.sqlite3-shm"]
        for file in files:
            file.chmod(0o644)
        async with server() as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.post(SUBMIT, json={"to": DAY["to"]})).status_code == 422
            assert path.stat().st_mode & 0o777 == 0o644
            assert (await submit(client))["state"] == "complete"
            assert all(file.stat().st_mode & 0o077 == 0 for file in files)
            assert db.execute("SELECT name FROM activities").fetchone() == ("Track <fixture>",)


@pytest.mark.parametrize("invalid", ["empty zip", "empty member", "damaged member", "truncated fit",
                                      "wrapped truncated fit", "invalid member name"])
async def test_unusable_original_is_archived_warned_and_retried(history, tmp_path, invalid):
    good = history.original
    if invalid in ("empty zip", "empty member"):
        with BytesIO() as stream:
            with ZipFile(stream, "w") as zipped:
                if invalid == "empty member":
                    zipped.writestr("empty.fit", b"")
            history.original = stream.getvalue()
    elif invalid == "damaged member":
        history.original = good.replace(FIT, b"x" * len(FIT))
    elif invalid in ("wrapped truncated fit", "invalid member name"):
        with BytesIO() as stream:
            with ZipFile(stream, "w") as zipped:
                zipped.writestr("é.fit", FIT[:-4] if invalid == "wrapped truncated fit" else FIT)
            history.original = stream.getvalue()
        if invalid == "invalid member name":
            history.original = history.original.replace("é.fit".encode(), b"\xff\xff.fit")
    else:
        history.original = FIT[:-4]
    async with server() as client:
        result = await submit(client)
        assert result["state"] == "complete" and result["issues"][0]["step"] == "Original download"
        item, = result["activities"]
        path = f'/api/activities/{item["id"]}'
        before = (await client.get(path)).json()
        assert before["warnings"] and "original" not in {s["kind"] for s in before["sources"]}
        assert (tmp_path / "archive" / digest(history.original)).read_bytes() == history.original
        history.original = good
        recovered = await submit(client)
        assert recovered["activities"] == [{**item, "outcome": "updated"}] and not recovered["issues"]
        assert history.calls.count("/download-service/files/activity/42") == 2
        assert not (await client.get(path)).json()["warnings"]


async def test_page_reorder_preserves_activity_outcomes_and_recorded_precision(history):
    history.listed[0]["startTimeLocal"] = "2024-02-29T12:11"
    history.listed.append({**LISTED, "activityId": 51})
    history.extra_details["51"] = {**DETAIL, "activityId": 51}
    async with server() as client:
        first = await submit(client)
        assert len(first["activities"]) == 2
        history.listed.reverse()
        repeat = await submit(client)
        assert repeat["activities"] == [{**item, "outcome": "unchanged"} for item in first["activities"]]
        local_id = first["activities"][0]["id"]
        detail = (await client.get(f"/api/activities/{local_id}")).json()
        assert detail["start_local"] == "2024-02-29 12:11"
        assert [s["kind"] for s in detail["sources"]].count("list") == 2
        assert "<dd>2024-02-29 12:11</dd>" in (await client.get(f"/activities/{local_id}")).text


async def test_final_report_waits_for_temporary_sqlite_lock(history, tmp_path, monkeypatch):
    # Acquire a real writer lock just before the background task commits its outcome.
    entered, proceed = threading.Event(), threading.Event()
    finish = Store.finish
    connect = sqlite3.connect

    def delayed_finish(self, import_id, state):
        entered.set()
        assert proceed.wait(5)
        finish(self, import_id, state)

    monkeypatch.setattr(Store, "finish", delayed_finish)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **{**kwargs, "timeout": 0.02}))
    async with server() as client:
        pending = asyncio.create_task(client.post(SUBMIT, json=DAY))
        assert await asyncio.to_thread(entered.wait, 5)
        with closing(connect(tmp_path / "runs.sqlite3")) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            proceed.set()
            await asyncio.sleep(0.15)
            assert not pending.done()
            conflict = await client.post(SUBMIT, json=DAY)
            assert conflict.status_code == 409 and conflict.json()["id"] == 1
            blocker.rollback()
        response = await pending
        assert (await client.get(response.json()["report_url"])).json()["state"] == "complete"
        assert (await submit(client))["state"] == "complete"


async def test_restored_storage_must_be_private_and_reused_objects_are_protected(history, tmp_path):
    directory = tmp_path / "archive"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    listed = history.wire(history.listed)
    restored = directory / digest(listed)
    restored.write_bytes(listed)
    restored.chmod(0o644)
    async with server() as client:
        refused = await submit(client)
        assert refused["state"] == "failed" and not refused["activities"]
        assert "0700" in refused["issues"][0]["message"]
        directory.chmod(0o700)
        assert (await submit(client))["state"] == "complete"
        assert restored.stat().st_mode & 0o077 == 0


async def test_cached_unusable_original_is_reclassified_and_replaced(history, tmp_path):
    async with server() as client:
        history.original = FIT[:-4]
        first = await submit(client)
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
            db.execute("UPDATE activity_sources SET kind='original', format='fit' WHERE kind='original_error'")
        history.original = FIT
        result = await submit(client)
        item, = result["activities"]
        assert item["id"] == first["activities"][0]["id"] and not result["issues"]
        assert history.calls.count("/download-service/files/activity/42") == 2
        row = (await client.get(f'/api/activities/{item["id"]}')).json()
        assert {(s["sha256"], s["kind"]) for s in row["sources"] if s["format"] == "fit"} == {
            (digest(FIT), "original"), (digest(FIT[:-4]), "original_error"),
        }


async def test_retry_recovers_worker_that_could_not_persist_final_state(history, monkeypatch):
    write_report = Store.write_report

    def unavailable(self, statement, parameters, **options):
        if statement.startswith("UPDATE imports"):
            raise OSError("fixture storage unavailable")
        write_report(self, statement, parameters, **options)

    async with server() as client:
        with monkeypatch.context() as temporarily:
            temporarily.setattr(Store, "write_report", unavailable)
            accepted = (await client.post(SUBMIT, json=DAY)).json()
        retry = await submit(client)
        assert retry["state"] == "complete" and retry["activities"][0]["outcome"] == "unchanged"
        assert (await client.get(accepted["report_url"])).json()["state"] == "interrupted"


async def test_securing_existing_database_preserves_cross_process_write_lock(history, tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite3"
    connect = sqlite3.connect
    independent_writer = """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0.02)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as exc:
    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
        raise
    print('locked')
else:
    db.rollback()
    print('acquired')
finally:
    db.close()
"""

    def lock_status():
        return subprocess.check_output([sys.executable, "-c", independent_writer, str(path)], text=True).strip()

    async with server() as client:
        await submit(client)
        with closing(connect(path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE activities SET name='local edit' WHERE id=1")
            assert lock_status() == "locked"
            path.chmod(0o644)
            monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **{**kwargs, "timeout": 0.02}))
            assert (await client.post(SUBMIT, json=DAY)).status_code == 503
            assert path.stat().st_mode & 0o077 == 0
            assert lock_status() == "locked"
            holder.commit()
        assert lock_status() == "acquired"
        assert (await client.get("/api/activities/1")).json()["name"] == "local edit"


async def test_broken_zip_codec_warns_and_other_runs_are_saved(history):
    with BytesIO() as stream:
        with ZipFile(stream, "w", compression=ZIP_LZMA) as zipped:
            zipped.writestr("fixture.fit", FIT)
        damaged = bytearray(stream.getvalue())
    # The ZIP LZMA stream starts with two version bytes and a two-byte properties length.
    payload_start = damaged.index(b"fixture.fit") + len(b"fixture.fit")
    damaged[payload_start + 4] = 255
    history.original = bytes(damaged)
    history.listed.append({**LISTED, "activityId": 51})
    history.extra_details["51"] = {**DETAIL, "activityId": 51}
    async with server() as client:
        report = await submit(client)
        assert report["state"] == "complete"
        assert {a["garmin_id"] for a in report["activities"]} == {"42", "51"}
        assert [(i["step"], i["severity"]) for i in report["issues"]] == [("Original download", "warning")] * 2

OCTOBER = {"from": "2026-10-01", "to": "2026-10-31"}


def add_run(history, number, local, sport="running", utc=None):
    record = {**LISTED, "activityId": number, "activityType": {"typeKey": sport},
              "startTimeLocal": local + " 00:15:00", "startTimeGMT": (utc or local) + " 23:15:00"}
    history.listed.append(record)
    history.extra_details[str(number)] = {"activityId": number, "activityTypeDTO": {"typeKey": sport},
                                           "summaryDTO": {"distance": 1000 + number}}
    return record


async def test_october_pages_boundaries_and_explicit_detail_edits(history, monkeypatch):
    monkeypatch.setattr(garmin_import, "PAGE_SIZE", 5)
    history.listed = []
    types = ["running", "trail_running", "treadmill_running", "track_running", "virtual_run"]
    for n in range(11, 24):
        add_run(history, n, "2026-10-01" if n == 11 else "2026-10-25", types[n % 5],
                utc="2026-09-30" if n == 11 else None)
    history.listed.insert(5, history.listed[0])
    add_run(history, 24, "2026-11-01", utc="2026-10-31")
    add_run(history, 25, "2026-10-31", "cycling")
    add_run(history, 26, "2026-10-31", utc="2026-11-01")
    expected = {str(n) for n in [*range(11, 24), 26]}
    async with server() as client:
        first = await submit(client, OCTOBER)
        scan, = first["scans"]
        assert first["state"] == "complete" and first["counts"] == {"inserted": 14, "updated": 0, "unchanged": 0}
        assert {a["garmin_id"] for a in first["activities"]} == expected
        assert [q["start"] for q in history.searches] == ["0", "5", "10", "15", "17"]
        assert all(q["startDate"] == "2026-09-30" and q["endDate"] == "2026-11-01"
                   and "activityType" not in q for q in history.searches)
        assert scan["checked_ranges"] == [OCTOBER] and scan["coverage"] == "checked"
        assert (scan["earliest_saved"], scan["latest_saved"]) == tuple(OCTOBER.values())
        assert (scan["unique_ids_found"], scan["excluded"], scan["originals_available"], scan["originals_requested"]) == (14, 2, 14, 14)
        ids = {a["garmin_id"]: a["id"] for a in first["activities"]}
        before = (await client.get(f'/api/activities/{ids["11"]}')).json()
        assert before["local_date"] == "2026-10-01" and before["start_utc"].startswith("2026-09-30")

        history.extra_details["11"]["summaryDTO"]["distance"] = 2026.5
        repeated = await submit(client, OCTOBER)
        assert {a["garmin_id"]: a["id"] for a in repeated["activities"]} == ids
        assert repeated["counts"] == {"inserted": 0, "updated": 1, "unchanged": 13}
        assert repeated["scans"][0]["originals_requested"] == 0
        assert all(history.calls.count("/activity-service/activity/" + key) == 2 for key in expected)
        assert all(history.calls.count("/download-service/files/activity/" + key) == 1 for key in expected)
        after = (await client.get(f'/api/activities/{ids["11"]}')).json()
        assert after["distance_m"] == 2026.5 and [s["kind"] for s in after["sources"]].count("detail") == 2
        assert all(source in after["sources"] for source in before["sources"])
        saved_total = 0
        for key in ids.values():
            saved_total += (await client.get(f"/api/activities/{key}")).json()["distance_m"]
        assert saved_total == sum(1000 + int(key) for key in expected - {"11"}) + 2026.5
        assert (await client.get(first["report_url"])).json() == first
        history.listed = []
        empty = await submit(client, OCTOBER)
        assert empty["scans"][0]["coverage"] == "checked_empty" and empty["activities"] == []
        assert "No runs returned" in (await client.get(empty["html_url"])).text
        assert (await client.get(f'/api/activities/{ids["11"]}')).json() == after


@pytest.mark.parametrize("failure", ["overlap", "timeout", "malformed"])
async def test_incomplete_later_page_survives_restart_and_converges(history, monkeypatch, tmp_path, failure):
    history.listed = []
    monkeypatch.setattr(garmin_import, "PAGE_SIZE", 2)
    for n in (11, 12, 13):
        add_run(history, n, "2026-10-07")

    def interrupted(query):
        if query["start"] == "2":
            if failure == "timeout":
                raise runs.ConnectionProblem("Garmin activity search timed out. Retry.")
            if failure == "malformed":
                return b'{"activities": []}'
            return list(reversed(history.listed[:2]))
        return history.search_page(query)

    history.search = interrupted
    async with server() as client:
        partial = await submit(client, OCTOBER)
        assert partial["state"] == "partial" and partial["scans"][0]["coverage"] == "unchecked"
        assert partial["scans"][0]["checked_ranges"] == [] and "Offset 2" in partial["issues"][0]["message"]
        assert {a["garmin_id"] for a in partial["activities"]} == {"11", "12"}
        assert "No runs returned" not in (await client.get(partial["html_url"])).text
    history.search = history.search_page
    async with server() as client:
        retried = await submit(client, OCTOBER)
        assert retried["state"] == "complete" and retried["counts"] == {"inserted": 1, "updated": 0, "unchanged": 2}
        assert (await client.get(partial["report_url"])).json() == partial

        async def records(report):
            result = {}
            for activity in report["activities"]:
                detail = (await client.get(f'/api/activities/{activity["id"]}')).json()
                result[activity["garmin_id"]] = (detail["id"], detail["metric_sources"], detail["distance_m"],
                                                [(s["kind"], s["sha256"]) for s in detail["sources"]])
            return result

        recovered = await records(retried)
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "clean.sqlite3"))
    monkeypatch.setenv("RUNS_ARCHIVE_PATH", str(tmp_path / "clean-archive"))
    async with server() as client:
        clean = await submit(client, OCTOBER)
        assert clean["state"] == "complete" and await records(clean) == recovered


async def test_contiguous_coverage_normal_window_and_cached_detail(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 1))
    history.listed = []
    add_run(history, 71, "2026-10-31")
    async with server() as client:
        await submit(client, OCTOBER)
        normal = await submit(client, {})
        assert (normal["requested_from"], normal["requested_to"]) == ("2026-06-01", "2026-11-01")
        assert normal["scans"][0]["mode"] == "normal" and normal["counts"]["unchanged"] == 1
        assert history.calls.count("/activity-service/activity/71") == 1

        history.listed[0]["distance"] = 7000
        history.extra_details["71"]["summaryDTO"]["distance"] = 7001
        monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 5))
        catchup = await submit(client, {})
        assert (catchup["requested_from"], catchup["requested_to"]) == ("2026-10-19", "2026-11-05")
        assert catchup["counts"]["updated"] == 1 and history.calls.count("/activity-service/activity/71") == 2


async def test_known_metadata_gap_blocks_baseline_and_absent_id_is_retried(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 7, 4))
    history.listed = []
    add_run(history, 21, "2026-06-20")
    history.extra_details["21"] = {"activityId": 999}
    async with server() as client:
        partial = await submit(client, {"from": "2026-06-01", "to": "2026-06-30"})
        scan, = partial["scans"]
        assert scan["coverage"] == "missing_activity" and scan["missing_activity_ids"] == ["21"]
        assert scan["checked_ranges"] == [{"from": "2026-06-01", "to": "2026-06-19"},
                                          {"from": "2026-06-21", "to": "2026-06-30"}]
    history.listed = []
    history.extra_details["21"] = {"activityId": 21, "activityTypeDTO": {"typeKey": "running"}}
    async with server() as client:
        recovered = await submit(client, {})
        assert recovered["requested_from"] == "2026-06-06" and recovered["state"] == "complete"
        assert [a["garmin_id"] for a in recovered["activities"]] == ["21"]
        assert (await submit(client, {}))["requested_from"] == "2026-06-21"


async def test_original_warning_keeps_coverage_and_is_retried_outside_normal_window(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 16))
    history.listed = []
    add_run(history, 41, "2026-06-01")
    history.original_error = runs.ConnectionProblem("Original download unavailable. Retry.")
    async with server() as client:
        first = await submit(client, {})
        scan, = first["scans"]
        assert first["state"] == "complete" and scan["coverage"] == "checked"
        assert scan["missing_original_ids"] == ["41"] and scan["originals_requested"] == 1
        assert scan["checked_ranges"] == [{"from": "2026-06-01", "to": "2026-11-16"}]
    history.original_error = None
    history.listed = []
    async with server() as client:
        retry = await submit(client, {})
        current, older = retry["scans"]
        assert (retry["requested_from"], retry["requested_to"]) == ("2026-11-03", "2026-11-16")
        assert current["coverage"] == "checked_empty" and older["mode"] == "retry"
        assert older["from"] == older["to"] == "2026-06-01" and older["missing_original_ids"] == []
        assert older["originals_available"] == older["originals_requested"] == 1
        report_html = (await client.get(retry["html_url"])).text
        assert '/imports?from=2026-06-01&amp;to=2026-06-01' in report_html
        assert retry["activities"] == [{**first["activities"][0], "outcome": "updated"}]
        assert len((await submit(client, {}))["scans"]) == 1


async def test_upgrade_keeps_pr9_sources_and_retries_its_unavailable_original(history, tmp_path, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 16))
    history.original_error = runs.ConnectionProblem("Original download unavailable. Retry.")
    async with server() as client:
        old = await submit(client)
        original_id = old["activities"][0]["id"]
        detail = (await client.get(f"/api/activities/{original_id}")).json()
    # Leave the exact tables/columns of the PR #9 database, including its evidence.
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.execute("DROP TABLE garmin_scan_items")
        db.execute("DROP TABLE garmin_scans")
    history.original_error = None
    async with server() as client:
        legacy = (await client.get(old["report_url"])).json()
        assert legacy["activities"] == old["activities"]
        scan, = legacy["scans"]
        assert scan["mode"] == "legacy" and (scan["search_from"], scan["search_to"]) == tuple(DAY.values())
        assert scan["pages_read"] is None and scan["originals_requested"] is None and scan["checked_ranges"] == []
        assert (await client.get(f"/api/activities/{original_id}")).json() == detail
        upgraded = await submit(client, {})
        assert upgraded["requested_from"] == "2026-06-01" and upgraded["state"] == "complete"
        assert upgraded["scans"][1]["from"] == DAY["from"]
        assert upgraded["activities"] == [{**old["activities"][0], "outcome": "updated"}]
        saved = (await client.get(f"/api/activities/{original_id}")).json()
        assert not saved["warnings"] and all(s in saved["sources"] for s in detail["sources"])
        assert (await client.get(old["report_url"])).json() == legacy

async def test_detail_reserialization_archived_unchanged_semantic_edits_updated(history, tmp_path):
    history.detail["extra"] = {"cadence": [1, 2], "enabled": True}
    async with server() as client:
        first = await submit(client)
        activity_id = first["activities"][0]["id"]
        original_bytes = history.wire(history.detail)
        history.wire = lambda record: json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        different_bytes = history.wire(history.detail)
        assert different_bytes != original_bytes and json.loads(different_bytes) == json.loads(original_bytes)
        repeat = await submit(client)
        assert repeat["activities"] == [{**first["activities"][0], "outcome": "unchanged"}]
        assert repeat["counts"] == {"inserted": 0, "updated": 0, "unchanged": 1}
        detail = (await client.get(f"/api/activities/{activity_id}")).json()
        assert {s["sha256"] for s in detail["sources"] if s["kind"] == "detail"} == {
            digest(original_bytes), digest(different_bytes)}
        assert all((tmp_path / "archive" / digest(data)).read_bytes() == data for data in (original_bytes, different_bytes))
        history.detail["extra"]["enabled"] = 1
        assert (await submit(client))["counts"]["updated"] == 1
        history.detail["extra"]["enabled"] = True
        assert (await submit(client))["counts"]["updated"] == 1  # revert to an already archived version
        history.detail["summaryDTO"]["distance"] = 7123
        assert (await submit(client))["counts"]["updated"] == 1
        assert (await client.get(f"/api/activities/{activity_id}")).json()["distance_m"] == 7123
        assert history.calls.count("/download-service/files/activity/42") == 1


async def test_future_request_does_not_credit_unelapsed_dates(history, monkeypatch):
    import storage

    history.listed = []
    def at(day):
        monkeypatch.setattr(garmin_coverage, "today", lambda: date.fromisoformat(day))
        monkeypatch.setattr(storage, "now", lambda: day + "T12:00:00+00:00")
    async with server() as client:
        at("2026-11-02")
        await submit(client, {})
        future = await submit(client, {"from": "2026-11-01", "to": "2026-11-30"})
        assert future["scans"][0]["checked_ranges"] == [{"from": "2026-11-01", "to": "2026-11-02"}]
        assert future["state"] == "partial"
        add_run(history, 111, "2026-11-10")
        at("2026-12-05")
        later = await submit(client, {})
        assert later["requested_from"] == "2026-10-20" and later["state"] == "complete"
        assert [a["garmin_id"] for a in later["activities"]] == ["111"]


async def test_corrected_date_and_original_request_survive_activity_rollback(history, tmp_path, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 7, 1))
    history.listed = []
    async with server() as client:
        await submit(client, DAY)
        add_run(history, 28, "2026-06-25")
        history.extra_details["28"]["summaryDTO"]["startTimeLocal"] = "2026-06-02 08:00:00"
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
            db.execute("CREATE TRIGGER stop_save BEFORE INSERT ON activities BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        partial = await submit(client, {"from": "2026-06-01", "to": "2026-06-30"})
        scan, = partial["scans"]
        assert scan["originals_requested"] == 1 and scan["missing_activity_ids"] == ["28"]
        assert scan["checked_ranges"] == [{"from": "2026-06-01", "to": "2026-06-01"},
                                          {"from": "2026-06-03", "to": "2026-06-30"}]
        with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
            db.execute("DROP TRIGGER stop_save")
        history.listed = []
        recovered = await submit(client, {})
        assert recovered["requested_from"] == "2026-06-01" and recovered["counts"]["inserted"] == 1
        activity = (await client.get(f'/api/activities/{recovered["activities"][0]["id"]}')).json()
        assert activity["local_date"] == "2026-06-02"


async def test_resolved_widened_boundary_stops_retrying(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 2))
    history.listed = []
    add_run(history, 803, "2026-05-31")
    history.detail_error = runs.ConnectionProblem("Detail timeout. Retry.")
    async with server() as client:
        partial = await submit(client, {})
        assert partial["scans"][0]["missing_activity_ids"] == ["803"] and partial["scans"][0]["checked_ranges"] == []
        history.detail_error = None
        recovered = await submit(client, {})
        assert recovered["scans"][0]["coverage"] == "checked_empty" and not recovered["activities"]
        for _ in range(2):
            later = await submit(client, {})
            assert len(later["scans"]) == 1 and later["requested_from"] == "2026-10-20"


async def test_sport_date_and_exclusions_follow_explicit_detail(history):
    history.listed = []
    listed = add_run(history, 49, "2026-10-31", sport="cycling")
    history.extra_details["49"]["activityTypeDTO"] = {"typeKey": "trail_running"}
    async with server() as client:
        good = await submit(client, OCTOBER)
        path = f'/api/activities/{good["activities"][0]["id"]}'
        assert (await client.get(path)).json()["sport"] == "trail_running"
        history.extra_details["49"]["summaryDTO"]["startTimeLocal"] = "2026-11-01 00:12:00"
        moved = await submit(client, OCTOBER)
        assert moved["activities"] == [] and moved["scans"][0]["excluded"] == 1
        assert (await client.get(path)).json()["local_date"] == "2026-11-01"
        history.extra_details["49"]["activityTypeDTO"]["typeKey"] = "cycling"
        assert (await submit(client, OCTOBER))["activities"] == []
        assert (await client.get(path)).json()["sport"] == "cycling"
        listed["activityType"] = {"typeKey": " "}
        history.extra_details["49"]["activityTypeDTO"]["typeKey"] = ""
        history.extra_details["49"]["summaryDTO"]["startTimeLocal"] = "2026-10-31 00:12:00"
        missing = await submit(client, OCTOBER)
        assert missing["scans"][0]["coverage"] == "missing_activity" and missing["scans"][0]["missing_metadata"] == 1


async def test_unidentified_row_is_not_checked_empty(history):
    history.listed = [{"startTimeLocal": "2026-10-01 00:00:00", "activityType": {"typeKey": "running"}}]
    async with server() as client:
        report = await submit(client, OCTOBER)
        scan, = report["scans"]
        assert report["state"] == "partial" and scan["unidentified_records"] == scan["missing_metadata"] == 1
        assert scan["checked_ranges"] == [] and scan["coverage"] == "missing_activity"


async def test_corrupt_original_can_be_unavailable_then_restored(history, tmp_path):
    async with server() as client:
        first = await submit(client)
        ident = first["activities"][0]["id"]
        target = tmp_path / "archive" / digest(history.original)
        target.write_bytes(b"damaged disk contents")
        history.original_error = runs.ConnectionProblem("Original unavailable. Retry.")
        history.detail["summaryDTO"]["distance"] = 7345
        incomplete = await submit(client)
        assert incomplete["state"] == "complete" and incomplete["scans"][0]["missing_original_ids"] == ["42"]
        assert (await client.get(f"/api/activities/{ident}")).json()["distance_m"] == 7345
        history.original_error = None
        repaired = await submit(client)
        assert repaired["scans"][0]["originals_requested"] == 1 and not repaired["issues"]
        assert target.read_bytes() == history.original


async def test_initial_scan_read_failure_does_not_stick_running(history, monkeypatch):
    async with server() as client:
        with monkeypatch.context() as patch:
            def unavailable(*args):
                raise sqlite3.OperationalError("fixture lock")
            patch.setattr(Store, "scans", unavailable)
            failed = await submit(client)
        assert failed["state"] == "failed" and failed["issues"][0]["step"] == "Import storage"
        assert (await submit(client))["state"] == "complete"

async def test_normal_uses_authoritative_sport_and_skips_undated_cycling(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 1))
    history.listed = []
    add_run(history, 70, "2026-06-02", sport="cycling")
    history.extra_details["70"]["activityTypeDTO"] = {"typeKey": "trail_running"}
    cyclist = add_run(history, 71, "2026-10-20", sport="cycling")
    del cyclist["startTimeLocal"]
    async with server() as client:
        first = await submit(client, {})
        assert first["state"] == "complete" and [a["garmin_id"] for a in first["activities"]] == ["70"]
        assert first["scans"][0]["excluded"] == 1 and first["scans"][0]["missing_metadata"] == 0
        assert (await submit(client, OCTOBER))["scans"][0]["coverage"] == "checked_empty"
        assert (await submit(client, {}))["state"] == "complete"
        assert history.calls.count("/activity-service/activity/71") == 2


async def test_corrected_date_is_retained_when_required_sport_is_missing(history, monkeypatch):
    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 7, 1))
    history.listed = []
    add_run(history, 73, "2026-06-25", sport="")
    history.extra_details["73"]["summaryDTO"]["startTimeLocal"] = "2026-06-02 09:00:00"
    async with server() as client:
        bad = await submit(client, {"from": "2026-06-01", "to": "2026-06-30"})
        assert bad["scans"][0]["checked_ranges"] == [{"from": "2026-06-01", "to": "2026-06-01"},
                                                     {"from": "2026-06-03", "to": "2026-06-30"}]
        history.listed = []
        history.extra_details["73"]["activityTypeDTO"]["typeKey"] = "running"
        retry = await submit(client, {})
        assert retry["requested_from"] == "2026-06-01" and retry["state"] == "complete"
        assert [a["garmin_id"] for a in retry["activities"]] == ["73"]


async def test_corrupt_cache_replaced_with_a_different_original(history, tmp_path):
    async with server() as client:
        first = await submit(client)
        old_digest = digest(history.original)
        (tmp_path / "archive" / old_digest).write_bytes(b"corrupted after the first import")
        history.original = FIT
        retry = await submit(client)
        assert retry["state"] == "complete" and retry["scans"][0]["originals_requested"] == 1
        detail = (await client.get(f'/api/activities/{first["activities"][0]["id"]}')).json()
        assert {s["sha256"] for s in detail["sources"] if s["kind"] == "original"} == {digest(FIT)}
        assert {s["sha256"] for s in detail["sources"] if s["kind"] == "original_unavailable"} == {old_digest}
        assert (await submit(client))["counts"]["unchanged"] == 1


async def test_fit_only_reports_never_seed_garmin_coverage_or_retries(history, tmp_path, monkeypatch):
    import storage

    monkeypatch.setattr(garmin_coverage, "today", lambda: date(2026, 11, 1))
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.executescript(storage.SCHEMA)
        db.executemany("""INSERT INTO imports(requested_from, requested_to, started_at, state, source)
                          VALUES (?, ?, '2026-10-30T12:00:00+00:00', 'partial', 'fit')""",
                       [(None, None), ("2026-05-01", "2026-05-01")])
        db.execute("INSERT INTO activities(id, sport, metric_sources) VALUES (301, 'running', '{}')")
        db.execute("INSERT INTO import_activities VALUES (1, 301, 'inserted')")
    async with server() as client:
        assert "FIT import" in (await client.get("/imports")).text
        assert '/activities/301' in (await client.get('/imports/1')).text
        assert (await client.get("/api/activities/301")).json()["garmin_url"] is None
        detail = await client.get("/activities/301")
        assert detail.status_code == 200 and "Garmin Connect activity" not in detail.text
        for url in ("/api/imports/1", "/api/imports/2"):
            row = (await client.get(url)).json()
            assert row["scans"] == []
            html = await client.get(row["html_url"])
            assert html.status_code == 200 and "FIT import" in html.text and "Recheck these dates" not in html.text
        normal = await submit(client, {})
        assert normal["requested_from"] == "2026-06-01" and len(normal["scans"]) == 1
        assert normal["scans"][0]["coverage"] == "checked_empty"
        assert "FIT import" in (await client.get("/imports")).text
        for url in ("/api/imports/1", "/api/imports/2"):
            row = (await client.get(url)).json()
            assert row["scans"] == []
            html = await client.get(row["html_url"])
            assert html.status_code == 200 and "FIT import" in html.text and "Recheck these dates" not in html.text

async def test_deeply_nested_valid_detail_repeat_keeps_other_runs(history):
    detail_value = "fixture"
    for _ in range(680):
        detail_value = [detail_value]
    history.detail["extra"] = detail_value
    history.wire = lambda record: json.dumps(record, separators=(",", ":")).encode()
    async with server() as client:
        assert (await submit(client))["counts"]["inserted"] == 1
        add_run(history, 51, DAY["from"])
        history.wire = lambda record: (json.dumps(record, separators=(",", ":")) + " ").encode()
        repeat = await submit(client)
        assert repeat["state"] == "complete" and repeat["counts"] == {"inserted": 1, "updated": 0, "unchanged": 1}


async def test_upgrade_preserves_reverted_pr9_detail_outcome(history, tmp_path):
    async with server() as client:
        first = await submit(client)
        distance = history.detail["summaryDTO"]["distance"]
        history.detail["summaryDTO"]["distance"] = 6900
        await submit(client)
        history.detail["summaryDTO"]["distance"] = distance
        await submit(client)
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.execute("DROP TABLE garmin_scan_items")
        db.execute("DROP TABLE garmin_scans")
    async with server() as client:
        history.wire = lambda row: json.dumps(row, sort_keys=True).encode()
        upgraded = await submit(client)
        assert upgraded["activities"] == [{**first["activities"][0], "outcome": "unchanged"}]
        history.detail["metadata"] = {"fixture": "new semantic version"}
        assert (await submit(client))["counts"]["updated"] == 1


async def test_normal_sync_uses_london_today_across_midnight(history, monkeypatch):
    from datetime import datetime, timezone

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(garmin_coverage, "datetime", FixedClock)
    async with server() as client:
        report = await submit(client, {})
        assert (report["requested_from"], report["requested_to"]) == ("2026-06-01", "2026-06-02")

async def test_legacy_unique_last_acquisition_detects_raw_field_revert(history, tmp_path, monkeypatch):
    import storage

    async with server() as client:
        history.detail["extra"] = "A"
        monkeypatch.setattr(storage, "now", lambda: "2026-11-01T12:00:00+00:00")
        first = await submit(client)
        history.detail["extra"] = "B"
        monkeypatch.setattr(storage, "now", lambda: "2026-11-08T12:00:00+00:00")
        assert (await submit(client))["counts"]["updated"] == 1
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.execute("DROP TABLE garmin_scan_items")
        db.execute("DROP TABLE garmin_scans")
    async with server() as client:
        history.detail["extra"] = "A"
        history.wire = lambda row: json.dumps(row, sort_keys=True).encode()
        monkeypatch.setattr(storage, "now", lambda: "2026-11-15T12:00:00+00:00")
        upgraded = await submit(client)
        assert upgraded["activities"] == [{**first["activities"][0], "outcome": "updated"}]
        assert (await submit(client))["counts"]["unchanged"] == 1


async def test_unreadable_original_and_restoration_preserve_acquisition_time(history, monkeypatch):
    import storage

    target = digest(history.original)
    first_acquired = "2026-11-01T12:00:00+00:00"
    async with server() as client:
        monkeypatch.setattr(storage, "now", lambda: first_acquired)
        first = await submit(client)
        path = f'/api/activities/{first["activities"][0]["id"]}'
        previous_read = storage.Archive.read
        def unreadable(self, sha):
            if sha == target:
                raise PermissionError("fixture unreadable file")
            return previous_read(self, sha)
        monkeypatch.setattr(storage, "now", lambda: "2026-11-08T12:00:00+00:00")
        history.detail["summaryDTO"]["distance"] = 7012
        history.original_error = runs.ConnectionProblem("Fixture original unavailable. Retry.")
        with monkeypatch.context() as patch:
            patch.setattr(storage.Archive, "read", unreadable)
            missing = await submit(client)
        detail = (await client.get(path)).json()
        assert missing["state"] == "complete" and missing["scans"][0]["originals_requested"] == 1
        assert detail["distance_m"] == 7012 and detail["warnings"]
        held, = [s for s in detail["sources"] if s["kind"] == "original_unavailable"]
        assert held["sha256"] == target and held["acquired_at"] == first_acquired
        monkeypatch.setattr(storage, "now", lambda: "2026-11-15T12:00:00+00:00")
        restored = await submit(client)
        assert restored["scans"][0]["originals_requested"] == 0 and restored["scans"][0]["originals_available"] == 1
        recovered, = [s for s in (await client.get(path)).json()["sources"] if s["kind"] == "original"]
        assert recovered == {**held, "kind": "original"}

@pytest.mark.parametrize("invalid_cache", ["directory", "dangling_link"])
async def test_nonregular_optional_original_never_blocks_metadata(history, tmp_path, invalid_cache):
    async with server() as client:
        first = await submit(client)
        ident = first["activities"][0]["id"]
        target = tmp_path / "archive" / digest(history.original)
        target.unlink()
        if invalid_cache == "directory":
            target.mkdir()
        else:
            target.symlink_to(tmp_path / "must-not-be-created")
        history.detail["summaryDTO"]["distance"] = 7012
        history.original = FIT
        repeat = await submit(client)
        assert repeat["state"] == "complete" and repeat["scans"][0]["originals_requested"] == 1
        activity = (await client.get(f"/api/activities/{ident}")).json()
        assert activity["distance_m"] == 7012 and not activity["warnings"]
        assert {s["sha256"] for s in activity["sources"] if s["kind"] == "original"} == {digest(FIT)}
        assert target.is_dir() if invalid_cache == "directory" else target.is_symlink()
        assert not (tmp_path / "must-not-be-created").exists()


async def test_legacy_duplicate_original_roles_keep_earliest_acquisition(history, tmp_path, monkeypatch):
    import storage

    first_time = "2026-11-01T12:00:00+00:00"
    monkeypatch.setattr(storage, "now", lambda: "2026-11-08T12:00:00+00:00")
    async with server() as client:
        first = await submit(client)
    with closing(sqlite3.connect(tmp_path / "runs.sqlite3")) as db, db:
        db.execute("DROP TABLE garmin_scan_items")
        db.execute("DROP TABLE garmin_scans")
        db.execute("""INSERT INTO activity_sources(activity_id, kind, sha256, format, acquired_at)
                      SELECT activity_id, 'original_error', sha256, format, ? FROM activity_sources
                      WHERE kind='original'""", (first_time,))
    async with server() as client:
        repeated = await submit(client)
        assert repeated["counts"]["unchanged"] == 1 and repeated["scans"][0]["originals_requested"] == 0
        path = f'/api/activities/{first["activities"][0]["id"]}'
        original, = [s for s in (await client.get(path)).json()["sources"] if s["kind"].startswith("original")]
        assert original["kind"] == "original" and original["acquired_at"] == first_time

def test_rejected_archive_directory_closes_its_descriptor(tmp_path, monkeypatch):
    import errno
    import os
    import storage

    fd = os.open(tmp_path, os.O_RDONLY)
    monkeypatch.setattr(storage.os, "open", lambda *args: fd)
    with pytest.raises(OSError):
        with storage.private_file(tmp_path):
            pytest.fail("A directory was accepted as an archive file")
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF
