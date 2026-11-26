"""Exercise the public fitness profile using synthetic physical FIT and context inputs."""
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


async def test_t13_run_available_with_missing_sleep_and_hrv(history):
    async with server() as client:
        await upload(client, ("steady.fit", summary_fit()))
        response = await client.get("/api/fitness", params={"as_of": absolute_time(START + 1501).isoformat()})
        assert response.status_code == 200, response.text
        state = response.json()
        window = state["exposure"]["rolling"][0]
        assert window["run_count"] == 1
        assert window["measures"]["distance_m"]["known_subtotal"] == "5000.0"
        assert window["measures"]["elapsed_seconds"]["known_subtotal"] == "1500.375"
        assert state["recovery"]["wellness"]["hrv"]["state"] == "MISSING"
        assert state["recovery"]["wellness"]["sleep_duration"]["state"] == "MISSING"


async def test_frozen_profile_rebuilt_with_original_raw_observations(history):
    async with server() as client:
        await upload(client, ("source.fit", summary_fit()))
        response = await client.post("/api/fitness/capture", json={"as_of": absolute_time(START + 1501).isoformat()})
        assert response.status_code == 200, response.text
        saved = response.json()
        best = next(r["best"] for r in saved["state"]["pb_all"] if r["distance_m"] == 5000)
        assert best["elapsed_coordinate_s"] == "1500" and best["status"] == "SUPPORTED_OBSERVATION"
        api = "/api/fitness/snapshots/" + saved["snapshot_id"]
        assert (await client.get(api + "/replay")).json()["status"] == "IDENTICAL"
        bundle = (await client.get(api + "/inputs")).json()
        assert bundle["capability_evidence"]["traces"]["1"]["source_sha256"] == best["ref"]["source_sha256"]
        assert (await client.get(api)).json()["state"] == saved["state"]
        fixed = await client.post("/api/fitness/capture", json={"as_of": saved["state"]["event_cutoff"],
            "knowledge_cutoff": saved["state"]["knowledge_cutoff"]})
        assert fixed.json()["snapshot_id"] == saved["snapshot_id"]


