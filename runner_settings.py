"""The runner's race goal and preferences, with immutable, independently selected revisions."""
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

import availability
from availability import Input, calendar_day, digest, encoded
from workload_inputs import utc


Kind = Literal["goal", "preferences"]
SPEC_SOURCE = "Marathon Coach Developer Master Implementation Specification v1.0"
RACE_DATE = "2027-03-14"
REVIEW_DATE = "2027-02-07"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_setting_revisions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL, decided_at TEXT,
    request_id TEXT UNIQUE, request_hash TEXT, payload TEXT NOT NULL,
    UNIQUE(kind, revision)
)
"""


class Conflict(ValueError):
    pass


def meaningful(value):
    if not value.strip() or "\x00" in value:
        raise ValueError("Enter meaningful text.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Use valid Unicode text.") from None
    return value


Text = Annotated[str, Field(max_length=8000), AfterValidator(meaningful)]


class Submission(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: Annotated[Text, Field(max_length=100)]
    reason: Text


class Goal(Submission):
    action: Literal["REVISE_GOAL"]
    target_duration_s: int = Field(gt=0, le=2**53 - 1)


class Constraint(Input):
    scope: Text
    text: Text
    from_date: str | None = None
    through_date: str | None = None

    @model_validator(mode="after")
    def date_range(self):
        first = calendar_day(self.from_date) if self.from_date is not None else None
        last = calendar_day(self.through_date) if self.through_date is not None else None
        if first and last and last < first:
            raise ValueError("The last local date must be on or after the first.")
        return self


class Preferences(Submission):
    action: Literal["SET_PREFERENCES"]
    max_sessions_per_day: int | None = Field(ge=0, le=10)
    strength_exercise_choices: Text | None
    special_constraints: list[Constraint] | None


def original(kind):
    if kind == "goal":
        values = {"target_duration_s": 9000, "race_local_date": RACE_DATE, "review_date": REVIEW_DATE,
                  "timezone": "Europe/London", "source": SPEC_SOURCE, "changed_by_user": False}
    elif kind == "preferences":
        values = {"max_sessions_per_day": None, "strength_exercise_choices": None,
                  "special_constraints": None, "strength_exercise_owner": "RUNNER",
                  "strength_loading_owner": "RUNNER", "timezone": "Europe/London", "source": "NOT_ENTERED"}
    else:
        raise ValueError("Use goal or preferences.")
    record = {"kind": kind, "schema_version": "runner-settings-v1", "revision": 0,
              "previous_version_id": None, "decided_at": None, "actor": None, "reason": None, **values}
    return {**record, "version_id": digest(record)}


def initialized(db):
    return db is not None and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runner_setting_revisions'").fetchone()


def baseline(db, kind):
    saved = initialized(db) and db.execute(
        "SELECT payload FROM runner_setting_revisions WHERE kind=? AND revision=0", (kind,)).fetchone()
    return json.loads(saved["payload"]) if saved else original(kind)


def read_record(db, kind: Kind, *, version_id=None, as_of=None):
    """Resolve an exact version or the latest decision known at a cutoff in an existing transaction."""
    cutoff = utc(as_of) if as_of is not None else None
    base = baseline(db, kind)
    if version_id is not None and not re.fullmatch(r"[a-f0-9]{64}", version_id):
        raise ValueError("Use a complete settings version ID.")
    if version_id == base["version_id"]:
        return base
    if initialized(db):
        row = db.execute("""SELECT payload FROM runner_setting_revisions WHERE kind=? AND revision>0
            AND (? IS NULL OR id=?) AND (? IS NULL OR decided_at<=?) ORDER BY revision DESC LIMIT 1""",
            (kind, version_id, version_id, cutoff, cutoff)).fetchone()
        if row:
            return json.loads(row["payload"])
    return base if version_id is None else None


def read(store, kind: Kind, *, version_id=None, as_of=None):
    cutoff = utc(as_of) if as_of is not None else None
    with store.history() as db:
        record = read_record(db, kind, version_id=version_id, as_of=cutoff)
        if version_id is not None:
            return record
        versions = [dict(row) for row in db.execute("""SELECT id AS version_id,revision,decided_at
            FROM runner_setting_revisions WHERE kind=? AND revision>0 AND (? IS NULL OR decided_at<=?)
            ORDER BY revision DESC""", (kind, cutoff, cutoff))] if initialized(db) else []
        base = baseline(db, kind)
    versions.append({key: base[key] for key in ("version_id", "revision", "decided_at")})
    return {"current": record, "versions": versions, "as_of": cutoff}


def calendar_limits(db, week, *, preferences_version=None, availability_version=None, as_of=None):
    """Intersect independently selected limits without changing the declared calendar."""
    first, _, _, _ = availability.week_bounds(week)
    cutoff = utc(as_of) if as_of is not None else now()
    preferences = read_record(db, "preferences", version_id=preferences_version, as_of=cutoff)
    calendar = (availability.read_version(db, week, availability_version) if availability_version is not None
                else availability.read_current(db, week, known_by_utc=cutoff))
    if preferences is None or (availability_version is not None and (calendar is None or calendar["received_at"] > cutoff)):
        raise LookupError("The requested preferences or availability revision is unavailable at this cutoff.")
    cap = preferences["max_sessions_per_day"]
    days = []
    for index in range(7):
        day = calendar["days"][index] if calendar else {
            "date": (first + timedelta(days=index)).isoformat(), "state": "UNKNOWN",
            "windows": [], "max_sessions": None, "doubles_allowed": False}
        limit = day["max_sessions"]
        if cap is not None and limit is not None:
            limit = min(cap, limit)
        days.append({"date": day["date"], "state": day["state"], "window_ids": [w["id"] for w in day["windows"]],
                     "declared_max_sessions": day["max_sessions"], "effective_max_sessions": limit,
                     "doubles_permitted": day["doubles_allowed"] and limit is not None and limit > 1})
    return {"week": week, "as_of": cutoff, "preferences_version": preferences["version_id"],
            "availability_version": calendar["version_id"] if calendar else None, "preferences_cap": cap, "days": days}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def save(store, kind: Kind, raw):
    submission = (Goal if kind == "goal" else Preferences).model_validate(raw)
    request_hash = digest({"kind": kind, "body": submission.model_dump()})
    with store.connect(create=True, write=True) as db:
        db.execute(SCHEMA)
        replay = db.execute("SELECT request_hash,payload FROM runner_setting_revisions WHERE request_id=?",
                            (submission.request_id,)).fetchone()
        if replay:
            if replay["request_hash"] != request_hash:
                raise Conflict("This request ID was used for a different decision.")
            return {"record": json.loads(replay["payload"]), "replayed": True}
        previous = read_record(db, kind)
        if submission.expected_revision != previous["version_id"]:
            raise Conflict("These settings changed. Reload the current revision before saving.")
        if kind == "goal" and submission.target_duration_s == previous["target_duration_s"]:
            raise ValueError("Enter a changed target to record a goal revision.")
        values = submission.model_dump(exclude={"request_id", "expected_revision"})
        if previous["revision"] == 0:
            # On the first decision, pin the exact displayed baseline alongside its revision.
            db.execute("""INSERT INTO runner_setting_revisions(id,kind,revision,payload)
                          VALUES (?,?,0,?)""", (previous["version_id"], kind, encoded(previous)))
        record = {**previous, **values, "source": "RUNNER_DECISION", "request_id": submission.request_id,
                  "revision": previous["revision"] + 1, "previous_version_id": previous["version_id"],
                  "decided_at": now()}
        if kind == "goal":
            record["changed_by_user"] = True
        del record["version_id"]
        record["version_id"] = digest(record)
        db.execute("INSERT INTO runner_setting_revisions VALUES (?,?,?,?,?,?,?)",
                   (record["version_id"], kind, record["revision"], record["decided_at"],
                    submission.request_id, request_hash, encoded(record)))
    return {"record": record, "replayed": False}
