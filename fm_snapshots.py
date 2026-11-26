"""G6. Build immutable FitnessSnapshots from dated G1–G5 selections in one transaction."""
import hashlib
import json
from pathlib import Path

import canonical
import fm_observations
import estimators
import fitness
import fitness_evidence
import fitness_inputs
import source_history
import workload
import workload_inputs as wi
import workload_processing

VERSION = "fitness-snapshot-v1"
AS_KNOWN = "RETROSPECTIVE_AS_KNOWN_AT_T"
RECONSTRUCTION = "EVENT_TIME_RECONSTRUCTION"


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes() + Path(fm_observations.__file__).read_bytes()).hexdigest()


def workout_tokens(group):
    """Identity within a workout; a shared provider list page cannot join workouts."""
    return sorted({*(f"local/{i}" for i in group["member_ids"]),
                   *(f"{p['provider']}/{p['id']}" for p in group["provider_ids"] or []),
                   *(f"raw/{s['sha256']}" for s in group["sources"] if s["kind"] in ("fit", "original"))})


def usable_group(row, by_owner):
    group = by_owner.get(row["activity_id"])
    if not group:
        return None, "NO_DATED_WORKOUT_OWNERSHIP"
    recording = row["selected_fit_sha256"]
    if recording and not any(s["kind"] == "fit" and s["sha256"] == recording for s in group["sources"]):
        return None, "SELECTED_RECORDING_OWNERSHIP_UNESTABLISHED"
    return group, None


def selected_config(db, k):
    row = db.execute("SELECT * FROM fm_config_selections WHERE selected_at<=? ORDER BY selected_at DESC,sequence DESC LIMIT 1",
                     (k,)).fetchone()
    return dict(row) if row else None


def config_evidence(db, g5):
    version = g5["definition"]["config"]["estimator_config"]
    selected = {"version": version, "basis": "VERSIONED_CODE_DEFAULT", "registered_at": None}
    if version != estimators.DEFAULT_CONFIG_VERSION:
        row = db.execute("SELECT created_at FROM estimator_configs WHERE version=?", (version,)).fetchone()
        if not row or wi.utc(row[0]) > g5["knowledge_cutoff"]:
            raise ValueError("Estimator configuration registration is unavailable at the knowledge cutoff.")
        selected.update(basis="REGISTRATION", registered_at=row[0])
    active = selected_config(db, g5["knowledge_cutoff"])
    return {"fitness": g5["definition"], "estimator": selected,
            "dated_active_fitness_config": active,
            "method_selection": "Current artifact creation; no earlier method selection is inferred."}


