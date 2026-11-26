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
        import recording
        assert run["normalization_version"] == recording.INSPECTION_VERSION
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
        pred_url = "/api/validation/predictions/" + prediction["id"]
        assert (await client.get(pred_url + "/replay")).json()["status"] == "IDENTICAL"
        # A later identity receipt now associates the earlier recording with the
        # heldout workout as well. A shared provider list page alone does not.
        import app
        import source_receipts
        store = app.app.state.store
        known_later = (now + timedelta(days=3)).isoformat(timespec="microseconds")
        monkeypatch.setattr(wi, "now", lambda: known_later)
        training_sha = source["ref"]["source_sha256"]
        with store.connect(write=True, create=True) as db:
            db.execute("INSERT INTO activity_sources VALUES(?,?,?,?,?)", (2,"original",training_sha,"fit",known_later))
            source_receipts.observe_group(db, 2, known_later, {"basis":"SYNTHETIC_RECORDED_IDENTITY","import_id":None,"issue_id":None,"review_event_ids":[]})
        refresh = (await client.post("/api/snapshots/refresh")).json()
        assert refresh.get("status") == "CAPTURED", refresh
        ledger = (await client.get("/api/validation")).json()
        revised = ledger["evaluations"][0]
        assert revised["previous_id"] == actual["id"] and revised["reasons"] == ["LATER_DISCOVERED_WORKOUT_OVERLAP"]
        assert "raw/" + training_sha in revised["overlap_tokens"]
        assert ledger["evaluations"][1] == actual
        assert all(v["n"] == 0 for v in (await client.get("/api/validation/backtest")).json()["strata"][0]["metrics"].values())
        assert (await client.get(pred_url)).json() == prediction
        assert (await client.get(pred_url + "/replay")).json()["status"] == "IDENTICAL"
        assert (await client.post("/api/validation/audit")).json() == []


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


async def test_provider_failure_retry_and_unowned_rollback(history, monkeypatch):
    import sqlite3
    import source_receipts
    from test_provider_modes import choose, sync
    async with server() as client:
        await choose(client, "GARMIN_PRIMARY_FALLBACK")
        original_search = history.search
        import runs
        def offline(query):
            raise runs.ConnectionProblem("Synthetic unavailable provider", code="OFFLINE")
        history.search = offline
        first = await sync(client)
        before = await current(client)
        url = "/api/snapshots/" + before["id"]
        state = (await client.get(url)).json()
        assert state["availability"]["strava_persistent"] == "POLICY_BLOCKED"
        failed = state["availability"]["provider"]["attempts"][0]["result"]
        assert failed == first["result"] and failed["fallback_reason"] == "GARMIN_OFFLINE"
        history.search = original_search
        success = await sync(client)
        done = (await client.post("/api/snapshots/refresh")).json()
        after = (await client.get("/api/snapshots/" + done["snapshot_id"])).json()
        assert after["availability"]["provider"]["attempts"][-1]["result"] == success["result"]
        assert (await client.get(url)).json() == state
        with monkeypatch.context() as patch:
            def interrupted(*args, **kwargs):
                raise sqlite3.OperationalError("Synthetic interruption before ownership commit")
            patch.setattr(source_receipts, "observe_group", interrupted)
            result = await upload(client, ("failed.fit", summary_fit()))
            assert result["state"] == "failed"
        uncertain = await current(client)
        bundle = (await client.get("/api/snapshots/" + uncertain["id"] + "/inputs")).json()
        assert any(r["reason"] == "NO_DATED_WORKOUT_OWNERSHIP" and r["kind"] == "fit" for r in bundle["source_evidence"]["unowned_sources"])
        await upload(client, ("retry.fit", summary_fit()))
        assert (await client.get("/api/snapshots/" + uncertain["id"] + "/inputs")).json() == bundle


