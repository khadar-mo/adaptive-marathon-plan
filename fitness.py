"""Versioned multidimensional state assembled within one SQLite transaction."""
import hashlib
import json
from pathlib import Path
import zlib

import effort_qualification
import efforts
import estimators
import fitness_endurance as endurance
import fitness_evidence as evidence
import fitness_inputs as inputs
import fitness_recovery as recovery
import workload
import workload_inputs as wi
import workload_processing
import workout_labels


VERSION = "g5-contextual-fitness-v1"


def code_hash():
    return hashlib.sha256(b"".join(Path(f).read_bytes() for f in (
        __file__, inputs.__file__, endurance.__file__, recovery.__file__, evidence.__file__))).hexdigest()


def collect(db, archive, event, knowledge, mode, *, config_version=inputs.DEFAULT_VERSION, processing_inventory=None):
    definition = inputs.configuration(db, config_version, knowledge)
    bundle = workload.collect(db, event, knowledge, mode, None, processing_inventory=processing_inventory)
    selected = evidence.capture(db, archive, bundle, definition["config"]["estimator_config"])
    completed = [a for a in bundle["activities"] if a["timing"] and a["timing"]["end_utc"] <= event]
    return {"algorithm_version": VERSION, "code_sha256": code_hash(), "definition": definition,
            "event_cutoff": event, "knowledge_cutoff": knowledge, "time_basis": mode,
            "workload": bundle, "capability_evidence": selected,
            "context_records": recovery.records_at(db, event, knowledge),
            "run_context": inputs.observations(db, knowledge, completed),
            "upstream": {"g2": efforts.CODE_SHA256, "g3": workload.code_hash(), "g4": estimators.CODE_SHA256,
                         "effort_qualification": effort_qualification.CODE_SHA256, "workout_labels": workout_labels.CODE_SHA256}}


def capabilities(g4):
    by_id = {r["id"]: r for r in g4["evidence"]["observations"]}

    def observed(value):
        if not value or not value.get("observation_id"):
            return None
        item = by_id[value["observation_id"]]
        if item["unavailable_reasons"]:
            return {"status": "INSUFFICIENT_DATA", "reasons": item["unavailable_reasons"], "ref": item["ref"]}
        c = item["candidate"]
        return {"status": "SUPPORTED_OBSERVATION", "ref": item["ref"], "observation_id": item["id"],
                "canonical_version_id": item["canonical_version_id"], "source_version_id": item["source_version_id"],
                "distance_m": c["distance_m"], "elapsed_seconds": c["elapsed_seconds"], "elapsed_coordinate_s": c["elapsed_coordinate_s"],
                "start_utc": c["start_utc"], "end_utc": c["end_utc"], "exact_endpoint_clock": item["exact_end_clock"],
                "speed_mps": value["speed_mps"], "age_days": item["age_days"], "quality": c["effective_quality_state"],
                "category": c["provenance"]["category"], "context_version": c["context_input_version"], "qualification": item["qualification"]}
    all_time, recent = [], []
    for row in g4["observed_profile"]:
        all_time.append({"distance_m": row["distance_m"], "state": row["all_time_state"], "best": observed(row["all_time"])})
        recent.append({"distance_m": row["distance_m"], "windows": [
            {"days": r["days"], "state": r["state"], "best": observed(r)} for r in row["recent"]]})
    return all_time, recent


def quality(all_time, recent, cs, training, wellness, capabilities):
    g2 = capabilities["estimator"]["evidence"]["g2_selection"]
    return {"trace_validity": {"sources": [{"activity_id": r["activity_id"],
                      "source_sha256": r["selected_fit_sha256"], "canonical_version_id": r["canonical_version_id"],
                      "quality": capabilities["traces"].get(str(r["activity_id"]), {}).get("qc", {}).get("state", "INSUFFICIENT")}
                     for r in training["activities"]], "exclusions": g2["excluded"]},
            "effort_provenance": [{"distance_m": r["distance_m"], "category": r["best"]["category"],
                "qualification": r["best"]["qualification"], "ref": r["best"]["ref"]}
                for r in all_time if r["best"] and r["best"]["status"] == "SUPPORTED_OBSERVATION"],
            "temporal_relevance": [{"distance_m": r["distance_m"], "windows": [
                {"days": v["days"], "state": v["state"], "age_days": (v["best"] or {}).get("age_days")} for v in r["windows"]]} for r in recent],
            "identifiability": [{"days": w["days"], "status": w["primary"]["status"], "flags": w["primary"]["flags"],
                "distinct_distances": len(w["primary"]["distances_m"]), "source_overlap": w["primary"]["source_overlap"],
                "method_sensitivity": w["subset_distribution"]} for w in cs["windows"]],
            "personal_predictive_validity": {"state": "UNTESTED", "independent_outcomes": 0,
                "prediction_interval": None, "reason": "No evaluated personal outcomes supplied to this estimator version."},
            "recovery_completeness": {k: {"current_status": m["state"], "streams": [
                {"source": s["stream"], "valid_days": s["observed_days"], "expected_days": s["expected_days"],
                 "baseline_status": s["baseline_status"]} for s in m["streams"]]} for k, m in wellness.items()},
            "human_validation": "UNVERIFIED", "physiological_validation": "UNVERIFIED", "prescription_authority": False}


