"""Versioned, runner-entered running workout templates and exact block accounting."""
from decimal import Decimal, localcontext
import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from availability import Conflict, Input, digest, encoded
from runner_settings import Text
import workload_inputs as inputs
from workout_labels import TYPES


SCHEMA = """
CREATE TABLE IF NOT EXISTS workout_template_versions (
 id TEXT PRIMARY KEY, template_id TEXT NOT NULL, revision INTEGER NOT NULL,
 known_at TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 payload TEXT NOT NULL, UNIQUE(template_id, revision)
)
"""
VERSION = "workout-template-v1"
CATEGORIES = tuple(t for t in TYPES if t not in {"UNKNOWN", "STRENGTH_CONDITIONING"})
ROLES = ("LONG_RUN", "PRIMARY_QUALITY", "SECOND_QUALITY", "EASY")
KINDS = ("WARM_UP", "WORK", "ACTIVE_RECOVERY", "COOL_DOWN", "EASY_VOLUME")
Key = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Metres = Annotated[int, Field(ge=0, le=2**31-1)]
ShortText = Annotated[Text, Field(max_length=200)]


def exact(value):
    if type(value) not in (Decimal, str, int):
        raise ValueError("Use a decimal number.")
    try:
        number = Decimal(value)
    except ArithmeticError:
        raise ValueError("Use a decimal number.") from None
    if not number.is_finite() or number.adjusted() > 18 or number.as_tuple().exponent < -60:
        raise ValueError("Use a finite value with up to 60 decimal places.")
    return number


Quantity = Annotated[Decimal, BeforeValidator(exact), Field(ge=0)]


class Pace(Input):
    lower: Quantity
    upper: Quantity
    unit: Literal["s/km", "min/km", "s/m"]
    method: ShortText | None = None
    source_ref: Text | None = None
    calibration: Text | None = None
    evidence_known_at: str | None = None
    uncertainty: Text | None = None
    status: Literal["SUPPORTED", "UNVERIFIED"]

    @field_validator("evidence_known_at")
    @classmethod
    def absolute_time(cls, value):
        return inputs.utc(value) if value is not None else None

    @model_validator(mode="after")
    def bounds(self):
        if self.lower <= 0 or self.upper < self.lower:
            raise ValueError("Give positive pace bounds from fastest to slowest.")
        if self.status == "SUPPORTED" and not all((self.method, self.source_ref, self.calibration,
                                                  self.evidence_known_at, self.uncertainty)):
            raise ValueError("Supported pace needs a method, source, calibration, knowledge time and uncertainty.")
        return self


class Zone(Input):
    method: ShortText
    band: ShortText
    calibration: Text | None = None


class Block(Input):
    block_id: Key
    kind: Literal[*KINDS]
    distance_m: Metres
    intensity: Text
    intensity_method: ShortText
    protected: bool = False
    min_m: Metres | None = None
    max_m: Metres | None = None
    allow_split: bool = False
    rest_after_s: Quantity = Decimal(0)
    pace: Pace | None = None
    zone: Zone | None = None

    @model_validator(mode="after")
    def adjustment(self):
        if (self.min_m is None) != (self.max_m is None):
            raise ValueError("Give both easy-volume limits or leave both empty.")
        if self.min_m is not None and not self.min_m <= self.distance_m <= self.max_m:
            raise ValueError("The entered distance must fall within its permitted limits.")
        fixed = self.min_m is None or self.min_m == self.max_m
        if (self.protected or self.kind == "WORK") and (not fixed or self.allow_split):
            raise ValueError("Work and protected blocks retain their distance and repetition structure.")
        if self.distance_m == 0 and self.kind == "WORK":
            raise ValueError("A work repetition needs positive running distance.")
        return self


class Submission(Input):
    request_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9._:-]{1,128}$")]
    expected_revision: Hash | None
    actor: ShortText
    reason: Text
    name: ShortText
    primary: Literal[*CATEGORIES]
    role: Literal[*ROLES]
    tags: list[Annotated[Text, Field(max_length=80)]] = Field(default_factory=list, max_length=32)
    blocks: list[Block] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def distinct(self):
        if len({b.block_id for b in self.blocks}) != len(self.blocks):
            raise ValueError("Give each ordered block a distinct ID.")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("Use distinct secondary tags.")
        if not any(b.distance_m for b in self.blocks):
            raise ValueError("A running template needs a positive total distance.")
        return self


class Adjustments(Input):
    distances_m: dict[Key, Metres]


