"""Synthetic provider contracts and real local API/CLI paths; no private calls."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

import app
import canonical
import garmin_import
import provider_modes
import runs
from storage import Archive, Store
from test_canonical import trace_fit
from test_fit_import import upload, resolve
from test_health import serve_database
from test_imports import DAY, digest, history, server
from fit_fixture import START, make_fit


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def choose(client, mode):
    current = (await client.get("/api/providers")).json()["config"]
    response = await client.post("/api/providers/config", json={"mode": mode, "expected_revision": current["revision"]})
    assert response.status_code == 200, response.text
    return response.json()["config"]


async def sync(client, dates=DAY):
    response = await client.post("/api/providers/import", json=dates)
    assert response.status_code == 202, response.text
    return (await client.get(response.json()["attempt_url"])).json()


async def test_read_only_default_and_policy_denial_precede_credentials_fetch_and_storage(history, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Blocked Strava analytics reached credentials, network or raw storage")

    monkeypatch.setattr(runs, "load_settings", forbidden)
    monkeypatch.setattr(runs, "request", forbidden)
    monkeypatch.setattr(Archive, "put", forbidden)
    monkeypatch.setattr(Store, "save", forbidden)
    async with server() as client:
        status = (await client.get("/api/providers")).json()
        assert status["config"]["mode"] == "GARMIN_ONLY" and status["config"]["revision"] == 0
        assert status["providers"]["strava"]["state"] == "POLICY_BLOCKED"
        assert (await client.get("/providers")).status_code == 200
        denied = await client.post("/api/imports/strava", json=DAY)
        assert denied.status_code == 403 and denied.json()["state"] == "POLICY_BLOCKED"
        assert not list(tmp_path.iterdir())
        config = await choose(client, "STRAVA_ONLY")
        result = await sync(client)
        assert result["config_revision"] == config["revision"]
        assert result["result"]["state"] == "POLICY_BLOCKED" and result["import_id"] is None
        assert result["result"]["providers"]["garmin"]["state"] == "NOT_SELECTED"
        assert (await client.get("/api/imports")).json() == []
        assert not (tmp_path / "archive").exists()
        assert not history.calls


@pytest.mark.parametrize("mode", provider_modes.MODES)
async def test_all_runtime_modes_and_fit_independence(history, mode):
    async with server() as client:
        await choose(client, mode)
        attempt = await sync(client)
        result = attempt["result"]
        if mode == "STRAVA_ONLY":
            assert result["state"] == "POLICY_BLOCKED" and not history.calls
        else:
            assert result["providers"]["garmin"]["state"] == "AVAILABLE"
            assert result["activity_ids"] == [1] and result["selected_sources"] == ["garmin"]
            assert result["providers"]["strava"]["state"] == ("POLICY_BLOCKED" if mode == "BOTH" else "NOT_SELECTED")
        raw = make_fit()
        imported = await upload(client, ("synthetic.fit", raw))
        assert imported["state"] == "complete"
        status = (await client.get("/api/providers")).json()
        assert len(status["activities"]) == 1
        assert status["activities"][0]["selected_fit_sha256"] == digest(raw)
        # The same Garmin original and exported FIT count as one actual (T10).
        actuals = (await client.get("/api/history", params=DAY)).json()
        assert actuals["run_count"] == 1


@pytest.mark.parametrize(("problem", "expected"), [
    (None, "EMPTY_RELEVANT"), (runs.ConnectionProblem("fixture offline", code="OFFLINE"), "OFFLINE"),
    (runs.ProviderError(401, "fixture 401"), "AUTH_REQUIRED"),
    (runs.ProviderError(403, "fixture 403"), "FORBIDDEN"),
    (runs.ProviderError(429, "fixture 429"), "RATE_LIMITED"),
    (runs.ConnectionProblem("fixture unsupported", code="UNSUPPORTED"), "UNSUPPORTED"),
    (runs.ConnectionProblem("fixture network failure"), "UNAVAILABLE"),
])
async def test_empty_is_not_outage_and_failures_keep_blocked_fallback_explicit(history, problem, expected):
    def search(query):
        if problem:
            raise problem
        return []

    history.search = search
    async with server() as client:
        await choose(client, "GARMIN_PRIMARY_FALLBACK")
        attempt = await sync(client)
        result = attempt["result"]
        assert result["providers"]["garmin"]["state"] == expected
        assert result["fallback_reason"] == "GARMIN_" + expected
        assert result["fallback_state"] == "POLICY_BLOCKED"
        assert result["activity_ids"] == [] and result["selected_sources"] == []
        assert result["providers"]["garmin"]["coverage"] == ("CHECKED" if problem is None else "INCOMPLETE")
        status = (await client.get("/api/providers")).json()
        assert bool(status["providers"]["garmin"]["checked_ranges"]) == (problem is None)
        assert (await client.get(attempt["html_url"])).status_code == 200
        assert not any("strava" in path for path in history.calls)


@pytest.mark.parametrize("mode", provider_modes.MODES)
async def test_synthetic_eligible_secondary_mode_contracts_use_g1_identity_without_double_count(mode):
    garmin = {"state": "AVAILABLE", "activity_ids": [7], "source_refs": [{"provider": "garmin", "id": "fixture-g"}]}
    strava = {"state": "AVAILABLE", "activity_ids": [7], "source_refs": [{"provider": "strava", "id": "fixture-s"}]}
    result = provider_modes.decide(mode, garmin, strava)
    assert result["activity_ids"] == [7]
    assert result["selected_sources"] == ({"STRAVA_ONLY": ["strava"], "BOTH": ["garmin", "strava"]}.get(mode, ["garmin"]))
    assert result["source_precedence"] == ["garmin", "strava"]
    if mode == "BOTH":
        assert result["providers"]["garmin"]["source_refs"] == garmin["source_refs"]
        assert result["providers"]["strava"]["source_refs"] == strava["source_refs"]
    if mode == "GARMIN_PRIMARY_FALLBACK":
        for state in ("EMPTY_RELEVANT", "UNAVAILABLE", "AUTH_REQUIRED", "LIMITED_SCOPE", "RATE_LIMITED"):
            empty = {"state": state, "activity_ids": []}
            fallback = provider_modes.decide(mode, empty, strava)
            assert fallback["selected_sources"] == ["strava"] and fallback["fallback_reason"] == "GARMIN_" + state
            assert fallback["fallback_state"] == "AVAILABLE"


async def test_config_versions_stale_writes_and_attempt_freeze_survive_restart(history, tmp_path, monkeypatch):
    async with server() as client:
        first = await choose(client, "BOTH")
        assert await choose(client, "BOTH") == first
        # A choice made during a provider request affects the next import only.
        history.before_detail = lambda: provider_modes.configure(app.app.state.store, "STRAVA_ONLY", first["revision"])
        attempt = await sync(client)
        assert attempt["mode"] == "BOTH" and attempt["config_revision"] == first["revision"]
        frozen = attempt["result"]["providers"]["garmin"]["canonical_versions"]
        assert len(frozen) == 1
        history.before_detail = lambda: None
        stale = await client.post("/api/providers/config", json={"mode": "GARMIN_ONLY", "expected_revision": first["revision"]})
        assert stale.status_code == 409
        await choose(client, "GARMIN_ONLY")
        history.detail["summaryDTO"]["distance"] = 5002.125
        correction = await sync(client)
        assert correction["result"]["providers"]["garmin"]["canonical_versions"] != frozen
        assert (await client.get(attempt["attempt_url"])).json() == attempt
        activity = (await client.get("/api/activities/1")).json()
        assert activity["distance_m"] == 5002.125
        saved = (await client.get("/api/activities/1/canonical")).json()
        prior = (await client.get("/api/activities/1/canonical", params={"version": frozen[0]["version_id"]})).json()
        assert prior["summary"]["distance_m"] == 5001.25 and prior["created_at"] < saved["created_at"]
        assert prior["summary"]["start_utc"] < prior["created_at"]
        again = await sync(client)
        assert again["result"]["providers"]["garmin"]["canonical_versions"] == correction["result"]["providers"]["garmin"]["canonical_versions"]
        assert len((await client.get("/api/activities/1/canonical")).json()["versions"]) == 2
        assert len([s for s in activity["sources"] if s["kind"] == "detail"]) == 2
        before = digest((tmp_path / "runs.sqlite3").read_bytes())
        (await client.get("/api/providers")).raise_for_status()
        assert digest((tmp_path / "runs.sqlite3").read_bytes()) == before
    monkeypatch.setattr(runs, "load_settings", lambda: pytest.fail("offline status loaded credentials"))
    async with server() as client:
        assert (await client.get(attempt["attempt_url"])).json() == attempt
        assert len((await client.get("/api/providers")).json()["revisions"]) == 3


@pytest.mark.parametrize(("raw", "tier", "events"), [
    (make_fit(), "A", "NOT_PROVIDED"),
    (trace_fit(), "B", "NOT_PROVIDED"),
    (trace_fit(extra=[(20, {"timestamp": START + 4, "distance": 12.5, "heart_rate": 140})]), "B", "NOT_PROVIDED"),
    (make_fit(messages=[(20, {"timestamp": START, "distance": 0, "heart_rate": 140, "altitude": 5}),
                        (20, {"timestamp": START + 1, "distance": 3, "heart_rate": 141, "altitude": 6}),
                        (21, {"timestamp": START, "event": 0, "event_type": 0})],
              metrics={"total_distance": 3}), "C", "RECORDED"),
])
async def test_actual_capability_tiers_no_invented_samples_or_events(history, raw, tier, events):
    async with server() as client:
        await upload(client, ("fixture.fit", raw))
        item, = (await client.get("/api/providers")).json()["activities"]
        assert item["capability"]["tier"] == tier
        assert item["capability"]["timer_event_coverage"] == events
        assert item["capability"]["wellness_feedback"] == "NOT_ASSESSED"
        if tier == "A":
            assert item["capability"]["continuous_efforts"] == "INSUFFICIENT"


async def test_ambiguous_source_stays_reviewable_through_modes(history):
    async with server() as client:
        await choose(client, "BOTH")
        await sync(client)
        review = await upload(client, ("nearby.fit", make_fit(start=START + 30)))
        data = (await client.get("/api/providers")).json()
        assert len(data["activities"]) == 1 and len(data["unresolved_matches"]) == 1
        await choose(client, "STRAVA_ONLY")
        assert len((await client.get("/api/providers")).json()["unresolved_matches"]) == 1
        response = await resolve(client, review, {"action": "link", "activity_id": 1})
        assert response.status_code == 200
        data = (await client.get("/api/providers")).json()
        assert len(data["activities"]) == 1 and not data["unresolved_matches"]


@pytest.mark.parametrize("payload", [{}, {"mode": "invented", "expected_revision": 0},
    {"mode": "BOTH", "expected_revision": True}, {"mode": "BOTH", "expected_revision": -1},
    {"mode": "BOTH", "expected_revision": 0, "enable_strava": True}])
async def test_invalid_config_never_writes(history, tmp_path, payload):
    async with server() as client:
        assert (await client.post("/api/providers/config", json=payload)).status_code == 422
        assert not list(tmp_path.iterdir())


async def test_bad_dates_unknown_attempt_and_cross_origin_do_not_write(history, tmp_path):
    async with server() as client:
        for url in ("/api/providers/import", "/api/imports/strava"):
            assert (await client.post(url, json={"from": "2024-02-30", "to": "2024-03-01"})).status_code == 422
            assert (await client.post(url, json={}, headers={"Origin": "https://elsewhere.invalid"})).status_code == 403
        assert (await client.post("/api/providers/config", json={"mode": "BOTH", "expected_revision": 0},
                                  headers={"Origin": "null"})).status_code == 403
        for value in ("99", "x", "999999999999999999999999"):
            assert (await client.get("/api/provider-attempts/" + value)).status_code == 404
        assert not list(tmp_path.iterdir())


async def test_config_concurrency_and_interrupted_attempt(history):
    async with server() as client:
        store = app.app.state.store
        def save(mode):
            try:
                return provider_modes.configure(store, mode, 0)
            except provider_modes.StaleConfiguration:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(save, ["BOTH", "GARMIN_PRIMARY_FALLBACK"]))
        assert results.count("stale") == 1
        attempt_id, import_id = provider_modes.start(store, DAY["from"], DAY["to"])
        assert (await client.post("/api/providers/import", json=DAY)).status_code == 409
        store.finish(import_id, "interrupted")
        result = (await client.get(f"/api/provider-attempts/{attempt_id}")).json()
        assert result["result"]["providers"]["garmin"]["state"] == "INTERRUPTED"
        assert result["result"]["providers"]["garmin"]["coverage"] == "INCOMPLETE"


async def test_finished_inner_import_keeps_live_outer_attempt_refreshing(history, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    inner_finished, release_outer = threading.Event(), threading.Event()
    original = garmin_import.run_import

    def held_worker(*args, **kwargs):
        observation = original(*args, **kwargs)
        inner_finished.set()
        assert release_outer.wait(10), "Test did not release the provider worker"
        return observation

    monkeypatch.setattr(garmin_import, "run_import", held_worker)
    async with server() as client:
        await choose(client, "GARMIN_PRIMARY_FALLBACK")
        submitted = asyncio.create_task(client.post("/api/providers/import", json=DAY))
        try:
            assert await asyncio.to_thread(inner_finished.wait, 5)
            # The inner importer has committed its terminal state and released
            # its guard, but this public request's provider worker is still alive.
            assert (await client.get("/api/imports/1")).json()["state"] == "failed"
            before = (tmp_path / "runs.sqlite3").read_bytes()
            pending = (await client.get("/api/provider-attempts/1")).json()
            assert pending["result"]["state"] == "RUNNING"
            assert (await client.get("/api/providers")).json()["attempts"][0]["result"]["state"] == "RUNNING"
            html = (await client.get("/providers?attempt=1")).text
            assert "Import in progress" in html and "setTimeout" in html
            assert (tmp_path / "runs.sqlite3").read_bytes() == before
        finally:
            release_outer.set()
            response = await submitted
        assert response.status_code == 202
        result = (await client.get(response.json()["attempt_url"])).json()["result"]
        assert result["state"] == "OFFLINE"
        assert result["fallback_reason"] == "GARMIN_OFFLINE"
        assert result["fallback_state"] == "POLICY_BLOCKED"
        assert "setTimeout" not in (await client.get("/providers?attempt=1")).text


async def test_outer_worker_failure_does_not_leave_attempt_running(history, monkeypatch):
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    original = garmin_import.run_import

    def abandoned_worker(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic failure before outer result persistence")

    monkeypatch.setattr(garmin_import, "run_import", abandoned_worker)
    async with server() as client:
        await choose(client, "GARMIN_PRIMARY_FALLBACK")
        with pytest.raises(RuntimeError, match="Synthetic failure"):
            await client.post("/api/providers/import", json=DAY)
        result = (await client.get("/api/provider-attempts/1")).json()["result"]
        assert result["state"] == "INTERRUPTED"
        assert result["fallback_reason"] == "GARMIN_INTERRUPTED"
        assert result["fallback_state"] == "POLICY_BLOCKED"
        assert "setTimeout" not in (await client.get("/providers?attempt=1")).text


async def test_real_restart_marks_abandoned_outer_attempt_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_GARMIN_OFFLINE", "1")
    path = tmp_path / "runs.sqlite3"
    store = Store(path)
    provider_modes.configure(store, "GARMIN_PRIMARY_FALLBACK", 0)
    attempt_id, import_id = provider_modes.start(store, DAY["from"], DAY["to"])
    # Durable state at a process death after the inner import ended, before the
    # provider result committed. No outer worker survives into the new process.
    garmin_import.run_import(store, store.archive, import_id, observation=True)
    with serve_database(path) as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            before = path.read_bytes()
            response = client.get(f"/api/provider-attempts/{attempt_id}")
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["state"] == "INTERRUPTED"
            assert result["providers"]["garmin"]["coverage"] == "INCOMPLETE"
            assert result["fallback_reason"] == "GARMIN_INTERRUPTED" and result["fallback_state"] == "POLICY_BLOCKED"
            assert "setTimeout" not in client.get(f"/providers?attempt={attempt_id}").text
            assert path.read_bytes() == before


async def test_actual_cli_config_status_and_import_with_loopback_server(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with serve_database(tmp_path / "runs.sqlite3") as port:
        def command(*args):
            return subprocess.run([sys.executable, str(root / "provider_cli.py"), "--port", str(port), *args],
                                  cwd=tmp_path, text=True, capture_output=True, timeout=10)
        first = command("status")
        assert first.returncode == 0 and json.loads(first.stdout)["config"]["revision"] == 0
        saved = command("configure", "STRAVA_ONLY", "--revision", "0")
        assert saved.returncode == 0 and json.loads(saved.stdout)["config"]["mode"] == "STRAVA_ONLY"
        imported = command("sync", "--from", "2024-02-29", "--to", "2024-02-29")
        assert imported.returncode == 0 and json.loads(imported.stdout)["attempt_url"]
        status = json.loads(command("status").stdout)
        assert status["attempts"][0]["result"]["state"] == "POLICY_BLOCKED"
        assert command("configure", "BOTH", "--revision", "0").returncode == 1
        assert command("sync", "--from", "bad").returncode == 1