def calculate(bundle):
    event, knowledge = bundle["event_cutoff"], bundle["knowledge_cutoff"]
    training = workload.calculate(bundle["workload"])
    config = bundle["definition"]["config"]
    records, capability = bundle["context_records"], bundle["capability_evidence"]
    completed_ids = {i for a in training["activities"] for i in [a["activity_id"], *a["identity"]["merged_activity_ids"]]}
    linked = [r for r in records if r["kind"] != "post_run" or r["activity_id"] in completed_ids]
    rec = recovery.calculate(linked, event, config)
    rec["unlinked_feedback"] = [{"record_id": r["record_id"], "version_id": r["version_id"], "activity_id": r["activity_id"],
                                  "reason": "No completed target established at the selected cutoffs."}
                                 for r in records if r["kind"] == "post_run" and r["activity_id"] not in completed_ids]
    all_time, recent = capabilities(capability["estimator"])
    cs = capability["estimator"]["critical_speed"]
    observations = endurance.calculate(training, capability["traces"], bundle["run_context"], records, config)
    return {"algorithm_version": VERSION, "config_version": bundle["definition"]["version"], "code_sha256": bundle["code_sha256"],
            "event_cutoff": event, "knowledge_cutoff": knowledge, "time_basis": bundle["time_basis"],
            "definition": bundle["definition"], "pb_all": all_time, "pb_recent": recent, "cs_candidate": cs,
            "endurance": observations, "exposure": training, "recovery": rec,
            "quality": quality(all_time, recent, cs, training, rec["wellness"], capability),
            "lineage": {"upstream": bundle["upstream"], "selection_policy": capability["selection_policy"],
                        "g2_selection_id": capability["estimator"]["evidence"]["g2_selection"].get("selection_id"),
                        "g4_input_hash": capability["estimator"]["input_hash"], "g3_input_hash": training["input_hash"],
                        "context_version_ids": [r["version_id"] for r in records], "run_context_ids": [r["id"] for r in bundle["run_context"]],
                        "session_labels": capability["labels"]}}


def assemble(db, archive, event, knowledge, mode, **options):
    """G6 entry point; the caller owns the read transaction and freezes collect() inputs."""
    bundle = collect(db, archive, event, knowledge, mode, **options)
    return calculate(bundle)


def parameters(as_of, knowledge_cutoff, mode):
    result = workload.parameters(as_of=as_of, knowledge_cutoff=knowledge_cutoff, mode=mode)
    # Validate the supported calendar before any date-window arithmetic.
    estimators.Calculation(cutoff=result[0])
    return result[:3]


def bounded(db, store, as_of, knowledge_cutoff, mode, config_version, *, persist=False):
    event, knowledge, mode = parameters(as_of, knowledge_cutoff, mode)
    inventory = None
    if mode == "EVENT_TIME_RECONSTRUCTION" and knowledge_cutoff is None:
        inventory = workload_processing.observe(db, persist=persist)
        knowledge = inventory["known_at"]
    return collect(db, store.archive, event, knowledge, mode, config_version=config_version, processing_inventory=inventory)


def read(store, *, as_of=None, knowledge_cutoff=None, mode="EVENT_TIME_RECONSTRUCTION", config_version=inputs.DEFAULT_VERSION):
    with store.history() as db:
        bundle = bounded(db, store, as_of, knowledge_cutoff, mode, config_version)
        return calculate(bundle)


