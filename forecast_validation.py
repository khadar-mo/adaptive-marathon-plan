"""Freeze benchmark forecasts, append qualified outcomes and compare common heldouts."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import math
from pathlib import Path

import estimator_math
import estimators
import fitness
import fm_snapshots as snapshots
import source_history
import workload_inputs as wi

VERSION = "benchmark-validation-v1"
METHODS = ("LAST_COMPARABLE", "RIEGEL")
KINDS = ("PROSPECTIVE", snapshots.AS_KNOWN, snapshots.RECONSTRUCTION)


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def positive(value, label):
    try:
        dec = Decimal(str(value))
        if not dec.is_finite() or dec <= 0 or not math.isfinite(float(dec)) or float(dec) <= 0:
            raise ValueError
        return dec
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be positive finite metres.") from None


def clock_seconds(later, earlier):
    gap = wi.stamp(later) - wi.stamp(earlier)
    return str((gap.days * 86400 + gap.seconds) + Decimal(gap.microseconds) / 1000000)


def compact_observation(o):
    c = o["candidate"]
    return {"observation_id": o["id"], "ref": o["ref"], "source_version_id": o["source_version_id"],
            "canonical_version_id": o["canonical_version_id"], "distance_m": str(c["distance_m"]),
            "elapsed_coordinate_s": c["elapsed_coordinate_s"], "kind": c["kind"], "provenance": c["provenance"],
            "qualification": o["qualification"], "start_utc": c["start_utc"], "end_utc": c["end_utc"],
            "start": c["start"], "end": c["end"], "unavailable_reasons": o["unavailable_reasons"]}


def selectable(store, identity):
    bundle = snapshots.load_snapshot(store, identity, "inputs")
    return [compact_observation(o) for o in bundle["observations"]]


def method_outputs(bundle, target, riegel_source):
    observations = bundle["observations"]
    config = bundle["g5"]["capability_evidence"]["estimator"]
    days = config["config"]["recent_days"]
    selected = None
    if riegel_source:
        selected = next((o for o in observations if o["id"] == riegel_source), None)
        if selected is None:
            raise ValueError("The selected effort does not belong to the frozen snapshot.")
    direct = [o for o in observations if o["candidate"]["kind"] == "WHOLE_ACTIVITY" and
              positive(o["candidate"]["distance_m"], "Source distance") == target and
              o["qualification"]["comparison_eligible"] and not estimators.window_reasons(o, days)]
    latest = max(direct, key=lambda o: (wi.stamp(o["candidate"]["end_utc"]), o["id"]), default=None)
    outputs = {"LAST_COMPARABLE": {"predicted_seconds": None, "reason": "NO_QUALIFIED_SAME_DISTANCE_WHOLE_WORKOUT",
                "source": None, "window_days": days, "method_version": VERSION},
               "RIEGEL": {"predicted_seconds": None, "reason": "NO_SELECTED_QUALIFYING_EFFORT", "source": None,
                "window_days": days, "method_version": config["algorithm_version"], "config_version": config["config_version"],
                "k": config["config"]["riegel_k"]}}
    if latest:
        outputs["LAST_COMPARABLE"].update(predicted_seconds=latest["candidate"]["elapsed_coordinate_s"], reason=None,
                                          source=compact_observation(latest))
    if selected and config["code_sha256"] != estimators.CODE_SHA256:
        outputs["RIEGEL"].update(source=compact_observation(selected), reason="REQUIRES_RECORDED_ESTIMATOR_CODE")
    elif selected:
        reasons = estimators.window_reasons(selected, days)
        comparison = estimator_math.riegel(selected, float(target), config["config"]["riegel_k"], config["config"]["review_policy"])
        if not selected["qualification"]["comparison_eligible"]:
            reasons.append("NO_SUPPORTED_MAXIMAL_TRIAL_OR_VERIFIED_RACE")
        if comparison["elapsed_seconds"] is None:
            reasons.append(comparison["reason"])
        outputs["RIEGEL"].update(source=compact_observation(selected), comparison=comparison, reason=", ".join(reasons) or None,
            predicted_seconds=None if reasons else str(comparison["elapsed_seconds"]))
    return outputs


def stored(db, table, key):
    if table not in ("fm_predictions", "fm_evaluations"):
        raise ValueError("Unrecognized ledger.")
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone() if wi.table(db, table) else None
    if not row:
        raise KeyError("Frozen benchmark record not found.")
    return fitness.unpack(row["payload"])


def freeze(store, **choice):
    with store.connect(write=True, create=True) as db:
        return freeze_in_db(db, **choice)


def freeze_in_db(db, *, snapshot_id, benchmark, scheduled_start, distance_m, kind, riegel_observation_id=None):
    target = positive(distance_m, "Target distance")
    start = wi.utc(scheduled_start)
    request = {"snapshot_id": snapshot_id, "benchmark": benchmark, "scheduled_start": start, "distance_m": format(target, "f").rstrip("0").rstrip(".") if "." in format(target, "f") else format(target, "f"),
               "kind": kind, "riegel_observation_id": riegel_observation_id}
    key = wi.digest({"request": request, "version": VERSION, "code_sha256": code_hash()})
    existing = db.execute("SELECT payload FROM fm_predictions WHERE id=?", (key,)).fetchone()
    if existing:
        return fitness.unpack(existing[0])
    row = snapshots.get_row(db, snapshot_id)
    if kind not in KINDS or kind == "PROSPECTIVE" and row["kind"] != snapshots.AS_KNOWN or kind != "PROSPECTIVE" and kind != row["kind"]:
        raise ValueError("The forecast kind must match the snapshot's time basis. Prospective forecasts require an as-known snapshot.")
    if start <= row["event_cutoff"]:
        raise ValueError("The benchmark must start after the training cutoff.")
    bundle = fitness.unpack(row["inputs"])
    result = fitness.unpack(row["result"])
    outputs = method_outputs(bundle, target, riegel_observation_id)
    at = wi.now()
    if kind == "PROSPECTIVE" and start <= at:
        raise ValueError("A prospective forecast must be saved before the scheduled benchmark start.")
    used_ids = {r["activity_id"] for r in result["planner"]["actuals"]}
    used_ids.update(m["source"]["ref"]["activity_id"] for m in outputs.values() if m["source"])
    groups = [snapshots.workout_tokens(g) for g in bundle["source_evidence"]["groups"] if g["owner_id"] in used_ids]
    state = result["fitness"]
    regime = {"sleep": state["recovery"]["wellness"]["sleep_duration"]["state"],
              "hrv": state["recovery"]["wellness"]["hrv"]["state"],
              "running_coverage": state["exposure"]["rolling"][0]["coverage"]["status"],
              "processing": state["exposure"]["rolling"][0]["processing"]["status"]}
    value = {**request, "id": key, "created_at": at, "method_selected_at": at, "input_hash": row["input_hash"],
             "event_cutoff": row["event_cutoff"], "knowledge_cutoff": row["knowledge_cutoff"],
             "model_version": VERSION, "validation_code_sha256": code_hash(), "g4_code_sha256": estimators.CODE_SHA256,
             "forecast_horizon_seconds": clock_seconds(start, at if kind == "PROSPECTIVE" else row["event_cutoff"]),
             "horizon_basis": "PREDICTION_CREATION_TO_SCHEDULE" if kind == "PROSPECTIVE" else "EVENT_CUTOFF_TO_SCHEDULE",
             "missing_data_regime": regime, "methods": outputs, "training_workout_tokens": groups,
             "source_partition": "All accepted training workout groups (raw recordings, provider aliases and local members).",
             "cs_forecast": {"status": "UNAVAILABLE", "reason": "No approved distance-specific predictive CS model."},
             "prediction_interval": {"status": "UNAVAILABLE", "reason": "No calibrated personal forecast interval."},
             "personal_validity": "NOT_VALIDATED", "athlete_approval": "UNAPPROVED"}
    db.execute("INSERT INTO fm_predictions VALUES(?,?,?,?)", (key, snapshot_id, at, fitness.pack(value)))
    if kind == "PROSPECTIVE" and wi.utc(wi.now()) >= start:
        raise ValueError("The scheduled workout started before the forecast write completed.")
    return value


def prediction_record(store, identity, *, replay=False):
    with store.history() as db:
        value = stored(db, "fm_predictions", identity)
        if not replay:
            return value
        row = snapshots.get_row(db, value["snapshot_id"])
        bundle = fitness.unpack(row["inputs"])
    if value["validation_code_sha256"] != code_hash() or value["g4_code_sha256"] != estimators.CODE_SHA256:
        return {"prediction_id": identity, "status": "REQUIRES_RECORDED_CODE"}
    same = wi.digest(bundle) == value["input_hash"] and method_outputs(bundle, Decimal(value["distance_m"]),
        value["riegel_observation_id"]) == value["methods"]
    return {"prediction_id": identity, "status": "IDENTICAL" if same else "MISMATCH", "input_hash": value["input_hash"]}


def list_ledger(store):
    with store.history() as db:
        if not wi.table(db, "fm_predictions"):
            return {"predictions": [], "evaluations": []}
        return {k: [fitness.unpack(r[0]) for r in db.execute(f"SELECT payload FROM {table} ORDER BY rowid DESC")]
                for k, table in (("predictions", "fm_predictions"), ("evaluations", "fm_evaluations"))}


def append(db, prediction, value):
    prior = db.execute("SELECT id,payload FROM fm_evaluations WHERE prediction_id=? ORDER BY rowid DESC LIMIT 1",
                       (prediction["id"],)).fetchone()
    # A retry of the same decision remains the original event, including its C.
    key = wi.digest({"prediction_id": prediction["id"], "decision": value})
    existing = db.execute("SELECT payload FROM fm_evaluations WHERE id=?", (key,)).fetchone()
    if existing:
        return fitness.unpack(existing[0])
    value = {**value, "id": key, "prediction_id": prediction["id"], "created_at": wi.now(), "previous_id": prior["id"] if prior else None}
    db.execute("INSERT INTO fm_evaluations VALUES(?,?,?,?,?)", (key, prediction["id"], value["created_at"], value["previous_id"], fitness.pack(value)))
    return value


def expand(tokens, groups):
    # The latest group may bridge two previously independent groups. Include
    # transitive links (e.g. provider alias -> raw recording -> local donor).
    result = set(tokens)
    while True:
        length = len(result)
        for group in groups:
            if result.intersection(group):
                result.update(group)
        if len(result) == length:
            return result


def overlap(training, outcome, groups):
    train = expand({token for g in training for token in g}, groups)
    return sorted(train.intersection(expand(outcome, groups)))


def evaluate(store, prediction_id, **choice):
    with store.connect(write=True, create=True) as db:
        return evaluate_in_db(db, prediction_id, **choice)


def evaluate_in_db(db, prediction_id, *, outcome_snapshot_id, outcome_observation_id):
    prediction = stored(db, "fm_predictions", prediction_id)
    row = snapshots.get_row(db, outcome_snapshot_id)
    bundle = fitness.unpack(row["inputs"])
    observation = next((o for o in bundle["observations"] if o["id"] == outcome_observation_id), None)
    if not observation:
        raise ValueError("The outcome must identify an observed effort in the outcome snapshot.")
    candidate = observation["candidate"]
    owner = observation["ref"]["activity_id"]
    workout = next((r for r in bundle["g5"]["workload"]["activities"] if r["activity_id"] == owner), None)
    if not workout or not workout["timing"]:
        raise ValueError("The source workout has no established start and end.")
    start, end = workout["timing"]["start_utc"], workout["timing"]["end_utc"]
    reasons = list(observation["unavailable_reasons"])
    if candidate["kind"] != "WHOLE_ACTIVITY":
        reasons.append("OUTCOME_IS_AN_INTERNAL_SEGMENT")
    if positive(candidate["distance_m"], "Outcome distance") != Decimal(prediction["distance_m"]):
        reasons.append("TARGET_DISTANCE_MISMATCH")
    if not observation["qualification"]["comparison_eligible"]:
        reasons.append("NO_QUALIFYING_BENCHMARK_OR_RACE")
    if start <= prediction["event_cutoff"]:
        reasons.append("WORKOUT_STARTED_BEFORE_TRAINING_CUTOFF")
    if prediction["kind"] == "PROSPECTIVE" and start <= prediction["created_at"]:
        reasons.append("FORECAST_NOT_FROZEN_BEFORE_ACTUAL_WORKOUT_START")
    if prediction["knowledge_cutoff"] >= start:
        reasons.append("TRAINING_KNOWLEDGE_REACHES_OUTCOME")
    if row["event_cutoff"] < end or row["knowledge_cutoff"] < prediction["knowledge_cutoff"]:
        reasons.append("OUTCOME_NOT_AVAILABLE_AT_EVALUATION_CUTOFF")
    groups = bundle["source_evidence"]["groups"]
    tokens = snapshots.workout_tokens(next(g for g in groups if g["owner_id"] == owner))
    identities = [snapshots.workout_tokens(g) for g in groups]
    prior = db.execute("SELECT payload FROM fm_evaluations WHERE prediction_id=? ORDER BY rowid DESC LIMIT 1", (prediction_id,)).fetchone()
    if prior:
        prior = fitness.unpack(prior[0])
        if row["knowledge_cutoff"] < max(prior["evaluation_knowledge_cutoff"], prior.get("identity_audit_cutoff", "")):
            raise ValueError("An outcome revision needs evidence at or after the previous evaluation knowledge cutoff.")
        for earlier in db.execute("SELECT payload FROM fm_evaluations WHERE prediction_id=? ORDER BY rowid DESC", (prediction_id,)):
            accepted = fitness.unpack(earlier[0])
            if accepted["status"] == "QUALIFIED":
                if not expand(accepted["outcome_workout_tokens"], identities).intersection(expand(tokens, identities)):
                    raise ValueError("This forecast already has a qualifying observed workout. Revisions need evidence from that workout.")
                break
        if "LATER_DISCOVERED_WORKOUT_OVERLAP" in prior["reasons"]:
            reasons.append("LATER_DISCOVERED_WORKOUT_OVERLAP")
    shared = overlap(prediction["training_workout_tokens"], tokens, identities)
    if shared:
        reasons.append("TRAIN_TEST_WORKOUT_OVERLAP")
    value = {"status": "EXCLUDED" if reasons else "QUALIFIED", "reasons": sorted(set(reasons)),
             "outcome_snapshot_id": outcome_snapshot_id, "outcome_observation_id": outcome_observation_id,
             "evaluation_knowledge_cutoff": row["knowledge_cutoff"], "outcome": compact_observation(observation),
             "observed_seconds": candidate["elapsed_coordinate_s"], "outcome_workout_tokens": tokens,
             "overlap_tokens": shared, "workout_start": start, "workout_end": end,
             "actual_horizon_seconds": clock_seconds(start, prediction["created_at"] if prediction["kind"] == "PROSPECTIVE" else prediction["event_cutoff"]),
             "outcome_source_group": next(g for g in groups if g["owner_id"] == owner)}
    saved = append(db, prediction, value)
    revised = audit_in_db(db, wi.utc(wi.now()))
    return next((e for e in revised if e["prediction_id"] == prediction_id), saved)


def audit_in_db(db, knowledge):
    """Append exclusions justified by a later identity decision. Never rewrite a forecast."""
    latest = db.execute("""SELECT e.payload FROM fm_evaluations e WHERE e.rowid=(
        SELECT MAX(v.rowid) FROM fm_evaluations v WHERE v.prediction_id=e.prediction_id)""").fetchall()
    qualified = [e for r in latest if (e := fitness.unpack(r[0]))["status"] == "QUALIFIED"]
    if not qualified:
        return []
    ledger = source_history.read_in_db(db, knowledge)
    groups = [snapshots.workout_tokens(g) for g in ledger["groups"]]
    appended = []
    for evaluation in qualified:
        prediction = stored(db, "fm_predictions", evaluation["prediction_id"])
        shared = overlap(prediction["training_workout_tokens"], evaluation["outcome_workout_tokens"], groups)
        expanded = sorted(expand(evaluation["outcome_workout_tokens"], groups))
        if shared or expanded != evaluation["outcome_workout_tokens"]:
            evidence = [g for g in ledger["groups"] if set(snapshots.workout_tokens(g)).intersection(expanded)]
            value = {k: v for k, v in evaluation.items() if k not in ("id", "prediction_id", "created_at", "previous_id")}
            value.update(outcome_workout_tokens=expanded, overlap_tokens=shared, identity_evidence=evidence,
                         identity_audit_cutoff=knowledge)
            if shared:
                value.update(status="EXCLUDED", reasons=["LATER_DISCOVERED_WORKOUT_OVERLAP"])
            appended.append(append(db, prediction, value))
    return appended


def audit(store):
    with store.connect(write=True, create=True) as db:
        return audit_in_db(db, wi.now())


def display(number):
    if number is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 25
        return str(Decimal(number.numerator) / Decimal(number.denominator))


def metrics(pairs):
    errors = [(Fraction(pred) - Fraction(actual), Fraction(actual)) for pred, actual in pairs]
    n = len(errors)
    vals = {"mae_s": None, "bias_s": None, "mape_percent": None}
    if n:
        vals = {"mae_s": sum(abs(e) for e, _ in errors) / n, "bias_s": sum(e for e, _ in errors) / n,
                "mape_percent": sum(100 * abs(e) / actual for e, actual in errors) / n}
    return {"n": n, **{k: display(v) for k, v in vals.items()},
            "exact": {k: str(v) if v is not None else None for k, v in vals.items()},
            "reason": None if n else "NO_COMMON_QUALIFYING_HELDOUT_OUTCOMES"}


def score_group(example):
    pairs = {m: [] for m in METHODS}
    seen, rows = set(), []
    for p, e in example:
        eligible = e is not None and e["status"] == "QUALIFIED"
        missing = [m for m in METHODS if p["methods"][m]["predicted_seconds"] is None]
        duplicate = bool(eligible and seen.intersection(e["outcome_workout_tokens"]))
        common = eligible and not missing and not duplicate
        if common:
            seen.update(e["outcome_workout_tokens"])
            for m in METHODS:
                pairs[m].append((p["methods"][m]["predicted_seconds"], e["observed_seconds"]))
        rows.append({"prediction_id": p["id"], "evaluation_id": e["id"] if e else None, "common_set": common,
                     "missing_methods": missing, "reason": "REPEATED_OUTCOME" if duplicate else None,
                     "status": e["status"] if e else "AWAITING_OUTCOME", "exclusions": e["reasons"] if e else [],
                     "forecast_horizon_seconds": p["forecast_horizon_seconds"],
                     "actual_horizon_seconds": e["actual_horizon_seconds"] if e else None,
                     "observed_seconds": e["observed_seconds"] if e else None,
                     "predicted_seconds": {m: p["methods"][m]["predicted_seconds"] for m in METHODS}})
    return {"metrics": {m: metrics(pairs[m]) for m in METHODS}, "outcomes": rows}


def backtest_estimators(ledger):
    """Score frozen forecasts only, with identically held-out workouts for each method."""
    latest = {}
    for e in ledger["evaluations"]:
        latest.setdefault(e["prediction_id"], e)
    groups = defaultdict(list)
    for prediction in ledger["predictions"]:
        e = latest.get(prediction["id"])
        provenance = e["outcome"]["provenance"]["category"] if e else "UNOBSERVED"
        method_set = {m: {k: v.get(k) for k in ("method_version", "config_version", "k", "window_days")} for m,v in prediction["methods"].items()}
        key = (prediction["kind"], prediction["distance_m"], provenance,
               wi.encoded(prediction["missing_data_regime"]), wi.encoded(method_set))
        groups[key].append((prediction, e))
    strata = []
    for key, example in sorted(groups.items()):
        horizons = defaultdict(list)
        for p, e in example:
            horizons[p["forecast_horizon_seconds"]].append((p,e))
        strata.append({"kind": key[0], "distance_m": key[1], "provenance": key[2],
                       "missing_data_regime": example[0][0]["missing_data_regime"],
                       "method_versions": {m: example[0][0]["methods"][m]["method_version"] for m in METHODS},
                       "by_horizon_seconds": {h: score_group(rows) for h,rows in sorted(horizons.items())}, **score_group(example)})
    return {"version": VERSION, "strata": strata, "empty_metrics": metrics([]), "personal_validity": "NOT_VALIDATED",
            "interval": "NO_CALIBRATED_PERSONAL_INTERVAL", "bias_sign": "positive means predicted elapsed time was longer",
            "policy": {"required_n": None, "acceptable_error": None, "underperformance_threshold": None,
                       "interval_coverage_standard": None, "physiological_validity": "UNKNOWN"},
            "comparison": "Identical qualifying whole workouts for both methods at each distance, provenance, timing policy, method version and missing-data regime. Exact horizon breakdowns are also available."}


def walk_forward(store, cases):
    """Refit each declared T/K from its own timeline; freeze every recipe before reading any outcome."""
    with store.connect(write=True, create=True) as db:
        predictions = []
        for case in cases:
            strict = case["kind"] == snapshots.AS_KNOWN
            if case["kind"] not in (snapshots.AS_KNOWN, snapshots.RECONSTRUCTION):
                raise ValueError("Walk-forward cases are retrospective evaluations.")
            selected = snapshots.build_fitness_snapshot(db, store.archive, as_of=case["event_cutoff"], knowledge_cutoff=case["knowledge_cutoff"],
                mode="AS_KNOWN" if strict else snapshots.RECONSTRUCTION, config_version=case.get("config_version"))
            predictions.append(freeze_in_db(db, snapshot_id=selected["id"], benchmark=case["benchmark"],
                scheduled_start=case["scheduled_start"], distance_m=case["distance_m"], kind=case["kind"],
                riegel_observation_id=case.get("riegel_observation_id")))
        evaluations = []
        for case, prediction in zip(cases, predictions):
            outcome = case.get("outcome")
            if outcome:
                evaluations.append(evaluate_in_db(db, prediction["id"], **outcome))
    ledger = list_ledger(store)
    ids = {p["id"] for p in predictions}
    subset = {"predictions": [p for p in ledger["predictions"] if p["id"] in ids],
              "evaluations": [e for e in ledger["evaluations"] if e["prediction_id"] in ids]}
    return {"predictions": predictions, "evaluations": evaluations, "report": backtest_estimators(subset)}


def refresh_outcomes(db, snapshot):
    """Revisit previously linked outcomes only on new, dated selection/qualification evidence."""
    bundle = fitness.unpack(snapshot["inputs"])
    observations = {o["id"]: o for o in bundle["observations"]}
    activities = {r["activity_id"]: r for r in bundle["g5"]["workload"]["activities"]}
    latest = db.execute("""SELECT payload FROM fm_evaluations e WHERE e.rowid=(
        SELECT MAX(v.rowid) FROM fm_evaluations v WHERE v.prediction_id=e.prediction_id)""").fetchall()
    for row in latest:
        previous = fitness.unpack(row[0])
        old = previous["outcome"]
        if snapshot["knowledge_cutoff"] <= max(previous["evaluation_knowledge_cutoff"], previous.get("identity_audit_cutoff", "")):
            continue
        observed = observations.get(previous["outcome_observation_id"])
        if observed:
            current = compact_observation(observed)
            if current != old and "ARCHIVE_UNAVAILABLE_AT_CAPTURE" not in current["unavailable_reasons"]:
                evaluate_in_db(db, previous["prediction_id"], outcome_snapshot_id=snapshot["id"], outcome_observation_id=observed["id"])
        elif previous["status"] == "QUALIFIED":
            selected = activities.get(old["ref"]["activity_id"])
            if selected and selected["canonical_version_id"] != old["canonical_version_id"]:
                prediction = stored(db, "fm_predictions", previous["prediction_id"])
                value = {k:v for k,v in previous.items() if k not in ("id","prediction_id","created_at","previous_id")}
                value.update(status="EXCLUDED", reasons=["OUTCOME_SOURCE_SELECTION_CHANGED"],
                    selection_evidence=selected["selection"], revision_snapshot_id=snapshot["id"],
                    evaluation_knowledge_cutoff=snapshot["knowledge_cutoff"])
                append(db, prediction, value)
