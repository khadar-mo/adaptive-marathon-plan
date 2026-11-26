"""Synthetic G6 raw-to-snapshot and heldout validation through the normal app."""
import pytest
from test_imports import history, server
from test_fit_import import upload
from test_fit_summary_spans import summary_fit
from fit_data import absolute_time
from fit_fixture import START

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_immutable_fractional_run(history):
    async with server() as client:
        await upload(client, ("summary.fit", summary_fit()))
        t = absolute_time(START + 1501).isoformat()
        response = await client.post("/api/snapshots", json={"as_of": t, "mode": "EVENT_TIME_RECONSTRUCTION"})
        assert response.status_code == 200, response.text
        frozen = response.json()
        url = "/api/snapshots/" + frozen["id"]
        planner = (await client.get(url + "/planner")).json()
        run, = planner["actuals"]
        assert run["distance_m"] == "5000.0" and run["measures"]["elapsed_seconds"] == "1500.375"
        assert run["selected_fit_span"]["end_utc"] == run["end_utc"]
        assert run["fit_message_written_utc"] != run["end_utc"]
        assert run["identity"]["sources"][0]["first_owner_evidence"]["known_at"] <= frozen["knowledge_cutoff"]
        assert (await client.get(url + "/replay")).json()["status"] == "IDENTICAL"
        fixed = await client.post("/api/snapshots", json={"as_of": t, "knowledge_cutoff": frozen["knowledge_cutoff"],
                                                          "mode": "EVENT_TIME_RECONSTRUCTION"})
        assert fixed.json()["id"] == frozen["id"]


async def current(client, **options):
    r = await client.post("/api/snapshots", json=options)
    assert r.status_code == 200, r.text
    return r.json()


async def label(client, ref):
    route = f"/api/activities/{ref['activity_id']}/efforts/{ref['version_id']}/{ref['candidate_id']}/context"
    body = {"category": "VERIFIED_TEST", "actor": "Synthetic runner", "note": "Timed whole workout",
        "protocol": "Controlled measured course", "maximal_confirmed": True, "qualification": {
        "maximality": "CONFIRMED", "control_state": "CONTROLLED",
        "evidence_refs": [{"kind": "athlete_attestation", "reference": "Synthetic course", "method": "Runner declaration"}]}}
    r = await client.post(route, json=body)
    assert r.status_code == 200, r.text


async def whole(client, snap, owner):
    r = await client.get(f"/api/snapshots/{snap['id']}/observations")
    assert r.status_code == 200, r.text
    return next(o for o in r.json() if o["ref"]["activity_id"] == owner and o["kind"] == "WHOLE_ACTIVITY")