def tracked(state):
    """Compare evidence fields, keeping execution clocks and cache IDs out of reasons."""
    fields = {}
    for row in state["pb_all"]:
        best = row["best"] or {}
        fields[f"pb_all.{row['distance_m']}m"] = [row["state"], best.get("observation_id"), best.get("context_version")]
    for row in state["pb_recent"]:
        for window in row["windows"]:
            best = window["best"] or {}
            fields[f"pb_recent.{row['distance_m']}m.{window['days']}d"] = [window["state"], best.get("observation_id"), best.get("context_version")]
    for w in state["cs_candidate"]["windows"]:
        fields[f"cs_candidate.{w['days']}d"] = w["primary"]
    for w in state["endurance"]["windows"]:
        for m in ("longest", "elapsed_seconds", "timer_seconds"):
            fields[f"endurance.{m}.{w['days']}d"] = w[m]
    for k in ("sustained", "late_blocks", "comparable_effort"):
        fields["endurance." + k] = state["endurance"][k]
    for w in state["exposure"]["rolling"]:
        for k in ("frequency", "measures", "canonical_version_ids"):
            fields[f"exposure.{k}.{w['days']}d"] = w[k]
    for k, v in state["recovery"]["wellness"].items():
        fields["recovery.wellness." + k] = v
    for k in ("feedback", "attention_signals", "strength"):
        fields["recovery." + k] = state["recovery"][k]
    for k in ("trace_validity", "effort_provenance", "temporal_relevance", "identifiability", "personal_predictive_validity", "recovery_completeness"):
        fields["quality." + k] = state["quality"][k]
    fields["config_version"] = state["config_version"]
    return fields


def differences(previous, current):
    before, after = tracked(previous) if previous else {}, tracked(current)
    reasons = {"pb_all": "Selected achieved interval, qualification or trace eligibility changed.",
               "pb_recent": "Window membership, selected interval or qualifying context changed.",
               "cs_candidate": "Selected evidence or fit diagnostics changed.",
               "endurance": "Observed sessions or descriptor matches changed.",
               "exposure": "Eligible sessions, measured totals or coverage changed.",
               "recovery": "Compatible daily values, report versions or baseline window changed.",
               "quality": "The available evidence for this quality dimension changed.",
               "config_version": "The selected version of the calculation policy changed."}
    return [{"field": key, "before": before.get(key), "after": after.get(key),
             "reason": reasons[key.split(".")[0]] if previous else "First saved observation."}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def pack(value):
    return zlib.compress(wi.encoded(value).encode(), 3)


def unpack(value):
    return json.loads(zlib.decompress(value))


def capture(store, *, as_of=None, knowledge_cutoff=None, mode="EVENT_TIME_RECONSTRUCTION", config_version=inputs.DEFAULT_VERSION):
    with store.connect(write=True, create=True) as db:
        bundle = bounded(db, store, as_of, knowledge_cutoff, mode, config_version, persist=True)
        state = calculate(bundle)
        digest = wi.digest(bundle)
        identity = wi.digest({"input_hash": digest, "algorithm": VERSION})
        row = db.execute("SELECT * FROM fitness_snapshots WHERE id=?", (identity,)).fetchone()
        if not row:
            prior = db.execute("SELECT id,result FROM fitness_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            changes = differences(unpack(prior["result"]) if prior else None, state)
            db.execute("INSERT INTO fitness_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", (identity, digest, state["event_cutoff"],
                state["knowledge_cutoff"], wi.now(), config_version, pack(bundle), pack(state), prior["id"] if prior else None, wi.encoded(changes)))
            row = db.execute("SELECT * FROM fitness_snapshots WHERE id=?", (identity,)).fetchone()
        return saved_row(row)


def saved_row(row):
    return {"snapshot_id": row["id"], "input_hash": row["input_hash"], "created_at": row["created_at"],
            "previous_id": row["previous_id"], "changes": json.loads(row["changes"]), "state": unpack(row["result"])}


def snapshot(store, identity, *, inputs_only=False, replay=False):
    with store.history() as db:
        row = db.execute("SELECT * FROM fitness_snapshots WHERE id=?", (identity,)).fetchone() if wi.table(db, "fitness_snapshots") else None
    if not row:
        raise KeyError("Fitness snapshot not found.")
    if inputs_only:
        return unpack(row["inputs"])
    if replay:
        bundle = unpack(row["inputs"])
        if bundle["code_sha256"] != code_hash() or bundle["upstream"]["g3"] != workload.code_hash():
            return {"status": "REQUIRES_RECORDED_CODE", "snapshot_id": identity}
        same = wi.digest(bundle) == row["input_hash"] and calculate(bundle) == unpack(row["result"])
        return {"status": "IDENTICAL" if same else "MISMATCH", "snapshot_id": identity, "input_hash": row["input_hash"]}
    return saved_row(row)


def history(store):
    with store.history() as db:
        return [dict(row) for row in db.execute("SELECT id,event_utc,knowledge_utc,created_at,config_version FROM fitness_snapshots ORDER BY rowid DESC")] if wi.table(db, "fitness_snapshots") else []
