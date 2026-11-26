"""Versioned optional context inputs. No fitness, medical or planning decisions."""
import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from typing import Literal


PARSER_VERSION = "context-export-v1"
ALGORITHM_VERSION = "context-ingestion-v1"
MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_POLICY = "context-policy-v1"
DEFAULT_SETTINGS = {"daily_aggregation": "retain_all", "expected_wellness_metrics": ["resting_hr", "hrv", "sleep_duration"]}
KINDS = ("wellness", "post_run", "preweek", "strength", "coverage", "fueling")
EVENTS = {"wellness": "WELLNESS", "post_run": "POST_RUN_FEEDBACK", "preweek": "PREWEEK_READINESS",
          "strength": "STRENGTH", "coverage": "CONTEXT_COVERAGE", "fueling": "FUELING_PRACTICE"}
DEVICE_METRICS = {"vo2max", "vo2_max", "hrv_status", "sleep_stages", "body_battery", "stress",
                  "maximum_hr", "max_hr", "readiness", "training_readiness", "sleep_score"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS context_definitions (
    kind TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, known_at TEXT NOT NULL,
    PRIMARY KEY (kind, version)
);
CREATE TABLE IF NOT EXISTS context_records (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
    first_known_at TEXT NOT NULL, UNIQUE(kind, source, source_id)
);
CREATE TABLE IF NOT EXISTS context_versions (
    id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES context_records(id),
    revision INTEGER NOT NULL, previous_id TEXT REFERENCES context_versions(id),
    payload TEXT NOT NULL, known_at TEXT NOT NULL, correction_reason TEXT,
    UNIQUE(record_id, revision)
);
CREATE TABLE IF NOT EXISTS context_imports (
    id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
    known_at TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_sources (
    import_id TEXT NOT NULL REFERENCES context_imports(id), item_index INTEGER NOT NULL,
    version_id TEXT NOT NULL REFERENCES context_versions(id), PRIMARY KEY(import_id, item_index)
);
CREATE TABLE IF NOT EXISTS context_events (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, version_id TEXT NOT NULL UNIQUE REFERENCES context_versions(id),
    event_utc TEXT, known_at TEXT NOT NULL
);
"""


class Conflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                      default=lambda x: str(x) if isinstance(x, Decimal) else x.isoformat())


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text_decimal(value):
    # Reject float callers: only the byte-level JSON parser or decimal strings
    # can preserve source precision. There is no implicit unit conversion.
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use a decimal string or exact JSON number.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Enter a finite decimal value.") from None
    if not number.is_finite() or len(number.as_tuple().digits) > 100 or abs(number.as_tuple().exponent) > 100:
        raise ValueError("Enter a finite decimal with at most 100 digits and exponent magnitude 100.")
    return format(number, "f")


def moment(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Use an ISO timestamp with an explicit UTC offset.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise ValueError("Use an ISO timestamp with an explicit UTC offset.") from None


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a calendar date YYYY-MM-DD.")
    date.fromisoformat(value)
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(Model):
    daily_aggregation: Literal["retain_all", "median"] = "retain_all"
    expected_wellness_metrics: list[str] = Field(default_factory=lambda: list(DEFAULT_SETTINGS["expected_wellness_metrics"]), max_length=100)

    @field_validator("expected_wellness_metrics")
    @classmethod
    def metrics(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", v) for v in values):
            raise ValueError("Use unique metric names of 1–80 letters, digits, dots, underscores or hyphens.")
        return values


class Scale(Model):
    name: str = Field(min_length=1, max_length=120)
    minimum: str
    maximum: str
    unit: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    anchors: dict[str, str] = Field(default_factory=dict)

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def decimal(cls, value):
        return text_decimal(value)

    @model_validator(mode="after")
    def bounds(self):
        if Decimal(self.minimum) >= Decimal(self.maximum):
            raise ValueError("Scale minimum must be below its maximum.")
        for key, label in self.anchors.items():
            if not Decimal(self.minimum) <= Decimal(text_decimal(key)) <= Decimal(self.maximum) or len(label) > 300:
                raise ValueError("Scale anchors must lie within the bounds and have short labels.")
        return self


class Rating(Model):
    value: str
    scale_version: str = Field(min_length=1, max_length=80)

    @field_validator("value", mode="before")
    @classmethod
    def decimal(cls, value):
        return text_decimal(value)


class BaseRecord(Model):
    kind: str
    source: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=160)
    policy_version: str = DEFAULT_POLICY
    supersedes: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    correction_reason: str | None = Field(None, min_length=1, max_length=2000)
    source_updated_at: str | None = None

    @field_validator("source_updated_at")
    @classmethod
    def optional_time(cls, value):
        if value is not None:
            moment(value)
        return value

    @model_validator(mode="after")
    def correction(self):
        if bool(self.supersedes) != bool(self.correction_reason):
            raise ValueError("A correction requires both supersedes and correction_reason.")
        return self


class Dated(BaseRecord):
    local_date: str
    timezone: str = "Europe/London"
    observed_at: str | None = None

    @field_validator("local_date")
    @classmethod
    def calendar(cls, value):
        return day(value)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a supported IANA timezone.") from None
        return value

    @model_validator(mode="after")
    def consistent_date(self):
        if self.observed_at:
            utc = datetime.fromisoformat(moment(self.observed_at))
            if utc.astimezone(ZoneInfo(self.timezone)).date().isoformat() != self.local_date:
                raise ValueError("Measurement timestamp and local date disagree in the supplied timezone.")
        return self


class Wellness(Dated):
    kind: Literal["wellness"]
    metric: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    value: str | None = None
    value_type: Literal["decimal", "category"] = "decimal"
    unit: str = Field(min_length=1, max_length=80)
    validity: Literal["VALID", "SUSPECT", "INVALID", "MISSING"] = "VALID"
    missing_reason: str | None = Field(None, min_length=1, max_length=1000)
    method: str = Field(default="UNKNOWN", min_length=1, max_length=500)
    origin: Literal["device", "self_report", "laboratory", "unknown"] = "unknown"

    @field_validator("value", mode="before")
    @classmethod
    def value_string(cls, value):
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            return text_decimal(value)
        return value

    @model_validator(mode="after")
    def measurement(self):
        if self.value is None:
            if self.validity != "MISSING" or not self.missing_reason:
                raise ValueError("An absent value needs MISSING validity and a missing reason.")
        else:
            if self.validity == "MISSING" or self.missing_reason:
                raise ValueError("A supplied value cannot be marked missing.")
            if self.value_type == "decimal":
                self.value = text_decimal(self.value)
            elif not self.value or len(self.value) > 500:
                raise ValueError("Use a category of 1–500 characters.")
        return self


class Feedback(BaseRecord):
    kind: Literal["post_run", "preweek"]
    reported_at: str
    activity_id: int | None = Field(None, gt=0, lt=2**63, strict=True)
    week: str | None = None
    timezone: Literal["Europe/London"] = "Europe/London"
    ratings: dict[Literal["rpe", "fatigue", "soreness", "pain", "readiness"], Rating] = Field(default_factory=dict)
    pain_discomfort: str | None = Field(None, max_length=2000)
    pain_location: str | None = Field(None, max_length=500)
    context: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=5000)
    attention: list[Literal["distinct_pain", "worsening_symptoms", "illness"]] = Field(default_factory=list, max_length=3)
    missing_reason: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def target(self):
        moment(self.reported_at)
        if self.kind == "post_run":
            if self.activity_id is None or self.week is not None:
                raise ValueError("Post-run feedback requires only an activity_id target.")
        else:
            if self.week is None or self.activity_id is not None or date.fromisoformat(day(self.week)).weekday() != 0:
                raise ValueError("Pre-week readiness requires only a Monday week date.")
        if len(set(self.attention)) != len(self.attention):
            raise ValueError("Attention signals must be unique.")
        if not any((self.ratings, self.attention, self.pain_discomfort, self.pain_location, self.context, self.notes, self.missing_reason)):
            raise ValueError("Supply a report value or an explicit missing report reason.")
        if self.missing_reason and (self.ratings or self.attention or self.pain_discomfort):
            raise ValueError("A missing report cannot contain ratings or symptoms.")
        return self


class Fueling(BaseRecord):
    kind: Literal["fueling"]
    activity_id: int = Field(gt=0, le=MAX_SQLITE_INTEGER, strict=True)
    actor: str = Field(min_length=1, max_length=120)
    reported_at: str
    observed_at: str | None = None
    practice_status: Literal["NOT_PLANNED", "OFFERED", "PLANNED", "COMPLETED", "UNKNOWN"] = "UNKNOWN"
    user_strategy: str | None = Field(None, max_length=10000)
    tolerance: str | None = Field(None, max_length=5000)
    tolerance_reported_at: str | None = None
    notes: str | None = Field(None, max_length=10000)
    research_status: Literal["RESEARCH_PENDING"] = "RESEARCH_PENDING"

    @model_validator(mode="after")
    def timestamps(self):
        report = moment(self.reported_at)
        for value in (self.observed_at, self.tolerance_reported_at):
            if value is not None and moment(value) > report:
                raise ValueError("Observation and tolerance report times cannot follow this report timestamp.")
        if bool(self.tolerance) != bool(self.tolerance_reported_at):
            raise ValueError("Supply tolerance text and its report timestamp together, or leave both unknown.")
        return self


class Exercise(Model):
    name: str = Field(min_length=1, max_length=200)
    sets: int | None = Field(None, ge=0, le=10000, strict=True)
    reps: str | None = Field(None, max_length=300)
    load: str | None = None
    load_unit: str | None = Field(None, min_length=1, max_length=80)

    @field_validator("load", mode="before")
    @classmethod
    def decimal(cls, value):
        if value is None:
            return None
        value = text_decimal(value)
        if Decimal(value) < 0:
            raise ValueError("Exercise load cannot be negative.")
        return value

    @model_validator(mode="after")
    def units(self):
        if (self.load is None) != (self.load_unit is None):
            raise ValueError("Load and its unit must be supplied together.")
        return self


class Strength(Dated):
    kind: Literal["strength"]
    duration_s: str | None = None
    status: Literal["completed", "partial", "not_completed", "unknown"] = "unknown"
    lower_body_emphasis: Literal["yes", "no", "unknown"] = "unknown"
    exercises: list[Exercise] = Field(default_factory=list, max_length=100)
    ratings: dict[Literal["rpe", "soreness"], Rating] = Field(default_factory=dict)
    soreness_reported_at: str | None = None
    notes: str | None = Field(None, max_length=5000)

    @field_validator("duration_s", mode="before")
    @classmethod
    def duration(cls, value):
        if value is not None:
            value = text_decimal(value)
            if Decimal(value) < 0:
                raise ValueError("Duration cannot be negative.")
        return value

    @model_validator(mode="after")
    def timing(self):
        if self.duration_s is None and self.status == "unknown":
            raise ValueError("Supply duration or completion status.")
        if "soreness" in self.ratings and not self.soreness_reported_at:
            raise ValueError("Later soreness needs its report timestamp.")
        if self.soreness_reported_at:
            if self.observed_at and moment(self.soreness_reported_at) < moment(self.observed_at):
                raise ValueError("Later soreness report cannot precede the session.")
            moment(self.soreness_reported_at)
        return self


class Coverage(BaseRecord):
    kind: Literal["coverage"]
    domain: Literal["wellness", "post_run", "preweek", "strength"]
    first: str
    last: str
    timezone: str = "Europe/London"
    metric: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,80}$")
    status: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]
    basis: Literal["athlete_attestation", "import_manifest"]
    reported_at: str
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def interval(self):
        Dated.zone(self.timezone)
        if day(self.first) > day(self.last):
            raise ValueError("Coverage start must be on or before its end.")
        moment(self.reported_at)
        return self


MODELS = {"wellness": Wellness, "post_run": Feedback, "preweek": Feedback, "strength": Strength, "coverage": Coverage, "fueling": Fueling}
DEFAULT_SCALES = {
    "rpe-0-10-v1": {"name": "RPE 0–10", "minimum": "0", "maximum": "10", "unit": "rating",
                    "description": "Runner-selected session exertion scale; not a clinical threshold.",
                    "anchors": {"0": "No exertion", "10": "Maximum perceived exertion"}},
}


def definition(db, kind, version, cutoff=None):
    builtin = DEFAULT_SETTINGS if kind == "policy" and version == DEFAULT_POLICY else DEFAULT_SCALES.get(version) if kind == "scale" else None
    row = db.execute("SELECT payload,known_at FROM context_definitions WHERE kind=? AND version=?", (kind, version)).fetchone()
    if row:
        if cutoff is not None and row["known_at"] > cutoff and builtin is None:
            raise ValueError("This definition was not known at the requested cutoff.")
        return json.loads(row[0])
    if builtin is not None:
        return builtin
    raise ValueError(f"Unknown {kind} version: register its definition first.")


def register_definition(store, kind, version, payload):
    if kind not in ("policy", "scale") or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
        raise ValueError("Choose policy or scale and a version of 1–80 letters, digits, dots, underscores or hyphens.")
    value = (Policy if kind == "policy" else Scale).model_validate(payload).model_dump()
    with store.connect(write=True, create=True) as db:
        try:
            old = definition(db, kind, version)
        except ValueError:
            old = None
        if old is not None and old != value:
            raise Conflict("This definition version is immutable. Choose a new version.")
        db.execute("INSERT OR IGNORE INTO context_definitions VALUES (?,?,?,?)", (kind, version, encoded(value), now()))
    return {"kind": kind, "version": version, "definition": value}


def definitions(store):
    defaults = [{"kind": "policy", "version": DEFAULT_POLICY, "definition": DEFAULT_SETTINGS, "known_at": None},
                *[{"kind": "scale", "version": k, "definition": v, "known_at": None} for k, v in DEFAULT_SCALES.items()]]
    with store.history() as db:
        if not initialized(db):
            return defaults
        saved = [{**dict(r), "definition": json.loads(r["payload"])} for r in db.execute("SELECT * FROM context_definitions ORDER BY kind,version")]
    keys = {(r["kind"], r["version"]) for r in saved}
    return [d for d in defaults if (d["kind"], d["version"]) not in keys] + [{k: v for k, v in r.items() if k != "payload"} for r in saved]


def initialized(db):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='context_versions'").fetchone() is not None


def owner(db, activity_id):
    row = db.execute("SELECT activity_id FROM activity_redirects WHERE old_id=?", (activity_id,)).fetchone()
    return row[0] if row else activity_id


def target_state(db, activity_id):
    target = owner(db, activity_id)
    activity = db.execute("SELECT sport FROM activities WHERE id=?", (target,)).fetchone()
    if activity is None:
        return {"activity_id": target, "sport": None, "status": "UNAVAILABLE", "canonical_version_id": None}
    canonical = db.execute("""SELECT v.id,v.payload FROM canonical_activity_heads h
        JOIN canonical_activity_versions v ON v.id=h.version_id WHERE h.activity_id=?""", (target,)).fetchone()
    return {"activity_id": target, "sport": json.loads(canonical["payload"])["summary"]["sport"] if canonical else activity[0],
            "status": "PRESENT", "canonical_version_id": canonical["id"] if canonical else None}


def normalize(db, raw, known_at):
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if not isinstance(kind, str) or kind not in MODELS:
        raise ValueError("Each record needs a supported kind.")
    record = MODELS[kind].model_validate(raw)
    value = record.model_dump(exclude={"supersedes", "correction_reason"})
    policy = definition(db, "policy", record.policy_version)
    value.update(algorithm_version=ALGORITHM_VERSION, parser_version=PARSER_VERSION, policy=policy)
    for field, rating in value.get("ratings", {}).items():
        scale = definition(db, "scale", rating["scale_version"])
        if not Decimal(scale["minimum"]) <= Decimal(rating["value"]) <= Decimal(scale["maximum"]):
            raise ValueError(f"{field} is outside the explicitly selected scale bounds.")
        rating["scale_definition"] = scale
    if record.kind in ("wellness", "strength"):
        value["event_utc"] = moment(record.observed_at) if record.observed_at else None
        value["time_precision"] = "instant" if record.observed_at else "local_date_only"
    elif record.kind in ("post_run", "preweek", "coverage", "fueling"):
        value["event_utc"] = moment(record.reported_at)
        value["time_precision"] = "instant"
    if record.kind in ("post_run", "fueling"):
        target = target_state(db, record.activity_id)
        if target["status"] == "UNAVAILABLE":
            raise ValueError("The feedback activity does not exist.")
        # Preserve the user-selected session identity; provider IDs can move to
        # another activity and must never silently retarget a historical report.
        value["target_at_entry"] = target
    if record.kind == "fueling":
        value.update(fueling_contract_version="fueling-practice-v1",
                     tolerance_status="REPORTED" if record.tolerance else "UNKNOWN")
    if record.kind == "wellness":
        value["interpretation"] = "DEVICE_OUTPUT" if record.origin == "device" or record.metric.lower() in DEVICE_METRICS else "REPORTED_OBSERVATION"
    if record.kind == "coverage" and record.status == "COMPLETE":
        end = datetime.combine(date.fromisoformat(record.last) + timedelta(days=1), time(), ZoneInfo(record.timezone))
        if end.astimezone(timezone.utc) > datetime.fromisoformat(known_at) or end.astimezone(timezone.utc) > datetime.fromisoformat(moment(record.reported_at)):
            raise ValueError("A partial current/future local day cannot be declared complete coverage.")
    return record, value


def reject_constant(value):
    raise ValueError("Non-finite JSON values are not supported.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are ambiguous and not supported.")
        result[key] = value
    return result


def parse(raw, format_name):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Context imports are limited to 2 MiB.")
    try:
        source = raw.decode("utf-8-sig")
        if format_name == "json":
            value = json.loads(source, parse_float=Decimal, parse_constant=reject_constant, object_pairs_hook=unique_object)
            rows = value if isinstance(value, list) else [value]
        elif format_name == "csv":
            reader = csv.DictReader(io.StringIO(source))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV needs unique column names.")
            rows = []
            for row in reader:
                if None in row:
                    raise ValueError("CSV rows must match their header.")
                row = {k: v for k, v in row.items() if v not in ("", None)}
                for field in ("ratings", "exercises", "attention"):
                    if field in row:
                        row[field] = json.loads(row[field], parse_float=Decimal, parse_constant=reject_constant, object_pairs_hook=unique_object)
                if "activity_id" in row:
                    row["activity_id"] = int(row["activity_id"])
                rows.append(row)
        else:
            raise ValueError("Choose json or csv in the context-export-v1 schema.")
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, RecursionError):
        raise ValueError("Use a UTF-8 JSON or CSV document in the documented context-export-v1 schema.") from None
    if not 1 <= len(rows) <= 1000:
        raise ValueError("Import between 1 and 1000 records.")
    return rows


def ingest(store, raw, format_name="json"):
    rows = parse(raw, format_name)
    sha = hashlib.sha256(raw).hexdigest()
    import_id = digest({"raw_sha256": sha, "format": format_name, "parser_version": PARSER_VERSION})
    # The archive uses its existing checksum/private-file protections. It is
    # written before committing references; a rejected import can leave only
    # unreferenced immutable bytes, never partially committed contextual records.
    store.archive.put(raw)
    with store.connect(write=True, create=True) as db:
        previous_import = db.execute("SELECT result FROM context_imports WHERE id=?", (import_id,)).fetchone()
        if previous_import:
            return {**json.loads(previous_import[0]), "replayed": True}
        known_at = now()
        results = []
        for index, raw_record in enumerate(rows):
            record, value = normalize(db, raw_record, known_at)
            record_id = digest({"kind": record.kind, "source": record.source, "source_id": record.source_id})
            existing = db.execute("SELECT * FROM context_versions WHERE record_id=? ORDER BY revision DESC LIMIT 1", (record_id,)).fetchone()
            if record.kind in ("post_run", "preweek"):
                if existing and record.kind == "preweek" and json.loads(existing["payload"])["week"] != record.week:
                    raise Conflict("A readiness report cannot change its target week. Record a missing/corrected report and a new target separately.")
                if not existing:
                    prior_reports = db.execute("""SELECT v.payload FROM context_records r JOIN context_versions v ON v.record_id=r.id
                        WHERE r.kind=? AND r.source=? AND v.revision=(SELECT MAX(revision) FROM context_versions WHERE record_id=r.id)""",
                        (record.kind, record.source)).fetchall()
                    for prior in prior_reports:
                        previous = json.loads(prior[0])
                        same_target = owner(db, previous["activity_id"]) == owner(db, record.activity_id) if record.kind == "post_run" else previous["week"] == record.week
                        if same_target:
                            raise Conflict("This source already has a check-in for the target. Correct its existing source record ID.")
            # Entry-time target metadata is immutable even after alias movement.
            if existing and record.kind in ("post_run", "fueling"):
                old = json.loads(existing["payload"])
                if record.activity_id != old["activity_id"]:
                    raise Conflict("A feedback report cannot change its target. Create a distinct source_id for another activity.")
                value["target_at_entry"] = old["target_at_entry"]
            if not existing and record.kind == "fueling":
                from fueling import entry_target
                value["target_at_entry"] = entry_target(db, record.activity_id, known_at)
            if existing and encoded(value) == existing["payload"]:
                version_id, revision, event_id = existing["id"], existing["revision"], digest({"version_id": existing["id"]})
                result = "unchanged"
            else:
                if existing and record.supersedes != existing["id"]:
                    raise Conflict("Record changed: supply the current version as supersedes and a correction reason.")
                if not existing and record.supersedes:
                    raise Conflict("Cannot correct a record that does not exist.")
                revision = existing["revision"] + 1 if existing else 1
                previous_id = existing["id"] if existing else None
                version_id = digest({"record_id": record_id, "previous_id": previous_id, "payload": value})
                db.execute("INSERT OR IGNORE INTO context_records VALUES (?,?,?,?,?)", (record_id, record.kind, record.source, record.source_id, known_at))
                db.execute("INSERT INTO context_versions VALUES (?,?,?,?,?,?,?)", (version_id, record_id, revision, previous_id, encoded(value), known_at, record.correction_reason))
                event_id = digest({"version_id": version_id})
                db.execute("INSERT INTO context_events VALUES (?,?,?,?,?)", (event_id, EVENTS[record.kind], version_id, value["event_utc"], known_at))
                result = "corrected" if existing else "created"
            results.append({"record_id": record_id, "version_id": version_id, "revision": revision, "event_id": event_id, "outcome": result, "item_index": index})
        report = {"import_id": import_id, "raw_sha256": sha, "parser_version": PARSER_VERSION, "known_at": known_at, "records": results, "replayed": False}
        db.execute("INSERT INTO context_imports VALUES (?,?,?,?,?)", (import_id, sha, PARSER_VERSION, known_at, encoded(report)))
        for row in results:
            db.execute("INSERT INTO context_sources VALUES (?,?,?)", (import_id, row["item_index"], row["version_id"]))
    return report


def eligible(value, cutoff):
    if value["event_utc"] and value["event_utc"] > cutoff:
        return False
    if value.get("soreness_reported_at") and moment(value["soreness_reported_at"]) > cutoff:
        return False
    if value["kind"] in ("wellness", "strength"):
        local_cutoff = datetime.fromisoformat(cutoff).astimezone(ZoneInfo(value["timezone"]))
        if value["local_date"] > local_cutoff.date().isoformat():
            return False
    return True


def decorate(db, row, cutoff):
    value = json.loads(row["payload"])
    refs = [dict(r) for r in db.execute("""SELECT s.import_id,s.item_index,i.raw_sha256,i.parser_version,i.known_at
        FROM context_sources s JOIN context_imports i ON i.id=s.import_id
        WHERE s.version_id=? AND i.known_at<=? ORDER BY i.known_at,s.import_id,s.item_index""", (row["id"], cutoff))]
    result = {**value, "record_id": row["record_id"], "version_id": row["id"], "revision": row["revision"],
              "previous_version_id": row["previous_id"], "known_at": row["known_at"], "correction_reason": row["correction_reason"],
              "first_known_at": db.execute("SELECT first_known_at FROM context_records WHERE id=?", (row["record_id"],)).fetchone()[0],
              "sources": refs, "eligible_at_cutoff": eligible(value, cutoff),
              "event_time_cutoff_status": "UNKNOWN_INSTANT" if value["time_precision"] == "local_date_only" else "TIMESTAMPED"}
    if value["kind"] in ("post_run", "fueling"):
        result["target_now"] = {**target_state(db, value["activity_id"]), "time_basis": "current_identity_not_as_of_evidence"}
    if value.get("time_precision") == "local_date_only":
        result["event_time_uncertainty"] = "Measurement instant unknown; known-at eligibility is not proof of an event-time cutoff."
    return result


def daily_groups(records, policy):
    groups = {}
    for row in records:
        if row["kind"] != "wellness":
            continue
        key = (row["local_date"], row["timezone"], row["metric"], row["unit"], row["source"], row["method"], row["value_type"], row["interpretation"])
        groups.setdefault(key, []).append(row)
    result = []
    for key, rows in sorted(groups.items()):
        valid = [r for r in rows if r["validity"] == "VALID" and r["value"] is not None]
        value = None
        state = "MISSING" if not valid else "OBSERVED" if len(valid) == 1 else "MULTIPLE_OBSERVATIONS"
        if len(valid) == 1:
            value = valid[0]["value"]
        elif len(valid) > 1 and policy["daily_aggregation"] == "median" and key[6] == "decimal":
            numbers = sorted(Decimal(r["value"]) for r in valid)
            middle = len(numbers) // 2
            with localcontext() as ctx:
                ctx.prec = 400
                value = format(numbers[middle] if len(numbers) % 2 else (numbers[middle - 1] + numbers[middle]) / 2, "f")
            state = "AGGREGATED"
        result.append(dict(zip(("local_date", "timezone", "metric", "unit", "source", "method", "value_type", "interpretation"), key)) |
                      {"state": state, "value": value, "valid_observations": len(valid), "observation_count": len(rows),
                       "version_ids": [r["version_id"] for r in rows], "missing_reasons": [r["missing_reason"] for r in rows if r["missing_reason"]],
                       "measurement_time_unknown": any(r["event_utc"] is None for r in rows),
                       "complete_day_coverage": "NOT_ESTABLISHED_BY_OBSERVATIONS"})
    return result


def read(store, *, as_of=None, first=None, last=None, policy_version=DEFAULT_POLICY):
    cutoff = moment(as_of) if as_of else now()
    if first is not None:
        day(first)
    if last is not None:
        day(last)
    if first and last and first > last:
        raise ValueError("From must be on or before To.")
    records, withheld, events = [], [], []
    policy = DEFAULT_SETTINGS
    with store.history() as db:
        if initialized(db):
            policy = definition(db, "policy", policy_version, cutoff)
            rows = db.execute("""SELECT v.* FROM context_versions v JOIN
                (SELECT record_id,MAX(revision) AS revision FROM context_versions WHERE known_at<=? GROUP BY record_id) h
                ON h.record_id=v.record_id AND h.revision=v.revision ORDER BY v.record_id""", (cutoff,)).fetchall()
            for row in rows:
                value = decorate(db, row, cutoff)
                record_day = value.get("local_date") or value.get("week") or datetime.fromisoformat(value["event_utc"]).astimezone(ZoneInfo(value.get("timezone", "Europe/London"))).date().isoformat()
                if value["kind"] == "coverage":
                    if (first and value["last"] < first) or (last and value["first"] > last):
                        continue
                elif (first and record_day < first) or (last and record_day > last):
                    continue
                (records if value["eligible_at_cutoff"] else withheld).append(value)
            visible_ids = {r["version_id"] for r in records}
            events = [dict(r) for r in db.execute("SELECT * FROM context_events WHERE known_at<=? ORDER BY known_at,id", (cutoff,)) if r["version_id"] in visible_ids]
        elif policy_version != DEFAULT_POLICY:
            raise ValueError("Unknown policy version.")
    daily = daily_groups(records, policy)
    wellness = {}
    for metric in sorted(set(policy["expected_wellness_metrics"]) | {r["metric"] for r in daily}):
        relevant = [r for r in daily if r["metric"] == metric]
        wellness[metric] = {"state": "OBSERVED" if any(r["valid_observations"] for r in relevant) else "MISSING",
                            "observed_days": len({r["local_date"] for r in relevant if r["valid_observations"]}),
                            "missing_reason": None if any(r["valid_observations"] for r in relevant) else "No valid supplied observation in this selection.",
                            "units": sorted({r["unit"] for r in relevant}), "coverage": "UNKNOWN_UNLESS_EXPLICITLY_REPORTED"}
    coverage_reports = [r for r in records if r["kind"] == "coverage"]
    for metric, state in wellness.items():
        state["coverage_reports"] = [{k: r[k] for k in ("record_id", "version_id", "source", "first", "last", "status", "basis", "reason", "known_at")}
                                     for r in coverage_reports if r["domain"] == "wellness" and r["metric"] in (None, metric)]
        if state["coverage_reports"]:
            state["coverage"] = "EXPLICIT_REPORTS_AVAILABLE"
    signals = [{"kind": flag, "record_id": r["record_id"], "version_id": r["version_id"], "reported_at": r["reported_at"],
                "known_at": r["known_at"], "target": r.get("activity_id", r.get("week")), "action": "PLANNER_REVIEW_ONLY"}
               for r in records for flag in r.get("attention", [])]
    counts = {kind: sum(r["kind"] == kind and not r.get("missing_reason") for r in records) for kind in KINDS}
    return {"as_of": cutoff, "selection": {"from": first, "to": last}, "policy_version": policy_version, "policy": policy,
            "algorithm_version": ALGORITHM_VERSION, "records": records, "withheld": withheld, "events": events,
            "daily_wellness": daily, "wellness": wellness, "attention_signals": signals, "report_counts": counts,
            "feedback_coverage": "Observed reports only; missing reports are not recovery or illness. Use a target query for a missing check-in.",
            "strength_running_distance_m": "0", "clinical_interpretation": "NOT_PERFORMED",
            "date_only_policy": "Known-at and local-date eligibility; measurement instant remains unknown. A partial current day never establishes complete-day coverage."}


def event_log(store, *, after=0, limit=200, as_of=None):
    if type(after) is not int or not 0 <= after <= MAX_SQLITE_INTEGER or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError(f"Use an after cursor from 0 to {MAX_SQLITE_INTEGER} and a limit from 1 to 1000.")
    cutoff = moment(as_of) if as_of else now()
    events = []
    with store.history() as db:
        if initialized(db):
            for row in db.execute("SELECT rowid AS sequence,* FROM context_events WHERE rowid>? AND known_at<=? ORDER BY rowid LIMIT ?", (after, cutoff, limit)):
                version = db.execute("SELECT * FROM context_versions WHERE id=?", (row["version_id"],)).fetchone()
                value = decorate(db, version, cutoff)
                events.append({**dict(row), "record_id": value["record_id"], "previous_version_id": value["previous_version_id"],
                               "source": value["source"], "source_id": value["source_id"], "sources": value["sources"],
                               "algorithm_version": value["algorithm_version"], "policy_version": value["policy_version"],
                               "eligible_at_cutoff": value["eligible_at_cutoff"], "event_time_cutoff_status": value["event_time_cutoff_status"]})
    return {"as_of": cutoff, "after": after, "next_after": events[-1]["sequence"] if events else after, "events": events}


def history(store, record_id, *, as_of=None):
    cutoff = moment(as_of) if as_of else now()
    with store.history() as db:
        if not initialized(db):
            return None
        rows = db.execute("SELECT * FROM context_versions WHERE record_id=? AND known_at<=? ORDER BY revision", (record_id, cutoff)).fetchall()
        if not rows:
            return None
        return {"record_id": record_id, "as_of": cutoff, "versions": [decorate(db, row, cutoff) for row in rows]}


def target_feedback(store, *, activity_id=None, week=None, as_of=None):
    if (activity_id is None) == (week is None):
        raise ValueError("Choose exactly one activity or week target.")
    if activity_id is not None and (type(activity_id) is not int or not 1 <= activity_id <= MAX_SQLITE_INTEGER):
        raise ValueError(f"Use an activity ID from 1 to {MAX_SQLITE_INTEGER}.")
    if week is not None and date.fromisoformat(day(week)).weekday() != 0:
        raise ValueError("Week must be a Monday.")
    data = read(store, as_of=as_of)
    with store.history() as db:
        if activity_id is not None:
            if db is None or not db.execute("SELECT 1 FROM activities WHERE id=?", (owner(db, activity_id),)).fetchone():
                raise ValueError("Activity not found.")
            resolved = owner(db, activity_id)
            rows = [r for r in data["records"] if r["kind"] == "post_run" and owner(db, r["activity_id"]) == resolved]
        else:
            rows = [r for r in data["records"] if r["kind"] == "preweek" and r["week"] == week]
    supplied = [r for r in rows if not r.get("missing_reason")]
    return {"state": "MULTIPLE_REPORTS" if len(supplied) > 1 else "REPORTED" if supplied else "MISSING", "reports": rows, "missing_reason": None if supplied else "No check-in supplied at this cutoff.",
            "identity_basis": "current redirects; original target and entry identity retained in each report"}


def main():
    from storage import Store
    parser = argparse.ArgumentParser(description="Import or inspect optional context-export-v1 JSON/CSV without network access.")
    parser.add_argument("--database", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("file")
    imp.add_argument("--format", choices=("json", "csv"), default="json")
    show = sub.add_parser("show")
    show.add_argument("--as-of")
    show.add_argument("--policy-version", default=DEFAULT_POLICY)
    args = parser.parse_args()
    try:
        store = Store(args.database)
        result = ingest(store, Path(args.file).read_bytes(), args.format) if args.command == "import" else read(store, as_of=args.as_of, policy_version=args.policy_version)
        print(encoded(result))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, "Context operation failed: " + (str(exc) if isinstance(exc, ValueError) else "local storage unavailable") + "\n")


if __name__ == "__main__":
    main()