def build_inputs(db, archive, t, k, mode, config_version, inventory=None):
    g5 = fitness.collect(db, archive, t, k, mode, config_version=config_version, processing_inventory=inventory)
    config = config_evidence(db, g5)
    ledger = source_history.read_in_db(db, k)
    owners = {g["owner_id"]: g for g in ledger["groups"]}
    wb = g5["workload"]
    excluded, kept, groups, remap = [], [], [], False
    for row in wb["activities"]:
        # A future performance never enters the training bundle, even when
        # rebuilding old event time with newer ownership evidence.
        timing = row["timing"]
        if timing and timing["end_utc"] > t:
            continue
        group, reason = usable_group(row, owners)
        version = canonical.load_version(db, row["canonical_version_id"])
        if reason or not version:
            excluded.append({"activity_id": row["activity_id"], "canonical_version_id": row["canonical_version_id"],
                             "reason": reason or "CANONICAL_VERSION_UNAVAILABLE", "selection": row["selection"]})
            remap = True
            continue
        source = db.execute("SELECT json_extract(payload,'$.parser') AS parser,created_at FROM canonical_source_versions WHERE id=?",
                            (row["selected_source_version_id"],)).fetchone()
        if source and wi.utc(source["created_at"]) <= k:
            row["source_parser"] = json.loads(source["parser"]) if source["parser"] else None
            row["source_created_at"] = source["created_at"]
        groups.append(group)
        kept.append(row)
    wb["activities"] = kept
    all_ids = {i for group in groups for i in group["member_ids"]}
    selected_versions = {r["canonical_version_id"] for r in kept}
    wb["records"] = [r for r in wb["records"] if r["kind"] != "band_ledger" or r["canonical_version_id"] in selected_versions]
    profile_ids = {r["profile_version_id"] for r in wb["records"] if r["kind"] == "band_ledger"}
    wb["referenced_profiles"] = [r for r in wb["referenced_profiles"] if r["version_id"] in profile_ids]
    frozen_inventory = wb["processing_inventory"]
    if frozen_inventory:
        wb["processing_inventory"] = {**frozen_inventory, "activities": [r for r in frozen_inventory["activities"]
            if not r["timing"] or r["timing"]["end_utc"] <= t]}
    wb["processing_gaps"] = [g for g in wb["processing_gaps"] if not g.get("timing") or g["timing"]["end_utc"] <= t]
    wb["selection_exclusions"].extend(excluded)
    if remap:
        g5["capability_evidence"] = fitness_evidence.capture(db, archive, wb, config["estimator"]["version"])
        g5["run_context"] = [r for r in g5["run_context"] if r["activity_id"] in all_ids]
    keys = {(s["kind"], s["sha256"], s["format"]) for g in groups for s in g["sources"]}
    source_facts = {"version": ledger["version"], "groups": groups,
                    "unowned_sources": [{**r, "reason": "NO_DATED_WORKOUT_OWNERSHIP"} for r in ledger["source_receipts"]
                                        if not r["observed_owners"]],
                    "source_receipts": [r for r in ledger["source_receipts"] if (r["kind"], r["sha256"], r["format"]) in keys],
                    "identity_reviews": [r for r in ledger["review_events"] if r["owner_id"] in all_ids
                                         or all_ids.intersection(r["candidates_at_observation"])],
                    "ownership_history": [h for h in ledger["group_history"] if all_ids.intersection(h["group"]["member_ids"])],
                    "availability_policy": "First immutable source and owner evidence, independently dated. Claimed acquisition is informational."}
    config["activity_configurations"] = activity_configs(db, kept, k)
    return {"version": VERSION, "code_sha256": code_hash(), "g5": g5, "source_evidence": source_facts, "configuration": config,
            "observations": fm_observations.extract(db, archive, g5)}


def activity_configs(db, activities, k):
    selections = []
    for row in activities:
        aid = row["activity_id"]
        qc = {"version": row["qc_config_version"], "definition": row["qc_config"], "selection": row["selection"],
              "available_by": row["canonical_created_at"], "registration_at": None,
              "basis": "IMMUTABLE_CANONICAL_VERSION_AND_SELECTION"}
        eff = db.execute("""SELECT s.id AS selection_id,s.selected_at,v.id AS version_id,v.created_at,v.payload
            FROM effort_selections s JOIN effort_versions v ON v.id=s.version_id WHERE s.activity_id=? AND
            s.selected_at<=? AND v.created_at<=? ORDER BY s.selected_at DESC,s.id DESC LIMIT 1""", (aid,k,k)).fetchone()
        extraction = None
        if eff:
            payload = json.loads(eff["payload"])
            extraction = {"version_id": eff["version_id"], "available_by": eff["created_at"],
                "selected_at": eff["selected_at"], "selection_id": eff["selection_id"], "registration_at": None,
                "config_version": payload.get("config_version"), "definition": payload.get("config"),
                "basis": "IMMUTABLE_EFFORT_VERSION_AND_SELECTION"}
        selections.append({"activity_id": aid, "qc": qc, "effort": extraction})
    return selections