async def current(client, **params):
    r = await client.get("/api/fitness", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def configure(client, config, version="synthetic-v1"):
    choice = {"version": version, "actor": "fixture", "reason": "Synthetic matching and recovery check", "config": config}
    r = await client.post("/api/fitness/configurations", json=choice)
    assert r.status_code == 200, r.text
    return r.json()


async def test_baseline_window_13_14_current_missing_precision_and_mad(history):
    from datetime import date, timedelta
    from test_context import enter, wellness
    today = date(2026, 10, 25)  # The 25-hour London day.
    cutoff = str(today) + "T23:50:00Z"
    stream = {"source": "watch", "metric": "resting_hr", "unit": "bpm", "method": "waking", "calibration": "seated"}
    def point(days, value, **extra):
        return wellness(local_date=str(today - timedelta(days=days)), **{**stream, "source_id": f"r-{days}", "value": str(value), **extra})
    async with server() as client:
        await configure(client, {"robust_deviations": [{"stream": stream, "epsilon": "0.125", "direction": "HIGHER"}]})
        # 29 days ago and the present day must never be used in the reference.
        initial = [point(29, 222), *[point(d, 48) for d in range(1, 14)], point(0, "49.000000000000000000001")]
        await enter(client, initial)
        async def report():
            rows = (await current(client, as_of=cutoff, config_version="synthetic-v1"))["recovery"]["wellness"]["resting_hr"]["streams"]
            return next(s for s in rows if s["stream"]["calibration"] == "seated" and s["stream"]["method"] == "waking")
        r = await report()
        assert (r["observed_days"], r["current_status"], r["baseline_status"], r["delta"]) == (13, "OBSERVED", "BASELINE_UNKNOWN", None)
        await enter(client, point(28, 48))
        r = await report()
        assert r["observed_days"] == 14 and r["missing_days"] == 14 and r["baseline"] == "48"
        assert r["delta"] == "1.000000000000000000001"
        assert r["robust_deviation"]["value"] == "8.000000000000000000008" and r["robust_deviation"]["mad"] == "0"
        await enter(client, point(2, 199, source_id="separate-calibration", calibration="after-exercise"))
        await enter(client, point(0, 3, source_id="other-method", method="different"))
        assert (await report()) == r
        tomorrow = await current(client, as_of="2026-10-26T12:00:00Z", config_version="synthetic-v1")
        next_day = next(s for s in tomorrow["recovery"]["wellness"]["resting_hr"]["streams"] if s["stream"]["calibration"] == "seated" and s["stream"]["method"] == "waking")
        assert next_day["current_status"] == "MISSING" and next_day["baseline_status"] == "AVAILABLE" and next_day["delta"] is None
        assert next_day["robust_deviation"]["status"] == "CURRENT_MISSING"
        # Earlier T/K is independent of a later correction and its provenance.
        past = await current(client, as_of=cutoff)
        metric = past["recovery"]["wellness"]["resting_hr"]["streams"]
        known = past["knowledge_cutoff"]
        old_id = next(s for s in metric if s["stream"]["method"] == "waking" and s["stream"]["calibration"] == "seated")["daily_evidence"][-1]["version_ids"][0]
        await enter(client, {**point(0, 54), "supersedes": old_id, "correction_reason": "Rechecked the watch"})
        then = await current(client, as_of=cutoff, knowledge_cutoff=known)
        assert then["recovery"] == past["recovery"]
        assert (await report())["delta"] == "6"


def steady(index, distance=1000, elapsed=300, *, start=None, events=None, hr=145):
    from demo_recordings import START as ORIGIN, example
    from demo_efforts import dense
    start = start if start is not None else ORIGIN + index * 86400
    return example(start, records=[{"timestamp": start + t, "distance": d, "heart_rate": hr, "altitude": 12 + d / 500}
                                  for t, d in dense([(0, 0), (elapsed, distance)], 10)],
                   events=[{"timestamp": start + t, "event": 0, "event_type": kind} for t, kind in (events or [])],
                   laps=[], distance=distance, timer=elapsed, elapsed=elapsed)


def matching():
    return {"route": "Park loop", "conditions": "Dry", "duration_s": {"low": "200", "high": "400"},
            "grade_percent": {"low": "-1", "high": "1"}, "hr_bpm": {"low": "140", "high": "155"},
            "rpe": {"scale_version": "rpe-0-10-v1", "values": {"low": "3", "high": "5"}}}


async def extra_scale(client):
    r = await client.post("/api/context/definitions/scale/exertion-6-20-v1", json={"name": "Synthetic exertion", "minimum": "6", "maximum": "20", "unit": "rating", "description": "Explicit separate scale."})
    assert r.status_code == 200, r.text


async def annotation(client, aid, cid, **kw):
    raw = {"activity_id": aid, "canonical_version_id": cid, "route": "Park loop", "conditions": "Dry",
           "grade_percent": "0", "rpe": {"value": "4", "scale_version": "rpe-0-10-v1"},
           "source": "runner", "source_reference": "Synthetic course log", "actor": "runner", "reason": "Record the route", **kw}
    r = await client.post("/api/fitness/run-context", json=raw)
    assert r.status_code == 200, r.text
    return r.json()


async def test_comparable_median_ordinary_non_pb_changes_and_versioned_exclusions(history):
    cutoff = "2026-01-20T12:00:00Z"
    async with server() as client:
        await configure(client, {"comparison": matching(), "sustained_pace_s_per_km": {"low": "290", "high": "310"}, "late_after_m": "500"})
        query = {"as_of": cutoff, "config_version": "synthetic-v1"}
        await upload(client, ("a.fit", steady(0)))
        s = await current(client, **query)
        aid = s["exposure"]["activities"][0]["canonical_version_id"]
        await annotation(client, 1, aid)
        before_r = await client.post("/api/fitness/capture", json=query)
        assert before_r.status_code == 200, before_r.text
        first = before_r.json()
        initial = first["state"]["endurance"]
        assert initial["comparable_effort"]["sample_count"] == 1 and initial["comparable_effort"]["status"] == "ANECDOTAL"
        assert initial["sustained"]["longest_by_elapsed"]["elapsed_coordinate_s"] == "300"
        late, = initial["late_blocks"]["blocks"]
        assert late["preceding_distance_coordinate_m"] == "500" and late["elapsed_coordinate_s"] == "150"
        assert late["hr_mean_bpm"] == "145" and late["elevation_start_m"] == 13 and late["elevation_end_m"] == 14
        assert late["session_rpe"]["scale_version"] == "rpe-0-10-v1"
        await upload(client, ("slower.fit", steady(1, elapsed=330)))
        bid = (await current(client, **query))["exposure"]["activities"][1]["canonical_version_id"]
        b = await annotation(client, 2, bid)
        second = (await client.post("/api/fitness/capture", json=query)).json()
        comp = second["state"]["endurance"]["comparable_effort"]
        assert (comp["sample_count"], comp["comparison_count"], comp["status"], comp["median_pace_s_per_km"]) == (2, 1, "ANECDOTAL", "315")
        changed = {r["field"] for r in second["changes"]}
        assert "endurance.comparable_effort" in changed and "exposure.frequency.28d" in changed
        assert not any(f.startswith(("pb_all.", "pb_recent.", "cs_candidate.")) for f in changed)
        await upload(client, ("third.fit", steady(2, elapsed=360)))
        cid = (await current(client, **query))["exposure"]["activities"][2]["canonical_version_id"]
        await annotation(client, 3, cid)
        third = (await current(client, **query))["endurance"]["comparable_effort"]
        assert (third["status"], third["median_pace_s_per_km"], third["changes_s_per_km"]) == ("REPEATED_OBSERVATIONS", "330", ["30", "30"])
        await extra_scale(client)
        await annotation(client, 2, bid, previous_id=b["id"], reason="Different conditions", conditions="Wet",
                         rpe={"value": "10", "scale_version": "exertion-6-20-v1"})
        view = (await current(client, **query))["endurance"]["comparable_effort"]
        assert view["excluded"][0]["reasons"] == ["CONDITIONS_DIFFERS", "INCOMPATIBLE_RPE_SCALE"]
        old = (await client.get("/api/fitness/snapshots/" + second["snapshot_id"])).json()
        assert old["state"] == second["state"]
        recomputed = await current(client, **query, knowledge_cutoff=second["state"]["knowledge_cutoff"])
        assert recomputed == second["state"]


async def test_known_before_import_or_label_context_and_unsupported_future_source(history):
    cutoff = "2026-01-20T12:00:00Z"
    async with server() as client:
        empty = (await client.post("/api/fitness/capture", json={"as_of": cutoff})).json()
        await upload(client, ("known-after.fit", steady(0)))
        past = await current(client, as_of=cutoff, knowledge_cutoff=empty["state"]["knowledge_cutoff"])
        assert past == empty["state"]
        one = (await client.post("/api/fitness/capture", json={"as_of": cutoff})).json()
        cid = one["state"]["exposure"]["activities"][0]["canonical_version_id"]
        response = await client.post("/api/activities/1/workout", json={"layer": "corrected", "canonical_version_id": cid,
            "primary": "EASY_AEROBIC", "actor": "athlete", "explanation": "Synthetic later label", "source": "athlete",
            "source_reference": "Log under known cutoff", "event_utc": cutoff})
        assert response.status_code == 200, response.text
        assert (await current(client, as_of=cutoff))["lineage"]["session_labels"]["1"]["effective"] == "EASY_AEROBIC"
        earlier = await current(client, as_of=cutoff, knowledge_cutoff=one["state"]["knowledge_cutoff"])
        assert earlier == one["state"] and earlier["lineage"]["session_labels"]["1"]["effective"] == "UNKNOWN"


async def test_pain_feedback_scales_strength_kept_separate_and_read_only(history, tmp_path):
    from test_context import enter, report
    async with server() as client:
        await upload(client, ("run.fit", steady(0)))
        initial = await current(client)
        await enter(client, report(attention=["distinct_pain", "worsening_symptoms"], pain_location="ankle", context="After finish"))
        await extra_scale(client)
        await enter(client, report(kind="preweek", activity_id=None, week="2026-10-26", source_id="pre", ratings={"rpe": {"value": "12", "scale_version": "exertion-6-20-v1"}}))
        await enter(client, {"kind":"strength","source":"athlete","source_id":"squat","local_date":"2026-10-25",
            "duration_s":"2400.5","lower_body_emphasis":"yes","status":"completed","exercises":[{"name":"Squat","reps":"6","sets":3}],"ratings":{}})
        before = (tmp_path / "runs.sqlite3").read_bytes()
        state = await current(client)
        rec = state["recovery"]
        assert {r["kind"] for r in rec["attention_signals"]} == {"distinct_pain", "worsening_symptoms"}
        assert all(r["pain_location"] == "ankle" and r["action"] == "PLANNER_REVIEW_ONLY" for r in rec["attention_signals"])
        assert {r["ratings"]["rpe"]["scale_version"] for r in rec["feedback"]} == {"rpe-0-10-v1", "exertion-6-20-v1"}
        assert rec["strength"][0]["duration_s"] == "2400.5" and rec["strength"][0]["exercises"][0]["sets"] == 3
        assert state["exposure"]["activities"] == initial["exposure"]["activities"]
        assert (await client.get("/fitness")).status_code == 200
        assert before == (tmp_path / "runs.sqlite3").read_bytes()


async def test_review_known_after_run_event_revises_pb_and_endurance_only_after_knowledge(history):
    cutoff = "2026-01-20T12:00:00Z"
    async with server() as client:
        await configure(client, {"sustained_pace_s_per_km": {"low": "290", "high": "310"}})
        await upload(client, ("one-km.fit", steady(0)))
        query = {"as_of": cutoff, "config_version": "synthetic-v1"}
        first = (await client.post("/api/fitness/capture", json=query)).json()
        original = first["state"]
        ref = next(row["best"]["ref"] for row in original["pb_all"] if row["distance_m"] == 1000)
        assert original["endurance"]["sustained"]["longest_by_elapsed"]["distance_coordinate_m"] == "1000"
        url = f'/api/activities/1/efforts/{ref["version_id"]}/{ref["candidate_id"]}/review'
        tagged = await client.post(url, json={"actor": "fixture", "note": "Inspected after T", "action": "annotate"})
        assert tagged.status_code == 200, tagged.text
        annotated = (await client.post("/api/fitness/capture", json=query)).json()
        inputs_url = "/api/fitness/snapshots/" + annotated["snapshot_id"] + "/inputs"
        supplied = (await client.get(inputs_url)).json()["capability_evidence"]["estimator"]["evidence"]["observations"]
        chosen = next(o for o in supplied if o["ref"] == ref)
        assert chosen["candidate"]["reviews"][-1]["note"] == "Inspected after T"
        rejected = await client.post(url, json={"actor": "fixture", "note": "Recorded distance unreliable", "action": "reject"})
        assert rejected.status_code == 200, rejected.text
        after = await current(client, **query)
        assert next(row["best"] for row in after["pb_all"] if row["distance_m"] == 1000) is None
        shorter = after["endurance"]["sustained"]["longest_by_elapsed"]
        assert shorter["elapsed_coordinate_s"] == "290" and shorter["distance_coordinate_m"] != "1000"
        assert await current(client, **query, knowledge_cutoff=original["knowledge_cutoff"]) == original
        assert (await client.get("/api/fitness/snapshots/" + first["snapshot_id"] + "/replay")).json()["status"] == "IDENTICAL"


async def test_synthetic_g0_history_independent_no_automatic_rebuild_or_authoritative_zero(history, tmp_path):
    import gzip
    import json
    from pathlib import Path
    import sqlite3
    from storage import Archive
    fixture = json.loads(gzip.decompress((Path(__file__).parent / "fixtures" / "fueling-g0-4a0630f.json.gz").read_bytes()))
    db = tmp_path / "runs.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(fixture["sql"])
    Archive(tmp_path / "archive").put(bytes.fromhex(fixture["raw_hex"]))
    async with server() as client:
        actual = (await client.get("/api/activities/1")).json()
        assert actual["distance_m"] == 10000 and actual["elapsed_seconds"] == 3600
        before = db.read_bytes()
        unprocessed = await current(client, as_of="2024-03-01T14:00:00Z")
        assert unprocessed["pb_all"] == [] and unprocessed["exposure"]["processing_gaps"]
        assert all(w["longest"]["distance_m"] is None for w in unprocessed["endurance"]["windows"])
        assert (await client.get("/fitness")).status_code == 200 and db.read_bytes() == before


async def test_precise_input_validation_retries_and_fueling_kept_outside_state(history):
    from test_fueling import enter as fueling_entry, report as fueling_report
    import json
    from fitness import tracked
    async with server() as client:
        await upload(client, ("run.fit", steady(0)))
        cutoff = "2026-11-01T12:00:00Z"
        original = await current(client, as_of=cutoff)
        await fueling_entry(client, fueling_report(user_strategy="runner-entered synthetic text"))
        after = await current(client, as_of=cutoff)
        assert original["lineage"]["context_version_ids"] == after["lineage"]["context_version_ids"] == []
        assert tracked(original) == tracked(after)
        cid = after["endurance"]["session_evidence"][0]["canonical_version_id"]
        value = {"activity_id": 1, "canonical_version_id": cid, "grade_percent": "RAW",
                 "source": "runner", "source_reference": "grade log", "actor": "athlete", "reason": "Record"}
        raw = json.dumps(value).replace('"RAW"', "0.12345678901234567890123456789")
        async def post(text):
            return await client.post("/api/fitness/run-context", content=text, headers={"content-type":"application/json"})
        response = await post(raw)
        assert response.status_code == 200, response.text
        r = response.json()
        assert r["grade_percent"] == "0.12345678901234567890123456789"
        assert (await post(raw)).json() == r
        assert (await post(raw.replace("grade log", "changed grade log"))).status_code == 409
        assert (await post(raw.replace(cid, "a"*64))).status_code == 409
        assert (await post(raw.replace('"athlete"', '"  "'))).status_code == 422
        assert (await post(raw.replace("0.12345678901234567890123456789", "NaN"))).status_code == 422
        for clock in ("2026-10-25", "0001-01-01T00:00:00+00:00"):
            assert (await client.get("/api/fitness", params={"as_of": clock})).status_code == 422
        assert (await client.get("/api/fitness/snapshots/missing")).status_code == 404


async def test_future_source_and_pause_do_not_contribute_to_supported_intervals(history):
    async with server() as client:
        await configure(client, {"sustained_pace_s_per_km":{"low":"250","high":"350"},"late_after_m":"90"})
        await upload(client, ("future.fit", steady(20)), ("pauses.fit", steady(1, events=[(0,0),(120,1),(150,0),(300,1)])))
        state = await current(client, as_of="2026-01-15T00:00:00Z", config_version="synthetic-v1")
        assert [s["activity_id"] for s in state["endurance"]["session_evidence"]] == [2]
        assert list(state["lineage"]["session_labels"]) == ["2"]
        spans = state["endurance"]["sustained"]["qualifying_spans"]
        assert spans and all(s["elapsed_seconds"] < 300 for s in spans)
        assert all(s["activity_id"] == 2 for s in state["endurance"]["late_blocks"]["blocks"])
        assert (await client.get("/fitness", params={"as_of":"2026-01-15T00:00:00Z","activity_id":1})).status_code == 404


async def test_conflicted_fit_keeps_uncertainty_dimensions_and_unavailable_validity(history):
    import estimators
    cutoff = "2026-01-15T12:00:00Z"
    async with server() as client:
        await upload(client, ("one-workout.fit", steady(0, distance=5000, elapsed=1500)))
        custom = {**estimators.DEFAULT_CONFIG, "review_policy": {"max_activity_fraction": 0.5}}
        saved = await client.post("/api/estimators/configurations", json={"version": "review-overlap", "config": custom})
        assert saved.status_code == 200, saved.text
        await configure(client, {"estimator_config": "review-overlap"})
        query = {"as_of": cutoff, "config_version": "synthetic-v1"}
        state = await current(client, **query)
        fit = state["quality"]["identifiability"][0]
        assert fit["status"] == "CONFLICTED_EVIDENCE" and fit["source_overlap"]["distinct_activities"] == 1
        assert state["quality"]["personal_predictive_validity"]["prediction_interval"] is None
        assert state["pb_all"][0]["best"]["status"] == "SUPPORTED_OBSERVATION"
        html = await client.get("/fitness", params=query)
        assert "CONFLICTED_EVIDENCE" in html.text and "INSUFFICIENT" in (await client.get("/fitness", params={"as_of":"2025-01-01T00:00:00Z"})).text
