"""Immutable running census assertions and sourced intensity inputs (G3)."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

VERSION = "g3-inputs-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS workload_input_versions (
    id TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
    previous_id TEXT, payload TEXT NOT NULL, known_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
    UNIQUE(record_id, revision)
);
CREATE TABLE IF NOT EXISTS workload_imports (
    id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL, known_at TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workload_snapshots (
    id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, inputs TEXT NOT NULL, result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workload_processing_inventories (
    id TEXT PRIMARY KEY, known_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class Conflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Use an absolute ISO timestamp with a UTC offset.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an absolute ISO timestamp with a UTC offset.") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A timestamp needs an explicit UTC offset.")
    return result.astimezone(timezone.utc)


def utc(value):
    return stamp(value).isoformat(timespec="microseconds")


def encoded(value):
    def convert(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=convert)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


Text = Annotated[str, Field(min_length=1, max_length=4000)]
Identifier = Annotated[str, Field(min_length=1, max_length=120)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Amount = Annotated[Decimal, Field(ge=0, max_digits=60, decimal_places=30, allow_inf_nan=False)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Identifier
    source_id: Identifier
    actor: Identifier
    reason: Text
    source_refs: list[dict] = Field(min_length=1, max_length=100)
    supersedes: Hash | None = None
    correction_reason: Text | None = None

    @model_validator(mode="after")
    def correction(self):
        if bool(self.supersedes) != bool(self.correction_reason):
            raise ValueError("A correction needs both supersedes and correction_reason.")
        if not self.actor.strip() or not self.reason.strip() or not all(self.source_refs):
            raise ValueError("Provide an actor, reason and nonempty source references.")
        return self


class Coverage(Input):
    kind: Literal["coverage"]
    basis: Literal["START", "END", "RECORDED_LOCAL_DATE"]
    lower_utc: str
    upper_utc: str
    lower_inclusive: bool = True
    upper_inclusive: bool = False
    observed_through: str
    reported_at: str
    status: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"]
    evidence_kind: Literal["ATHLETE_ATTESTATION", "IMPORT_MANIFEST", "PROVIDER_CENSUS"]
    exclusions: list[Text] = Field(default_factory=list, max_length=100)
    boundary_timezone: Literal["Europe/London", "UTC"] = "UTC"
    original_local_bounds: dict | None = None

    @field_validator("lower_utc", "upper_utc", "observed_through", "reported_at")
    @classmethod
    def clocks(cls, value):
        return utc(value)

    @model_validator(mode="after")
    def interval(self):
        if stamp(self.lower_utc) >= stamp(self.upper_utc):
            raise ValueError("Coverage must have a positive interval.")
        if self.status == "COMPLETE" and (self.exclusions or stamp(self.upper_utc) > stamp(self.observed_through)):
            raise ValueError("Complete coverage cannot exceed observation or contain unresolved exclusions.")
        if stamp(self.observed_through) > stamp(self.reported_at):
            raise ValueError("Observation cannot be after its report timestamp.")
        return self


class Band(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Identifier
    lower: Decimal | None = Field(None, allow_inf_nan=False)
    upper: Decimal | None = Field(None, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounds(self):
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ValueError("Zone lower must be less than upper.")
        return self


class ZoneProfile(Input):
    kind: Literal["zone_profile"]
    method: Text
    system: Literal["DEVICE_NATIVE_FIVE", "RESEARCH_THREE", "OTHER"]
    metric: Identifier
    unit: Identifier
    effective_from: str
    effective_to: str | None = None
    reliability: Literal["CALIBRATED", "DEVICE_REPORTED", "UNCALIBRATED"]
    calibration: Text
    bands: list[Band] = Field(min_length=1, max_length=20)

    @field_validator("effective_from", "effective_to")
    @classmethod
    def clocks(cls, value):
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def profile(self):
        if len({b.label for b in self.bands}) != len(self.bands):
            raise ValueError("Band labels must be unique.")
        expected = {"DEVICE_NATIVE_FIVE": 5, "RESEARCH_THREE": 3}.get(self.system)
        if expected and len(self.bands) != expected:
            raise ValueError(f"{self.system} needs exactly {expected} separately named bands.")
        if self.effective_to and stamp(self.effective_to) <= stamp(self.effective_from):
            raise ValueError("Profile effective_to must follow effective_from.")
        return self


class BandLedger(Input):
    kind: Literal["band_ledger"]
    canonical_version_id: Hash
    profile_version_id: Hash
    duration_basis: Literal["elapsed_seconds", "timer_seconds", "moving_seconds"]
    seconds: dict[str, Amount] = Field(min_length=1, max_length=20)
    unclassified_seconds: Amount = Decimal(0)
    measured_at: str
    measurement_method: Text

    @field_validator("measured_at")
    @classmethod
    def clock(cls, value):
        return utc(value)


Adapter = TypeAdapter(Annotated[Coverage | ZoneProfile | BandLedger, Field(discriminator="kind")])


def table(db, name):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def decorate(row):
    return {**json.loads(row["payload"]), "version_id": row["id"], "record_id": row["record_id"],
            "revision": row["revision"], "previous_version_id": row["previous_id"],
            "known_at": row["known_at"], "raw_sha256": row["raw_sha256"], "input_version": VERSION}


def selected(db, cutoff):
    if not table(db, "workload_input_versions"):
        return []
    latest = {}
    for row in db.execute("SELECT * FROM workload_input_versions WHERE known_at<=? ORDER BY revision, id", (utc(cutoff),)):
        latest[row["record_id"]] = decorate(row)
    return sorted(latest.values(), key=lambda row: (row["kind"], row["record_id"]))


def history(store, record_id):
    with store.history() as db:
        if not table(db, "workload_input_versions"):
            return []
        return [decorate(r) for r in db.execute("SELECT * FROM workload_input_versions WHERE record_id=? ORDER BY revision", (record_id,))]


def ingest(store, raw):
    if len(raw) > 2_000_000:
        raise ValueError("Workload imports are limited to 2 MB.")
    try:
        values = json.loads(raw, parse_float=Decimal, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite number")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Import a JSON record or list of records.") from None
    values = values if isinstance(values, list) else [values]
    if not 1 <= len(values) <= 1000:
        raise ValueError("Import 1–1000 records.")
    records = [Adapter.validate_python(v).model_dump(mode="json") for v in values]
    raw_sha = store.archive.put(raw)
    import_id = digest({"raw": raw_sha, "parser": VERSION})
    with store.connect(write=True, create=True) as db:
        existing = db.execute("SELECT result FROM workload_imports WHERE id=?", (import_id,)).fetchone()
        if existing:
            return {**json.loads(existing[0]), "replayed": True}
        # A census assertion cannot certify that all imported rows were normalized.
        # Preserve a separate processing observation in this same input transaction.
        import workload_processing
        known = workload_processing.observe(db, persist=True)["known_at"]
        saved = []
        for value in records:
            for key in ("reported_at", "measured_at"):
                if value.get(key) and stamp(value[key]) > stamp(known):
                    raise ValueError(f"{key} cannot be in the future.")
            rid = digest({"kind": value["kind"], "source": value["source"], "source_id": value["source_id"]})
            prior = db.execute("SELECT * FROM workload_input_versions WHERE record_id=? ORDER BY revision DESC LIMIT 1", (rid,)).fetchone()
            identical = db.execute("SELECT * FROM workload_input_versions WHERE record_id=? AND payload=?", (rid, encoded(value))).fetchone()
            if identical:
                saved.append(decorate(identical))
                continue
            if (prior and value["supersedes"] != prior["id"]) or (not prior and value["supersedes"]):
                raise Conflict("A changed record must name its current version in supersedes and explain the correction.")
            if value["kind"] == "band_ledger":
                profile = db.execute("SELECT payload FROM workload_input_versions WHERE id=? AND kind='zone_profile'", (value["profile_version_id"],)).fetchone()
                activity = db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (value["canonical_version_id"],)).fetchone()
                if not profile or not activity:
                    raise ValueError("A band ledger must reference existing exact profile and canonical versions.")
                if not json.loads(activity[0])["running_sport"]:
                    raise ValueError("Running intensity needs a running canonical version.")
                labels = {b["label"] for b in json.loads(profile[0])["bands"]}
                if set(value["seconds"]) != labels:
                    raise ValueError("Supply seconds for every profile band, including explicit zeroes.")
            version_id = digest({"record_id": rid, "previous": prior["id"] if prior else None, "payload": value})
            db.execute("INSERT INTO workload_input_versions VALUES (?,?,?,?,?,?,?,?)",
                       (version_id, rid, value["kind"], prior["revision"] + 1 if prior else 1,
                        prior["id"] if prior else None, encoded(value), known, raw_sha))
            saved.append(decorate(db.execute("SELECT * FROM workload_input_versions WHERE id=?", (version_id,)).fetchone()))
        result = {"import_id": import_id, "raw_sha256": raw_sha, "known_at": known, "records": saved, "replayed": False}
        db.execute("INSERT INTO workload_imports VALUES (?,?,?,?)", (import_id, raw_sha, known, encoded(result)))
        return result


def provider_evidence(store, scan_id, actor, reason):
    """Freeze present scan facts; never promote recorded-local-date to START/END."""
    with store.history() as db:
        if not table(db, "garmin_scans"):
            raise ValueError("Provider scan not found.")
        scan = db.execute("SELECT s.*, i.started_at, i.finished_at, i.state FROM garmin_scans s JOIN imports i ON i.id=s.import_id WHERE s.id=?", (scan_id,)).fetchone()
        if not scan:
            raise ValueError("Provider scan not found.")
        items = [dict(r) for r in db.execute("SELECT * FROM garmin_scan_items WHERE scan_id=? ORDER BY garmin_id", (scan_id,))]
        evidence = {"scan": dict(scan), "items": items, "captured_at": now(),
                    "actor": actor, "reason": reason, "basis": "RECORDED_LOCAL_DATE",
                    "limitation": "Current mutable scan captured now; does not establish canonical START/END census or original historical knowledge."}
    # Save as an UNKNOWN assertion with an immutable raw snapshot as its reference.
    from datetime import date, time, timedelta
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Europe/London")
    lower = datetime.combine(date.fromisoformat(scan["first"]), time(), zone)
    upper = datetime.combine(date.fromisoformat(scan["last"]) + timedelta(days=1), time(), zone)
    raw_sha = store.archive.put(encoded(evidence).encode())
    value = {"kind": "coverage", "source": "garmin-scan-capture", "source_id": digest(evidence),
             "actor": actor, "reason": reason, "source_refs": [{"raw_sha256": raw_sha, "scan_id": scan_id, "import_id": scan["import_id"]}],
             "basis": "RECORDED_LOCAL_DATE", "lower_utc": lower.isoformat(), "upper_utc": upper.isoformat(),
             "observed_through": scan["finished_at"] or scan["started_at"], "reported_at": evidence["captured_at"],
             "status": "UNKNOWN", "evidence_kind": "PROVIDER_CENSUS", "boundary_timezone": "Europe/London",
             "exclusions": [evidence["limitation"]]}
    return ingest(store, encoded(value).encode())