def planner_adapter(bundle, state):
    exposure = state["exposure"]
    groups = {g["owner_id"]: g for g in bundle["source_evidence"]["groups"]}
    rows = []
    for row in exposure["activities"]:
        group = groups[row["activity_id"]]
        rows.append({"activity_id": row["activity_id"], "canonical_version_id": row["canonical_version_id"],
            "selection": row["selection"], "canonical_created_at": row["canonical_created_at"],
            "identity": group, "workout_tokens": workout_tokens(group), "completed_by_cutoff": True,
            "start_utc": row["timing"]["start_utc"], "end_utc": row["timing"]["end_utc"], "end_basis": row["timing"]["end_basis"],
            "timing_basis": row["timing_basis"], "selected_fit_span": row["selected_fit_span"],
            "fit_message_written_utc": row["fit_message_written_utc"], "timing_disagreement": row["timing_disagreement"],
            "measures": row["measures"], "distance_m": row["measures"]["distance_m"], "missing_measures": row["missing_measures"],
            "source_version_id": row["selected_source_version_id"], "selected_fit_sha256": row["selected_fit_sha256"],
            "normalization_version": (row.get("source_parser") or {}).get("normalization_version"),
            "source_parser": row.get("source_parser"), "source_created_at": row.get("source_created_at"),
            "fit_timing_mapping": (row["canonical_payload"].get("header") or {}).get("end_basis"),
            "qc_config_version": row["qc_config_version"]})
    return {"basis": "START", "timezone": "Europe/London", "actuals": rows,
        "weeks": [{k: w[k] for k in ("iso_week", "local_from", "local_to_exclusive", "full_week_bounds", "week_state", "planner_start", "fm_end")}
                  for w in exposure["weeks"]],
        "coverage": [r for r in exposure["input_records"] if r["kind"] == "coverage"],
        "intensity": workload.intensity(bundle["g5"]["workload"], exposure["activities"]),
        "zone_profiles_at_cutoff": exposure["zone_profiles_at_cutoff"],
        "unassigned": exposure["unassigned"], "excluded": exposure["excluded"], "processing_gaps": exposure["processing_gaps"],
        "accounting": "Whole completed runs by London start week. Source metres and seconds stay unrounded.",
        "authority": "OBSERVED_ACTUALS_ONLY"}


def calculate(bundle):
    state = fitness.calculate(bundle["g5"])
    config = bundle["configuration"]
    source = bundle["source_evidence"]
    return {"fitness": state, "planner": planner_adapter(bundle, state),
            "lineage": {"fitness_config": config["fitness"]["version"], "fitness_config_selection": config["dated_active_fitness_config"],
                        "estimator": config["estimator"], "input_versions": [r["version_id"] for r in state["exposure"]["input_records"]],
                        "context_versions": state["lineage"]["context_version_ids"], "classification": state["lineage"]["session_labels"],
                        "run_context_versions": state["lineage"]["run_context_ids"], "activity_configurations": config["activity_configurations"],
                        "provider": state["exposure"]["provider_lineage"], "unowned_sources": source["unowned_sources"],
                        "code": {"g6": bundle["code_sha256"], "g5": bundle["g5"]["code_sha256"], **bundle["g5"]["upstream"]}},
            "availability": {"provider": state["exposure"]["provider_lineage"],
                             "strava_persistent": "POLICY_BLOCKED", "missingness": state["quality"]},
            "validation": {"deterministic": "REPLAY_AVAILABLE", "manual_garmin": "UNVERIFIED",
                           "physiological": "UNVALIDATED", "personal_prediction": "NOT_VALIDATED",
                           "planning_authority": "UNAPPROVED", "personal_error_threshold_seconds": None,
                           "personal_minimum_sample_count": None, "interval_coverage_standard": None}}


def header(row):
    return {key: row[key] for key in ("id", "input_hash", "event_cutoff", "knowledge_cutoff", "created_at", "kind",
                                      "config_version", "previous_id")} | {"snapshot_id": row["id"]}


def get_row(db, identity):
    row = db.execute("SELECT * FROM fm_snapshots WHERE id=?", (identity,)).fetchone() if wi.table(db, "fm_snapshots") else None
    if not row:
        raise KeyError("FitnessSnapshot not found.")
    return row


def changes(previous, result, before_hash, digest):
    differences = fitness.differences(previous["fitness"] if previous else None, result["fitness"])
    if before_hash != digest:
        differences.append({"field": "input_hash", "before": before_hash, "after": digest,
                            "reason": "Frozen eligible evidence, cutoffs or recipe changed."})
    if previous:
        for key in sorted(result["lineage"]):
            old = previous.get("lineage", {}).get(key)
            if old != result["lineage"][key]:
                differences.append({"field": f"lineage.{key}", "before": old, "after": result["lineage"][key],
                                    "reason": "The recorded input version, selection or source evidence changed."})
        before = {r["activity_id"]: r for r in previous["planner"]["actuals"]}
        after = {r["activity_id"]: r for r in result["planner"]["actuals"]}
        for owner in sorted(before.keys() | after.keys()):
            if before.get(owner) != after.get(owner):
                differences.append({"field": f"planner.actuals.{owner}", "before": before.get(owner), "after": after.get(owner),
                                    "reason": "Accepted workout, recorded timing, source or membership changed."})
    return differences