async def test_date_only_wellness_and_feedback_revision_bound(history):
    from test_context import enter, wellness
    async with server() as client:
        first = await current(client)
        await enter(client, wellness())
        day = "2026-10-25"
        before_end = await current(client, as_of=day + "T12:00:00Z", mode="EVENT_TIME_RECONSTRUCTION")
        after_end = await current(client, as_of="2026-10-26T00:00:00Z", mode="EVENT_TIME_RECONSTRUCTION")
        async def records(snapshot):
            data = (await client.get("/api/snapshots/" + snapshot["id"] + "/inputs")).json()
            return data["g5"]["context_records"]
        assert await records(before_end) == []
        observed, = await records(after_end)
        assert observed["local_date"] == day and observed["value"] == "48.12345678901234567890"
        assert (await current(client, as_of=first["event_cutoff"]))["id"] == first["id"]
        assert await records(first) == []


async def test_refresh_scheduler_drains_after_real_import_and_restart(history, monkeypatch):
    import asyncio
    import validation_refresh
    from test_imports import submit
    worker = validation_refresh.run
    async def frequent(store):
        await worker(store, interval=0.04)
    async with server() as client:
        await submit(client)
    monkeypatch.setattr(validation_refresh, "run", frequent)
    async with server() as client:
        for _ in range(150):
            await asyncio.sleep(0.025)
            snapshots = (await client.get("/api/snapshots")).json()
            if snapshots:
                break
        assert len(snapshots) == 1
        r, retry = await asyncio.gather(client.post("/api/snapshots/refresh"), client.post("/api/snapshots/refresh"))
        assert r.json() == retry.json() and r.json()["status"] == "CURRENT"
        assert r.json()["snapshot_id"] == snapshots[0]["id"]


async def test_exact_start_end_dst_coverage_calibration_and_later_profile(history):
    from datetime import datetime
    import json
    from demo_recordings import convert_datetime_to_timestamp
    from test_fitness import steady
    from test_workload import coverage, profile, ledger
    async with server() as client:
        start = convert_datetime_to_timestamp(datetime.fromisoformat("2026-10-25T23:30:00+00:00"))
        await upload(client, ("cross-week.fit", steady(0, distance=5000.25, elapsed=3600, start=start)))
        async def ingest(data):
            r = await client.post("/api/workload/import", files={"file":("synthetic.json", json.dumps(data), "application/json")})
            assert r.status_code == 200, r.text
            return r.json()["records"][0]
        t = "2026-11-01T12:00:00Z"
        for basis in ("START", "END"):
            await ingest(coverage(source_id="fixture-" + basis, basis=basis, lower_utc="2026-10-18T23:00:00Z",
                upper_utc=t, upper_inclusive=True, observed_through=t, reported_at=t))
        raw = await current(client, as_of=t, mode="EVENT_TIME_RECONSTRUCTION")
        raw_row, = (await client.get("/api/snapshots/" + raw["id"] + "/planner")).json()["actuals"]
        cfg = await ingest(profile())
        await ingest(ledger(raw_row["canonical_version_id"], cfg))
        initial = await current(client, as_of=t, mode="EVENT_TIME_RECONSTRUCTION")
        url = "/api/snapshots/" + initial["id"]
        rows = (await client.get(url + "/planner")).json()
        run, = rows["actuals"]
        assert run["distance_m"] == "5000.25"
        before, after = rows["weeks"][-2:]
        assert before["full_week_bounds"]["lower_utc"] == "2026-10-18T23:00:00.000000+00:00"
        assert before["planner_start"]["activity_ids"] == [1] and before["fm_end"]["activity_ids"] == []
        assert after["planner_start"]["activity_ids"] == [] and after["fm_end"]["activity_ids"] == [1]
        cov = after["planner_start"]["coverage"]
        assert cov["complete"] and cov["upper_inclusive"] and cov["upper_utc"] == initial["event_cutoff"]
        assert not before["planner_start"]["coverage"]["upper_inclusive"]
        bands, = rows["intensity"]["groups"]
        assert bands["profile"]["version_id"] == cfg["version_id"] and bands["seconds"]["low"] == "2400.125"
        revised = await ingest(profile(reliability="UNCALIBRATED", supersedes=cfg["version_id"], correction_reason="Supplied correction"))
        corrected = await current(client, as_of=t, mode="EVENT_TIME_RECONSTRUCTION")
        new = (await client.get("/api/snapshots/" + corrected["id"] + "/planner")).json()
        assert new["zone_profiles_at_cutoff"][0]["version_id"] == revised["version_id"]
        assert (await client.get(url + "/planner")).json() == rows
        assert (await client.get(url + "/replay")).json()["status"] == "IDENTICAL"