async def test_T14_freeze_whole_workout_then_qualify_later(history, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from demo_recordings import convert_datetime_to_timestamp
    from test_fitness import steady
    import workload_inputs as wi
    now = datetime.now(timezone.utc).replace(microsecond=0)
    train = now - timedelta(days=1)
    target = now + timedelta(days=1)
    async with server() as client:
        await upload(client, ("training.fit", steady(0, elapsed=300, start=convert_datetime_to_timestamp(train))))
        first = await current(client)
        source = await whole(client, first, 1)
        await label(client, source["ref"])
        before = await current(client)
        source = await whole(client, before, 1)
        assert source["elapsed_coordinate_s"] == "300" and source["qualification"]["credible_maximal_trial"]
        freeze = {"snapshot_id": before["id"], "benchmark": "Controlled 1 km", "scheduled_start": target.isoformat(),
                  "distance_m": "1000", "kind": "PROSPECTIVE", "riegel_observation_id": source["observation_id"]}
        r = await client.post("/api/validation/predictions", json=freeze)
        assert r.status_code == 200, r.text
        prediction = r.json()
        assert prediction["methods"]["LAST_COMPARABLE"]["predicted_seconds"] == "300"
        assert prediction["methods"]["RIEGEL"]["predicted_seconds"] == "300.0"
        assert (await client.get("/api/validation/backtest")).json()["strata"][0]["metrics"]["LAST_COMPARABLE"]["n"] == 0
        seen_later = (now + timedelta(days=2)).isoformat(timespec="microseconds")
        monkeypatch.setattr(wi, "now", lambda: seen_later)
        await upload(client, ("holdout.fit", steady(1, elapsed=320, start=convert_datetime_to_timestamp(target))))
        unqualified = await current(client)
        actual = await whole(client, unqualified, 2)
        path = "/api/validation/predictions/" + prediction["id"] + "/outcomes"
        excluded = (await client.post(path, json={"outcome_snapshot_id": unqualified["id"], "outcome_observation_id":actual["observation_id"]})).json()
        assert excluded["status"] == "EXCLUDED" and "NO_QUALIFYING_BENCHMARK_OR_RACE" in excluded["reasons"]
        await label(client, actual["ref"])
        latest = await current(client)
        outcome = await whole(client, latest, 2)
        r = await client.post(path, json={"outcome_snapshot_id":latest["id"], "outcome_observation_id":outcome["observation_id"]})
        assert r.status_code == 200, r.text
        actual = r.json()
        assert actual["status"] == "QUALIFIED", actual["reasons"]
        assert actual["previous_id"] == excluded["id"] and actual["observed_seconds"] == "320"
        rows = (await client.get("/api/validation/backtest")).json()["strata"]
        for m in rows[0]["metrics"].values():
            assert (m["n"], m["exact"]) == (1, {"mae_s":"20", "bias_s":"-20", "mape_percent":"25/4"})
        assert (await client.post("/api/validation/predictions", json=freeze)).json() == prediction
        replay = await client.get("/api/snapshots/" + before["id"] + "/replay")
        assert replay.json()["status"] == "IDENTICAL"
        assert (await client.get("/validation", params={"snapshot": latest["id"]})).status_code == 200


async def test_T15_T21_refresh_reselection_raw_and_restart(history, tmp_path):
    import hashlib
    import canonical
    import canonical_qc
    from test_fitness import configure
    async with server() as client:
        raw = summary_fit()
        await upload(client, ("raw.fit", raw))
        initial = (await client.post("/api/snapshots/refresh")).json()
        assert initial["status"] == "CAPTURED"
        old_url = "/api/snapshots/" + initial["snapshot_id"]
        old = (await client.get(old_url)).json()
        assert old["planner"]["actuals"][0]["normalization_version"]
        assert old["planner"]["actuals"][0]["fit_timing_mapping"] == "start_time_plus_total_elapsed_time"
        assert old["planner"]["actuals"][0]["selected_fit_sha256"] == hashlib.sha256(raw).hexdigest()
        cfg = {**canonical_qc.DEFAULT_CONFIG, "max_sample_gap_s":25}
        r = await client.post("/api/activities/1/canonical/recompute", json={"config_version":"g6-test-qc", "config":cfg})
        assert r.status_code == 200, r.text
        # An unrelated GET never has to compute a G6 snapshot to commit an input.
    async with server() as client:
        after = (await client.post("/api/snapshots/refresh")).json()
        assert after["status"] == "CAPTURED"
        new_url = "/api/snapshots/" + after["snapshot_id"]
        new = (await client.get(new_url)).json()
        current_run = new["planner"]["actuals"][0]
        assert current_run["qc_config_version"] == "g6-test-qc"
        assert current_run["selected_fit_sha256"] == old["planner"]["actuals"][0]["selected_fit_sha256"]
        assert current_run["canonical_version_id"] != old["planner"]["actuals"][0]["canonical_version_id"]
        assert (await client.post("/api/snapshots/refresh")).json()["snapshot_id"] == after["snapshot_id"]
        assert (await client.get(new_url + "/replay")).json()["status"] == "IDENTICAL"
        await client.post("/api/activities/1/canonical/recompute", json={"config_version":canonical.DEFAULT_CONFIG_VERSION})
        reverted = (await client.post("/api/snapshots/refresh")).json()
        original = (await client.get("/api/snapshots/" + reverted["snapshot_id"] + "/planner")).json()["actuals"][0]
        assert original["canonical_version_id"] == old["planner"]["actuals"][0]["canonical_version_id"]
        assert original["selection"]["id"] != old["planner"]["actuals"][0]["selection"]["id"]
        assert (await client.get(old_url)).json() == old
        await configure(client, {"baseline_days":30}, "g6-coverage")
        activation = {"config_version":"g6-coverage", "key":"activate-v1", "actor":"synthetic", "reason":"Changed lookback"}
        first = (await client.post("/api/snapshots/configuration", json=activation)).json()
        assert (await client.post("/api/snapshots/configuration", json=activation)).json() == first
        chosen = (await client.post("/api/snapshots/refresh")).json()
        active = (await client.get("/api/snapshots/" + chosen["snapshot_id"])).json()
        assert active["config_version"] == "g6-coverage" and active["lineage"]["fitness_config_selection"]["selected_at"] == first["selected_at"]
        assert (await current(client, as_of=old["event_cutoff"]))["id"] == old["id"]
        assert (await client.get(old_url + "/replay")).json()["status"] == "IDENTICAL"
        # A real restart and a new choice preserve G5's previously saved schema/API.
        g5 = (await client.post("/api/fitness/capture", json={})).json()
        assert (await client.get("/api/fitness/snapshots/" + g5["snapshot_id"] + "/replay")).json()["status"] == "IDENTICAL"


async def test_pending_events_restart_retries_and_old_run_arrival(history, monkeypatch):
    import fm_snapshots as fm
    import workload_inputs as wi
    from test_fitness import steady
    async with server() as client:
        cutoff = wi.now()
        late_date = absolute_time(START + 1501).isoformat()
        before = await current(client, as_of=cutoff)
        await upload(client, ("arrived-later.fit", steady(0, start=START)))
        same = await current(client, as_of=cutoff)
        assert before == same
        with monkeypatch.context() as patch:
            def interrupt(*args, **kwargs):
                raise ValueError("synthetic interruption")
            patch.setattr(fm, "calculate", interrupt)
            failed = await client.post("/api/snapshots/refresh")
            assert failed.status_code == 422
    async with server() as client:
        new = (await client.post("/api/snapshots/refresh")).json()
        assert new["status"] == "CAPTURED"
        assert (await client.post("/api/snapshots/refresh")).json()["status"] == "CURRENT"
        history_view = await current(client, as_of=late_date, mode="EVENT_TIME_RECONSTRUCTION")
        yes = (await client.get("/api/snapshots/" + history_view["id"] + "/planner")).json()
        assert [r["activity_id"] for r in yes["actuals"]] == [1]
        original = (await client.get("/api/snapshots/" + before["id"] + "/planner")).json()
        assert not original["actuals"]
        assert (await client.post("/api/snapshots", json={"as_of":cutoff,"knowledge_cutoff":late_date,"mode":"AS_KNOWN"})).status_code == 422
