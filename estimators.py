"""G4 immutable evaluations from a single consistent G2 evidence capture."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

import canonical
import effort_math
import efforts
import estimator_math as maths


DEFAULT_CONFIG_VERSION = "g4-comparisons-v1"
VERSION_PATTERN = r"^[A-Za-z0-9._-]{1,80}$"
SHA_PATTERN = r"^[a-f0-9]{64}$"
SCHEMA = """
CREATE TABLE IF NOT EXISTS estimator_configs (
    version TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimator_evaluations (
    id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, config_version TEXT NOT NULL,
    cutoff_utc TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes() + Path(maths.__file__).read_bytes()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewPolicy(Model):
    d_prime_min_m: float | None = Field(None, strict=True, allow_inf_nan=False)
    d_prime_max_m: float | None = Field(None, strict=True, allow_inf_nan=False)
    max_loo_relative_cs_change: float | None = Field(None, ge=0, strict=True, allow_inf_nan=False)
    max_subset_relative_cs_range: float | None = Field(None, ge=0, strict=True, allow_inf_nan=False)
    max_activity_fraction: float | None = Field(None, gt=0, le=1, strict=True, allow_inf_nan=False)
    max_effort_date_span_days: float | None = Field(None, ge=0, strict=True, allow_inf_nan=False)
    far_extrapolation_ratio: float | None = Field(None, gt=1, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounds(self):
        if self.d_prime_min_m is not None and self.d_prime_max_m is not None and self.d_prime_min_m > self.d_prime_max_m:
            raise ValueError("D-prime lower review bound must not exceed its upper bound.")
        return self


class Configuration(Model):
    riegel_k: float = Field(1.06, gt=0, strict=True, allow_inf_nan=False)
    target_distances_m: list[float] = Field(default_factory=lambda: [5000, 10000, 21097.5, 42195], min_length=1, max_length=24)
    recent_days: int = Field(112, strict=True)
    cs_distances_m: list[float] = Field(default_factory=lambda: list(maths.CS_DISTANCES), min_length=1, max_length=6)
    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)

    @model_validator(mode="before")
    @classmethod
    def exact_numbers(cls, value):
        if isinstance(value, dict):
            for key in ("target_distances_m", "cs_distances_m"):
                if key in value and (not isinstance(value[key], list) or not all(maths.positive(d) for d in value[key])):
                    raise ValueError("Distances must be positive finite SI numbers, not booleans or text.")
        return value

    @model_validator(mode="after")
    def choices(self):
        if self.recent_days not in maths.WINDOWS:
            raise ValueError("Select a provisional 56, 84, 112 or 168-day window.")
        if len(set(self.target_distances_m)) != len(self.target_distances_m) or len(set(self.cs_distances_m)) != len(self.cs_distances_m):
            raise ValueError("Distances must be distinct.")
        if any(d not in maths.CS_DISTANCES for d in self.cs_distances_m):
            raise ValueError("CS supports only 400/800/1000/1500/3000/5000 metres.")
        return self


DEFAULT_CONFIG = Configuration().model_dump()


class Definition(Model):
    version: str = Field(pattern=VERSION_PATTERN)
    config: Configuration


class EffortRef(Model):
    activity_id: int = Field(gt=0, lt=2**63, strict=True)
    version_id: str = Field(pattern=SHA_PATTERN)
    source_sha256: str = Field(pattern=SHA_PATTERN)
    candidate_id: str = Field(pattern=SHA_PATTERN)


class Calculation(Model):
    cutoff: str
    config_version: str = Field(DEFAULT_CONFIG_VERSION, pattern=VERSION_PATTERN)
    riegel_sources: list[EffortRef] | None = Field(None, max_length=64)
    cs_sources: list[EffortRef] | None = Field(None, max_length=6)

    @model_validator(mode="after")
    def timestamp(self):
        try:
            self.cutoff = effort_math.stamp(self.cutoff).astimezone(timezone.utc).isoformat()
            # Keep date-window failures in validation, not an unhandled 500.
            effort_math.stamp(self.cutoff) - timedelta(days=max(maths.WINDOWS))
        except OverflowError:
            raise ValueError("The event cutoff must support all requested lookback windows in UTC.") from None
        for sources in (self.riegel_sources, self.cs_sources):
            if sources is not None and len({canonical.fingerprint(r.model_dump()) for r in sources}) != len(sources):
                raise ValueError("Do not select an identical source effort twice.")
        return self


def has_table(db, name):
    return bool(db and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def configuration(db, version):
    if version == DEFAULT_CONFIG_VERSION:
        return deepcopy(DEFAULT_CONFIG)
    row = db.execute("SELECT payload FROM estimator_configs WHERE version=?", (version,)).fetchone() if has_table(db, "estimator_configs") else None
    if row is None:
        raise efforts.NotFound("Estimator configuration not found.")
    return json.loads(row[0])


def definitions(db):
    rows = [{"version": DEFAULT_CONFIG_VERSION, "config": deepcopy(DEFAULT_CONFIG), "created_at": None}]
    if has_table(db, "estimator_configs"):
        rows.extend({"version": r["version"], "config": json.loads(r["payload"]), "created_at": r["created_at"]}
                    for r in db.execute("SELECT * FROM estimator_configs WHERE version!=? ORDER BY rowid", (DEFAULT_CONFIG_VERSION,)))
    return rows


def register(store, definition):
    definition = Definition.model_validate(definition)
    config = definition.config.model_dump()
    with store.connect(write=True, create=True) as db:
        previous = db.execute("SELECT payload FROM estimator_configs WHERE version=?", (definition.version,)).fetchone()
        if (definition.version == DEFAULT_CONFIG_VERSION and config != DEFAULT_CONFIG) or (previous and json.loads(previous[0]) != config):
            raise ValueError("Configuration versions are immutable; choose a new version name.")
        db.execute("INSERT OR IGNORE INTO estimator_configs VALUES (?, ?, ?)",
                   (definition.version, canonical.encoded(config), canonical.knowledge_time()))
    return {"version": definition.version, "config": config}


class TransactionView:
    """Let the public G2 selector reuse our transaction without changing G2 code identity."""
    def __init__(self, db, archive):
        self.db, self.archive = db, archive

    @contextmanager
    def history(self):
        yield self.db


def reference(candidate):
    return {"activity_id": candidate["activity_id"], "version_id": candidate["version_id"],
            "source_sha256": candidate["source_sha256"], "candidate_id": candidate["id"]}


def report_candidates(report):
    for row in report["distances"]:
        for key in ("all_time", "best_whole_activity", "best_internal_segment", "flagged_candidate", "attained_candidate"):
            if row.get(key):
                yield row[key]
        for recent in row["recent"]:
            for key in ("best", "attained_candidate"):
                if recent.get(key):
                    yield recent[key]


class Capture:
    def __init__(self, db, archive, cutoff):
        self.db, self.archive, self.cutoff = db, archive, effort_math.stamp(cutoff)
        self.report = efforts.bests(TransactionView(db, archive), cutoff, 112)
        self.dynamic = {canonical.fingerprint(reference(c)): c for c in report_candidates(self.report)}
        self.loaded, self.observations, self.source_records, self.canonical_records = {}, {}, {}, {}
        self.archive_available = {}

    def load(self, aid, version=None):
        key = (aid, version)
        if key not in self.loaded:
            self.loaded[key] = efforts.load(self.db, aid, version)
        return self.loaded[key]

    def resolve(self, ref):
        ref = EffortRef.model_validate(ref).model_dump()
        result = self.load(ref["activity_id"], ref["version_id"])
        ref["activity_id"] = result["activity_id"]
        oid = canonical.fingerprint(ref)
        if oid in self.observations:
            return self.observations[oid]
        source = efforts.select_source(result, ref["source_sha256"])
        if oid in self.dynamic:
            candidate = deepcopy(self.dynamic[oid])
        else:
            candidate, owned_source = efforts.find_candidate(self.db, result, ref["candidate_id"])
            if owned_source["fit_sha256"] != ref["source_sha256"]:
                raise efforts.NotFound("The effort does not belong to this source.")
            candidate = deepcopy(candidate)
        efforts.reviewed(self.db, candidate, ref["version_id"])
        # #30 adds qualification to the existing append-only context ledger.
        # Preserve the exact event; absent legacy qualification stays unknown.
        if candidate.get("context_history") and "qualification" in candidate["context_history"][-1]:
            candidate["provenance"]["qualification"] = deepcopy(candidate["context_history"][-1]["qualification"])
        canonical_id = result["canonical_version_id"]
        if canonical_id not in self.canonical_records:
            self.canonical_records[canonical_id] = json.loads(self.db.execute(
                "SELECT payload FROM canonical_activity_versions WHERE id=?", (canonical_id,)).fetchone()[0])
        source_id = source["source_version_id"]
        if source_id not in self.source_records:
            data, qc, created = efforts.source_data(self.db, source)
            self.source_records[source_id] = {"source": deepcopy(source), "canonical_source": data,
                                              "qc": qc, "source_created_at": created}
            self.source_records[source_id]["source"].pop("result", None)
        evidence = {"id": oid, "ref": ref, "candidate": candidate,
            "source_version_id": source_id, "canonical_version_id": canonical_id,
            "extraction": {k: deepcopy(v) for k, v in result.items() if k not in {"sources", "versions"}},
            "selection_basis": "CURRENT_SOURCE" if result["current_version_id"] == ref["version_id"] and result["selected_fit_sha256"] == ref["source_sha256"] else "EXPLICIT_HISTORICAL_OR_ALTERNATE_SOURCE",
            "qualification": maths.qualification(candidate["provenance"]), "unavailable_reasons": []}
        reasons = evidence["unavailable_reasons"]
        if result["recompute_required"]:
            reasons.append("SOURCE_NORMALIZATION_REBUILD_REQUIRED")
        if candidate.get("attainment") != "ATTAINED":
            reasons.append("UNATTAINED_LIMIT_IS_NOT_AN_OBSERVATION")
        if candidate["effective_quality_state"] != "VALID":
            reasons.append("TRACE_NOT_VALID")
        if not result["running_sport"]:
            reasons.append("NONRUNNING_SOURCE")
        if source["fit_sha256"] not in self.archive_available:
            self.archive_available[source["fit_sha256"]] = canonical.source_bytes(self.archive, source) is not None
        if not self.archive_available[source["fit_sha256"]]:
            reasons.append("ARCHIVE_UNAVAILABLE_AT_CAPTURE")
        header = source.get("header") or {}
        end = header.get("end_utc")
        if not end:
            reasons.append("MISSING_SOURCE_ACTIVITY_END")
        elif any(effort_math.stamp(t) > self.cutoff for t in (end, source.get("last_sample_utc"), candidate["end_utc"]) if t):
            reasons.append("FUTURE_ACTIVITY_OR_SAMPLE")
        if effort_math.stamp(candidate["start_utc"]) >= effort_math.stamp(candidate["end_utc"]):
            reasons.append("CONTRADICTORY_EFFORT_DATES")
        if not maths.positive(candidate["distance_m"]) or not maths.positive(candidate["elapsed_seconds"]):
            reasons.append("NONPOSITIVE_OR_NONFINITE_INPUT")
        evidence["age_days"] = (self.cutoff - effort_math.stamp(candidate["end_utc"])).total_seconds() / 86400
        self.observations[oid] = evidence
        return evidence

    def catalog(self):
        options = {}
        def add(ref):
            observation = self.resolve(ref)
            c = observation["candidate"]
            options[observation["id"]] = {"id": observation["id"], "ref": observation["ref"],
                "distance_m": c["distance_m"], "elapsed_seconds": c["elapsed_seconds"], "end_utc": c["end_utc"],
                "quality": c["effective_quality_state"], "provenance": c["provenance"],
                "unavailable_reasons": observation["unavailable_reasons"], "selection_basis": observation["selection_basis"]}
        for c in self.dynamic.values():
            add(reference(c))
        if has_table(self.db, "effort_versions"):
            for row in self.db.execute("SELECT id FROM activities ORDER BY id").fetchall():
                result = self.load(row[0])
                if result["state"] != "PROCESSED":
                    continue
                source = efforts.select_source(result)
                if not source or not source["source_version_id"]:
                    continue
                for c in efforts.candidates(source["result"]):
                    add({"activity_id": row[0], "version_id": result["version_id"], "source_sha256": source["fit_sha256"], "candidate_id": c["id"]})
            for row in self.db.execute("""SELECT i.id, i.version_id, i.source_sha256, v.activity_id
                    FROM effort_inspections i JOIN effort_versions v ON v.id=i.version_id ORDER BY i.rowid""").fetchall():
                add({"activity_id": efforts.owner(self.db, row["activity_id"]), "version_id": row["version_id"],
                     "source_sha256": row["source_sha256"], "candidate_id": row["id"]})
        return sorted(options.values(), key=lambda r: (r["distance_m"], r["ref"]["activity_id"], r["end_utc"], r["id"]))


def window_reasons(observation, days):
    reasons = list(observation["unavailable_reasons"])
    if observation["age_days"] > days:
        reasons.append("STALE_OUTSIDE_SELECTED_WINDOW")
    return reasons


def build(capture, request, config):
    used = set()
    def observe(ref):
        row = capture.resolve(ref)
        used.add(row["id"])
        return row
    profile = []
    defaults = {days: [] for days in maths.WINDOWS}
    for row in capture.report["distances"]:
        item = {"distance_m": row["distance_m"], "all_time": None, "all_time_state": row["state"], "recent": []}
        if row["all_time"]:
            o = observe(reference(row["all_time"]))
            c = o["candidate"]
            item["all_time"] = {"observation_id": o["id"], "speed_mps": c["distance_m"] / c["elapsed_seconds"],
                                "status": "SUPPORTED_OBSERVATION", "age_days": o["age_days"]}
        for recent in row["recent"]:
            if recent["days"] not in maths.WINDOWS:
                continue
            value = {"days": recent["days"], "state": recent["state"], "observation_id": None, "speed_mps": None}
            if recent["best"]:
                o = observe(reference(recent["best"]))
                c = o["candidate"]
                value.update(observation_id=o["id"], speed_mps=c["distance_m"] / c["elapsed_seconds"], status="SUPPORTED_OBSERVATION")
                defaults[recent["days"]].append(o)
            item["recent"].append(value)
        profile.append(item)
    windows = []
    for days in maths.WINDOWS:
        candidates = ([observe(r.model_dump()) for r in request.cs_sources] if request.cs_sources is not None
                      else [r for r in defaults[days] if r["candidate"]["distance_m"] in config["cs_distances_m"]])
        ds = [r["candidate"]["distance_m"] for r in candidates]
        if len(ds) != len(set(ds)):
            raise ValueError("Choose only one CS source at each distance. Compare alternatives in separate saved evaluations.")
        selected, excluded = [], []
        for o in candidates:
            reasons = window_reasons(o, days)
            if o["candidate"]["distance_m"] not in config["cs_distances_m"]:
                reasons.append("DISTANCE_OUTSIDE_CONFIGURED_CS_SET")
            if reasons:
                excluded.append({"observation_id": o["id"], "reasons": reasons})
            else:
                selected.append(o)
        model = maths.critical_speed(selected, config["review_policy"])
        date_span = max((r["age_days"] for r in selected), default=0) - min((r["age_days"] for r in selected), default=0) if selected else None
        threshold = config["review_policy"]["max_effort_date_span_days"]
        date_state = "NOT_AVAILABLE" if date_span is None else "UNKNOWN" if threshold is None else "REVIEW" if date_span > threshold else "WITHIN_CONFIGURED_POLICY"
        if date_state == "REVIEW":
            model["primary"]["status"] = "CONFLICTED_EVIDENCE"
            model["primary"]["flags"].append("EFFORT_DATE_SPAN_REVIEW")
        windows.append({"days": days, "lower_inclusive_utc": (capture.cutoff - timedelta(days=days)).isoformat(),
                        "state": "MISSING_RECENT" if not selected else model["primary"]["status"], "excluded": excluded,
                        "selection_basis": "EXPLICIT_EFFORTS" if request.cs_sources is not None else "G2_WINDOW_BESTS",
                        "date_span_review": {"value_days": date_span, "threshold_days": threshold, "state": date_state}, **model})
    selected = [observe(r.model_dump()) for r in request.riegel_sources] if request.riegel_sources is not None else defaults[config["recent_days"]]
    projections, projection_exclusions = [], []
    for o in selected:
        reasons = window_reasons(o, config["recent_days"])
        if reasons:
            projection_exclusions.append({"observation_id": o["id"], "reasons": reasons})
        else:
            projections.extend(maths.riegel(o, target, config["riegel_k"], config["review_policy"]) for target in config["target_distances_m"])
    ledger = [deepcopy(capture.observations[oid]) for oid in sorted(used)]
    identity_state = {name: [dict(r) for r in capture.db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                      for name in ("activity_redirects", "garmin_aliases") if has_table(capture.db, name)}
    evidence = {"request": request.model_dump(), "config": config, "g2_selection": capture.report,
        "identity_state_at_capture": identity_state, "legacy_identity_knowledge_time": "UNKNOWN",
        "observations": ledger,
        "canonical_versions": {key: capture.canonical_records[key] for key in sorted({o["canonical_version_id"] for o in ledger})},
        "source_versions": {key: capture.source_records[key] for key in sorted({o["source_version_id"] for o in ledger})}}
    # No execution/capture timestamp is part of this hash. Evidence timestamps are
    # retained as evidence, including known-at times later than the event cutoff.
    input_hash = canonical.fingerprint(evidence)
    return {"input_hash": input_hash, "event_cutoff_utc": request.cutoff, "knowledge_cutoff_utc": None,
        "time_basis": "EVENT_TIME_RECONSTRUCTION", "knowledge_time_frozen": False,
        "algorithm_version": maths.ALGORITHM_VERSION, "code_sha256": CODE_SHA256,
        "config_version": request.config_version, "config": config, "observed_profile": profile,
        "riegel": {"selection_basis": "EXPLICIT_EFFORTS" if request.riegel_sources is not None else "G2_WINDOW_BESTS",
                   "window_days": config["recent_days"], "comparisons": projections, "excluded": projection_exclusions,
                   "selection_policy": "Separate source/target results. No preferred or fastest forecast is selected."},
        "critical_speed": {"primary_window_days": config["recent_days"], "windows": windows,
                           "window_distribution": maths.distribution([w["primary"] for w in windows])},
        "evidence": evidence, "validation": {"human": "UNVERIFIED", "physiological": "UNVERIFIED", "personal_predictive": "UNTESTED",
            "error_ledger": [], "prediction_interval": None, "interval_unavailable_reason": "No independent prospective outcomes or coverage calibration.",
            "recovery_completeness": "NOT_ASSESSED_BY_CANDIDATE_ESTIMATORS", "prescription_authority": False}}


def history(db):
    return [dict(r) for r in db.execute("SELECT id, input_hash, config_version, cutoff_utc, created_at FROM estimator_evaluations ORDER BY rowid DESC")] if has_table(db, "estimator_evaluations") else []


def preview(store, cutoff=None, config_version=DEFAULT_CONFIG_VERSION):
    request = Calculation(cutoff=cutoff or canonical.knowledge_time(), config_version=config_version)
    with store.history() as db:
        config = configuration(db, config_version)
        capture = Capture(db, store.archive, request.cutoff)
        report = build(capture, request, config)
        return {"preview": report, "catalog": capture.catalog(), "configurations": definitions(db), "history": history(db)}


def calculate(store, request):
    request = Calculation.model_validate(request)
    # All source reads and the append belong to one transaction. Source changes
    # cannot slip between selecting a winner and copying its context/geometry.
    with store.connect(write=True, create=True) as db:
        config = configuration(db, request.config_version)
        capture = Capture(db, store.archive, request.cutoff)
        result = build(capture, request, config)
        eid = canonical.fingerprint({"input_hash": result["input_hash"], "code_sha256": CODE_SHA256, "algorithm": maths.ALGORITHM_VERSION})
        existing = db.execute("SELECT payload FROM estimator_evaluations WHERE id=?", (eid,)).fetchone()
        if existing:
            return json.loads(existing[0])
        result.update(id=eid, created_at=canonical.knowledge_time())
        db.execute("INSERT INTO estimator_evaluations VALUES (?, ?, ?, ?, ?, ?)",
            (eid, result["input_hash"], request.config_version, request.cutoff, result["created_at"], canonical.encoded(result)))
        return result


def read(store, evaluation_id):
    with store.history() as db:
        row = db.execute("SELECT payload FROM estimator_evaluations WHERE id=?", (evaluation_id,)).fetchone() if has_table(db, "estimator_evaluations") else None
        if row is None:
            raise efforts.NotFound("Saved estimator evaluation not found.")
        return json.loads(row[0])
