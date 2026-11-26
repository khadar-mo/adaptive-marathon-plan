"""Immutable descriptor configurations and runner-supplied comparison evidence."""
from decimal import Decimal
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

import context_data
import estimators
import workload_inputs as wi

SCHEMA = """
CREATE TABLE IF NOT EXISTS fitness_configs (
 version TEXT PRIMARY KEY, payload TEXT NOT NULL, known_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fitness_run_context (
 id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL, canonical_version_id TEXT NOT NULL, previous_id TEXT,
 revision INTEGER NOT NULL, known_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(activity_id,revision)
);
CREATE TABLE IF NOT EXISTS fitness_snapshots (
 id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, event_utc TEXT NOT NULL, knowledge_utc TEXT NOT NULL,
 created_at TEXT NOT NULL, config_version TEXT NOT NULL, inputs BLOB NOT NULL, result BLOB NOT NULL,
 previous_id TEXT REFERENCES fitness_snapshots(id), changes TEXT NOT NULL
);
"""
DEFAULT_VERSION = "g5-descriptive-v1"
Text = Annotated[str, StringConstraints(min_length=1, max_length=500, pattern=r"\S")]
Note = Annotated[str, StringConstraints(min_length=1, max_length=4000, pattern=r"\S")]
Version = Annotated[str, StringConstraints(pattern=estimators.VERSION_PATTERN)]
Hash = Annotated[str, StringConstraints(pattern=estimators.SHA_PATTERN)]
Number = Annotated[Decimal, Field(max_digits=60, decimal_places=30, allow_inf_nan=False)]
Positive = Annotated[Number, Field(gt=0)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Range(Model):
    low: Number
    high: Number

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("The lower bound must be at most the upper bound.")
        return self


class EffortBand(Model):
    scale_version: Version
    values: Range


class Comparison(Model):
    route: Text
    conditions: Text
    duration_s: Range
    grade_percent: Range
    hr_bpm: Range | None = None
    rpe: EffortBand | None = None

    @model_validator(mode="after")
    def effort(self):
        if not self.hr_bpm and not self.rpe:
            raise ValueError("Choose a recorded heart-rate band or a versioned RPE band.")
        if self.duration_s.low <= 0 or (self.hr_bpm and self.hr_bpm.low <= 0):
            raise ValueError("Duration and heart rate bands require positive values.")
        return self


class Stream(Model):
    metric: Text
    unit: Text
    source: Text
    method: Text
    calibration: Text | None = None


class Robust(Model):
    stream: Stream
    epsilon: Positive
    direction: Literal["HIGHER", "LOWER", "BIDIRECTIONAL"]


class Configuration(Model):
    baseline_days: int = Field(28, ge=1, le=366, strict=True)
    minimum_days: int = Field(14, ge=1, le=366, strict=True)
    expected_metrics: list[Version] = Field(default_factory=lambda: list(context_data.DEFAULT_SETTINGS["expected_wellness_metrics"]), max_length=100)
    robust_deviations: list[Robust] = Field(default_factory=list, max_length=100)
    estimator_config: Version = estimators.DEFAULT_CONFIG_VERSION
    comparison: Comparison | None = None
    sustained_pace_s_per_km: Range | None = None
    late_after_m: Positive | None = None

    @model_validator(mode="after")
    def constraints(self):
        if self.minimum_days > self.baseline_days:
            raise ValueError("Minimum observed days must fit in the baseline window.")
        if self.sustained_pace_s_per_km and self.sustained_pace_s_per_km.low <= 0:
            raise ValueError("A sustained pace must be positive.")
        if len(set(self.expected_metrics)) != len(self.expected_metrics):
            raise ValueError("Expected metric names must be unique.")
        streams = [wi.encoded(r.stream.model_dump()) for r in self.robust_deviations]
        if len(set(streams)) != len(streams):
            raise ValueError("Each compatible metric stream can have only one robust-deviation rule.")
        return self


class Definition(Model):
    version: Version
    actor: Text
    reason: Note
    config: Configuration


class Observation(Model):
    activity_id: int = Field(strict=True, gt=0, lt=2**63)
    canonical_version_id: Hash
    route: Text | None = None
    conditions: Text | None = None
    grade_percent: Number | None = None
    mean_hr_bpm: Positive | None = None
    rpe: context_data.Rating | None = None
    source: Text
    source_reference: Note
    actor: Text
    reason: Note
    previous_id: Hash | None = None


DEFAULT_CONFIG = Configuration().model_dump(mode="json")


def configuration(db, version, knowledge):
    if version == DEFAULT_VERSION:
        return {"version": version, "known_at": None, "actor": "master v1.0",
                "reason": "Provisional descriptive defaults. No physiological matching thresholds supplied.",
                "config": DEFAULT_CONFIG}
    row = db.execute("SELECT * FROM fitness_configs WHERE version=? AND known_at<=?", (version, knowledge)).fetchone() if wi.table(db, "fitness_configs") else None
    if not row:
        raise ValueError("Fitness configuration is unavailable at the knowledge cutoff.")
    return {k: row[k] for k in ("version", "known_at", "actor", "reason")} | {"config": json.loads(row["payload"])}


def register(store, choice):
    data = Definition.model_validate(choice)
    config = data.config.model_dump(mode="json")
    with store.connect(write=True, create=True) as db:
        known = wi.now()
        estimators.configuration(db, config["estimator_config"])
        match = config["comparison"]
        if match and match["rpe"]:
            scale = context_data.definition(db, "scale", match["rpe"]["scale_version"], known)
            limits = match["rpe"]["values"]
            if Decimal(limits["low"]) < Decimal(scale["minimum"]) or Decimal(limits["high"]) > Decimal(scale["maximum"]):
                raise ValueError("The RPE band must lie within the selected scale.")
        if data.version == DEFAULT_VERSION:
            if config != DEFAULT_CONFIG:
                raise wi.Conflict("The built-in version is immutable. Choose a new version.")
            return configuration(db, data.version, known)
        existing = db.execute("SELECT * FROM fitness_configs WHERE version=?", (data.version,)).fetchone()
        if existing and (existing["payload"], existing["actor"], existing["reason"]) != (wi.encoded(config), data.actor, data.reason):
            raise wi.Conflict("That configuration version already exists. Choose a new version.")
        db.execute("INSERT OR IGNORE INTO fitness_configs VALUES (?,?,?,?,?)", (data.version, wi.encoded(config), known, data.actor, data.reason))
        return configuration(db, data.version, known)


def decorated(row):
    return {**json.loads(row["payload"]), "id": row["id"], "revision": row["revision"],
            "known_at": row["known_at"], "raw_sha256": row["raw_sha256"]}


def parse(raw):
    if len(raw) > 50_000:
        raise ValueError("Fitness input exceeds 50 KB.")
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=context_data.reject_constant, object_pairs_hook=context_data.unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Expected a JSON object.") from None


def observe(store, raw):
    value = Observation.model_validate(parse(raw)).model_dump(mode="json")
    aid, cid = value["activity_id"], value["canonical_version_id"]
    with store.connect(write=True, create=True) as db:
        known = wi.now()
        current = db.execute("SELECT v.* FROM canonical_activity_heads h JOIN canonical_activity_versions v ON v.id=h.version_id JOIN activities a ON a.id=h.activity_id WHERE h.activity_id=? AND NOT EXISTS (SELECT 1 FROM activity_redirects d WHERE d.old_id=h.activity_id)", (aid,)).fetchone()
        if not current or current["id"] != cid:
            raise wi.Conflict("Choose the current canonical version for this same activity.")
        canonical = json.loads(current["payload"])
        end = canonical["summary"]["end_utc"] or (canonical.get("header") or {}).get("end_utc")
        if not canonical["running_sport"] or not end or wi.stamp(end) > wi.stamp(known):
            raise ValueError("Choose a completed running activity with a known end time.")
        if value["rpe"]:
            rating = value["rpe"]
            scale = context_data.definition(db, "scale", rating["scale_version"], known)
            if not Decimal(scale["minimum"]) <= Decimal(rating["value"]) <= Decimal(scale["maximum"]):
                raise ValueError("RPE is outside its scale.")
            rating["scale_definition"] = scale
        ident = wi.digest(value)
        old = db.execute("SELECT * FROM fitness_run_context WHERE id=?", (ident,)).fetchone()
        if old:
            return decorated(old)
        prior = db.execute("SELECT id,revision FROM fitness_run_context WHERE activity_id=? ORDER BY revision DESC LIMIT 1", (aid,)).fetchone()
        if (prior["id"] if prior else None) != value["previous_id"]:
            raise wi.Conflict("A correction must name the current run context revision.")
        sha = store.archive.put(raw)
        db.execute("INSERT INTO fitness_run_context VALUES (?,?,?,?,?,?,?,?)",
                   (ident, aid, cid, value["previous_id"], prior["revision"] + 1 if prior else 1, known, sha, wi.encoded(value)))
        return decorated(db.execute("SELECT * FROM fitness_run_context WHERE id=?", (ident,)).fetchone())


def observations(db, knowledge, activities):
    if not wi.table(db, "fitness_run_context"):
        return []
    ids = {i for row in activities for i in [row["activity_id"], *row["identity"]["merged_activity_ids"]]}
    previous = {}
    for row in db.execute("SELECT * FROM fitness_run_context WHERE known_at<=? ORDER BY revision,id", (knowledge,)):
        if row["activity_id"] in ids:
            previous[row["activity_id"]] = decorated(row)
    return list(previous.values())


def choices(store, knowledge):
    with store.history() as db:
        extras = [r[0] for r in db.execute("SELECT version FROM fitness_configs WHERE known_at<=? ORDER BY rowid", (knowledge,))] if wi.table(db, "fitness_configs") else []
        return [DEFAULT_VERSION, *extras]


def activity_notes(store, activity_id, knowledge):
    with store.history() as db:
        if not wi.table(db, "fitness_run_context"):
            return []
        return [decorated(r) for r in db.execute("SELECT * FROM fitness_run_context WHERE activity_id=? AND known_at<=? ORDER BY revision",
                                                 (activity_id, knowledge))]