def build_fitness_snapshot(db, archive, *, as_of=None, knowledge_cutoff=None, mode="AS_KNOWN",
                           config_version=None, previous_id=None):
    inventory = None
    if not as_of and not knowledge_cutoff or mode == "EVENT_TIME_RECONSTRUCTION" and not knowledge_cutoff:
        inventory = workload_processing.observe(db, persist=True)
        knowledge_cutoff = inventory["known_at"]
        as_of = as_of or knowledge_cutoff
    t, k, mode = fitness.parameters(as_of, knowledge_cutoff, mode)
    if t > wi.utc(wi.now()) or mode == "AS_KNOWN" and k != t or mode == "EVENT_TIME_RECONSTRUCTION" and k < t:
        raise ValueError("As-known snapshots require K = T. Later-knowledge reconstructions require T ≤ K. Both cutoffs must have passed.")
    if config_version is None:
        selected = selected_config(db, k)
        config_version = selected["config_version"] if selected else fitness_inputs.DEFAULT_VERSION
    bundle = build_inputs(db, archive, t, k, mode, config_version, inventory)
    digest = wi.digest(bundle)
    identity = wi.digest({"version": VERSION, "input_hash": digest})
    row = db.execute("SELECT * FROM fm_snapshots WHERE id=?", (identity,)).fetchone()
    if row:
        return row
    result = calculate(bundle)
    prior = (get_row(db, previous_id) if previous_id else db.execute(
        "SELECT * FROM fm_snapshots ORDER BY (event_cutoff=?) DESC,rowid DESC LIMIT 1", (t,)).fetchone())
    delta = changes(fitness.unpack(prior["result"]) if prior else None, result, prior["input_hash"] if prior else None, digest)
    db.execute("INSERT INTO fm_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (identity, digest, t, k, wi.now(), AS_KNOWN if mode == "AS_KNOWN" else RECONSTRUCTION, config_version,
                prior["id"] if prior else None, fitness.pack(bundle), fitness.pack(result), fitness.pack(delta)))
    return get_row(db, identity)


def capture(store, **options):
    with store.connect(write=True, create=True) as db:
        return header(build_fitness_snapshot(db, store.archive, **options))


def load_snapshot(store, identity, section=None):
    with store.history() as db:
        row = get_row(db, identity)
    if section == "inputs":
        return fitness.unpack(row["inputs"])
    result = fitness.unpack(row["result"])
    if section == "replay":
        bundle = fitness.unpack(row["inputs"])
        if (bundle["code_sha256"] != code_hash() or bundle["g5"]["code_sha256"] != fitness.code_hash()
                or bundle["g5"]["upstream"] != fitness.upstream_versions()):
            return {**header(row), "status": "REQUIRES_RECORDED_CODE"}
        same = wi.digest(bundle) == row["input_hash"] and calculate(bundle) == result
        return {**header(row), "status": "IDENTICAL" if same else "MISMATCH"}
    if section:
        return result[section]
    return {**header(row), **result, "changes": fitness.unpack(row["changes"])}


def history(store):
    with store.history() as db:
        if not wi.table(db, "fm_snapshots"):
            return []
        return [header(r) for r in db.execute("SELECT id,input_hash,event_cutoff,knowledge_cutoff,created_at,kind,config_version,previous_id FROM fm_snapshots ORDER BY rowid DESC")]


def compare(store, identity, previous):
    with store.history() as db:
        prior, row = get_row(db, previous), get_row(db, identity)
    return {"snapshot_id": identity, "previous_id": previous,
            "changes": changes(fitness.unpack(prior["result"]), fitness.unpack(row["result"]), prior["input_hash"], row["input_hash"])}