async def test_rolling_cutoffs_actual_start_and_shared_comparison(history, monkeypatch):
    import forecast_validation as validation
    from datetime import datetime, timedelta, timezone
    from demo_recordings import convert_datetime_to_timestamp
    from test_fitness import steady
    from copy import deepcopy
    now = datetime.now(timezone.utc)
    async with server() as client:
        history_only = await current(client)
        await upload(client, ("already-run.fit", steady(0, start=convert_datetime_to_timestamp(now - timedelta(days=1)))))
        latest = await current(client)
        scheduled = (now + timedelta(days=1)).isoformat()
        forecast = (await client.post("/api/validation/predictions", json={"snapshot_id":latest["id"],"benchmark":"Later schedule",
            "kind":"PROSPECTIVE","scheduled_start":scheduled,"distance_m":"1000"})).json()
        outcome = await whole(client, latest, 1)
        evaluated = (await client.post("/api/validation/predictions/" + forecast["id"] + "/outcomes", json={
            "outcome_snapshot_id":latest["id"], "outcome_observation_id":outcome["observation_id"]})).json()
        assert "FORECAST_NOT_FROZEN_BEFORE_ACTUAL_WORKOUT_START" in evaluated["reasons"]
        assert "TRAIN_TEST_WORKOUT_OVERLAP" in evaluated["reasons"]
        cases = [{"event_cutoff":r["event_cutoff"],"knowledge_cutoff":r["event_cutoff"],"benchmark":"Rolling example",
            "scheduled_start":scheduled, "distance_m":"1609.344"} for r in (history_only, latest)]
        r = await client.post("/api/validation/backtest", json={"cases":cases})
        assert r.status_code == 200, r.text
        snapshots = [p["snapshot_id"] for p in r.json()["predictions"]]
        assert snapshots == [history_only["id"],latest["id"]]
        assert all(p["kind"] == "RETROSPECTIVE_AS_KNOWN_AT_T" for p in r.json()["predictions"])
        assert (await client.post("/api/validation/backtest", json={"cases":list(reversed(cases))})).json()["predictions"] == list(reversed(r.json()["predictions"]))
    # Two ordinary numerical examples share one outcome set; a missing method
    # and a duplicate workout are visible but never contribute to one side only.
    example, actual = deepcopy(forecast), deepcopy(evaluated)
    example["methods"]["LAST_COMPARABLE"]["predicted_seconds"] = "101/2"
    example["methods"]["RIEGEL"]["predicted_seconds"] = "49.5"
    actual.update(status="QUALIFIED", observed_seconds="50", reasons=[])
    pairs = []
    for index, (seconds, token) in enumerate((("50", "A"), ("100", "B"), ("200", "B"), ("80", "C"))):
        p,e = deepcopy(example),deepcopy(actual)
        p.update(id=str(index),forecast_horizon_seconds=str(100+index))
        e.update(prediction_id=p["id"], id="e"+p["id"], observed_seconds=seconds, outcome_workout_tokens=[token])
        if index==3:
            p["methods"]["RIEGEL"]["predicted_seconds"] = None
        pairs.append((p,e))
    report = validation.backtest_estimators({"predictions":[p for p,e in pairs], "evaluations":[e for p,e in pairs]})
    common, = report["strata"]
    assert [o["common_set"] for o in common["outcomes"]] == [True, True, False, False]
    assert {v["n"] for v in common["metrics"].values()} == {2}
    assert common["metrics"]["LAST_COMPARABLE"]["exact"] == {"mae_s":"25", "bias_s":"-49/2","mape_percent":"101/4"}
    assert len(common["by_horizon_seconds"]) == 4