def account(blocks, distances_m=None):
    """Reconcile exact metres, supplied block pace evidence and explicit stationary rest."""
    chosen = distances_m or {}
    if chosen.keys() - {b["block_id"] for b in blocks}:
        raise ValueError("An adjustment refers to an unknown block.")
    rows = []
    total = work = 0
    low = high = rest = Decimal(0)
    missing = []
    with localcontext() as ctx:
        ctx.prec = 130
        for b in blocks:
            key = b["block_id"]
            distance = chosen.get(key, b["distance_m"])
            minimum = b["distance_m"] if b["min_m"] is None else b["min_m"]
            maximum = b["distance_m"] if b["max_m"] is None else b["max_m"]
            if not minimum <= distance <= maximum:
                raise ValueError(f"Block {key} permits {minimum} to {maximum} metres.")
            total += distance
            if b["kind"] == "WORK":
                work += distance
            seconds = Decimal(b["rest_after_s"])
            rest += seconds
            low += seconds
            high += seconds
            pace = b["pace"]
            lo = hi = None
            if distance == 0:
                lo = hi = Decimal(0)
            elif pace and pace["status"] == "SUPPORTED":
                factor = {"s/km": Decimal(".001"), "min/km": Decimal(".06"), "s/m": Decimal(1)}[pace["unit"]]
                lo = Decimal(pace["lower"]) * factor * distance
                hi = Decimal(pace["upper"]) * factor * distance
                low += lo
                high += hi
            else:
                missing.append(key)
            zone = b["zone"]
            rows.append({"block_id": key, "distance_m": distance, "min_m": minimum, "max_m": maximum,
                         "running_lower_s": str(lo) if lo is not None else None,
                         "running_upper_s": str(hi) if hi is not None else None,
                         "zone_status": "METHOD_AND_CALIBRATION_RECORDED" if zone and zone["calibration"] else "ZONE_UNVERIFIED"})
    return {"total_m": total, "work_m": work, "easy_m": total-work, "rest_s": str(rest),
            "duration_lower_s": None if missing else str(low), "duration_upper_s": None if missing else str(high),
            "duration_status": "FEASIBILITY_UNCERTAIN" if missing else "SUPPLIED_PACE_BOUNDS",
            "missing_pace_block_ids": missing, "blocks": rows}


def read_version(db, template_id, *, version_id=None, as_of=None):
    """Use the caller's transaction for a named revision or latest revision known by K."""
    cutoff = inputs.utc(as_of) if as_of is not None else None
    if not inputs.table(db, "workout_template_versions"):
        return None
    row = db.execute("""SELECT payload FROM workout_template_versions WHERE template_id=?
        AND (? IS NULL OR id=?) AND (? IS NULL OR known_at<=?) ORDER BY revision DESC LIMIT 1""",
        (template_id, version_id, version_id, cutoff, cutoff)).fetchone()
    return json.loads(row[0]) if row else None


def index(db, *, template_id=None, as_of=None):
    cutoff = inputs.utc(as_of) if as_of is not None else None
    if not inputs.table(db, "workout_template_versions"):
        return []
    rows = db.execute("""SELECT template_id,revision,id AS version_id,known_at,
        json_extract(payload,'$.name') AS name FROM workout_template_versions
        WHERE (? IS NULL OR known_at<=?) AND (? IS NULL OR template_id=?)
        ORDER BY template_id,revision DESC""", (cutoff, cutoff, template_id, template_id))
    result = []
    for row in rows:
        if template_id is not None or not result or result[-1]["template_id"] != row["template_id"]:
            result.append(dict(row))
    return result


def save(store, key, raw):
    submission = Submission.model_validate(raw)
    payload = submission.model_dump(mode="json")
    request_hash = digest({"template_id": key, "submission": payload})
    with store.connect(create=True, write=True) as db:
        db.execute(SCHEMA)
        replay = db.execute("SELECT request_hash,payload FROM workout_template_versions WHERE request_id=?",
                            (submission.request_id,)).fetchone()
        if replay:
            if replay[0] != request_hash:
                raise Conflict("This request ID has already recorded a different template change.")
            return {"record": json.loads(replay[1]), "replayed": True}
        previous = read_version(db, key)
        if submission.expected_revision != (previous["version_id"] if previous else None):
            raise Conflict("The template changed. Reopen the current revision before editing.")
        known = inputs.now()
        if any(b.pace and b.pace.evidence_known_at and b.pace.evidence_known_at > known for b in submission.blocks):
            raise ValueError("Pace evidence cannot be first known after this submission.")
        del payload["expected_revision"]
        record = {**payload, "template_id": key, "schema_version": VERSION, "known_at": known,
                  "revision": previous["revision"]+1 if previous else 1,
                  "previous_version_id": submission.expected_revision, "accounting": account(payload["blocks"])}
        record["version_id"] = digest(record)
        db.execute("INSERT INTO workout_template_versions VALUES (?,?,?,?,?,?,?)",
                   (record["version_id"], key, record["revision"], known, submission.request_id, request_hash, encoded(record)))
    return {"record": record, "replayed": False}
