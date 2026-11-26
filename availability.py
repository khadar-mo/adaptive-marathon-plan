"""Immutable runner-entered calendar windows; no training-dose or scheduling decisions."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


ZONE = "Europe/London"
POLICY_VERSION = "availability-london-start-day-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS availability_revisions (
    id TEXT PRIMARY KEY, week TEXT NOT NULL, revision INTEGER NOT NULL,
    previous_id TEXT REFERENCES availability_revisions(id),
    received_at TEXT NOT NULL, actor TEXT NOT NULL,
    request_id TEXT NOT NULL, request_hash TEXT NOT NULL, payload TEXT NOT NULL,
    UNIQUE(week, revision), UNIQUE(week, request_id)
);
"""


class Conflict(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def calendar_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a calendar date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def week_bounds(value):
    first = calendar_day(value)
    if first.weekday() != 0:
        raise ValueError("Choose the Monday beginning the availability week.")
    try:
        last = first + timedelta(days=7)
    except OverflowError:
        raise ValueError("The complete week must fit within supported calendar dates.") from None
    zone = ZoneInfo(ZONE)
    start = datetime.combine(first, time(), zone).astimezone(timezone.utc)
    end = datetime.combine(last, time(), zone).astimezone(timezone.utc)
    return first, last, start, end


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Boundary(Input):
    local: str = Field(min_length=16, max_length=32)
    offset: str | None = Field(None, pattern=r"^(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)$")
    fold: StrictInt | None = Field(None, ge=0, le=1)


class Window(Input):
    start: Boundary
    end: Boundary


class Day(Input):
    date: str
    state: Literal["UNKNOWN", "UNAVAILABLE", "AVAILABLE"]
    max_sessions: StrictInt | None = Field(None, ge=0, le=10)
    doubles_allowed: StrictBool = False
    windows: list[Window] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def limits(self):
        calendar_day(self.date)
        if self.state == "AVAILABLE":
            if not self.windows or self.max_sessions is None or self.max_sessions < 1:
                raise ValueError("An available day needs a window and a positive daily session limit.")
            if (self.max_sessions > 1) != self.doubles_allowed:
                raise ValueError("A limit above one requires explicit doubles permission; keep these choices consistent.")
        elif self.windows or self.doubles_allowed or self.max_sessions != (0 if self.state == "UNAVAILABLE" else None):
            raise ValueError("Unavailable days have no windows and a zero limit; unknown days have no windows and an unknown limit.")
        return self


class Submission(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    expected_revision: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)
    days: list[Day] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def named_actor(self):
        if not self.actor.strip():
            raise ValueError("Name the person submitting this availability.")
        return self


def resolve(boundary):
    """Round-trip both folds: zoneinfo alone accepts nonexistent local clocks."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", boundary.local):
        raise ValueError("Use a local date/time without an offset; choose the UTC offset or clock occurrence separately.")
    local = datetime.fromisoformat(boundary.local)
    zone = ZoneInfo(ZONE)
    possible = {}
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        utc = candidate.astimezone(timezone.utc)
        returned = utc.astimezone(zone)
        if returned.replace(tzinfo=None) == local:
            possible[utc] = returned
    if not possible:
        raise ValueError(f"{boundary.local} does not exist in Europe/London because the clocks move forward.")
    offset = boundary.offset
    if offset is not None:
        if offset == "Z":
            offset = "+00:00"
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        seconds = int(offset[7:9]) if len(offset) == 9 else 0
        if hours > 23 or minutes > 59 or seconds > 59:
            raise ValueError("Use a valid UTC offset.")
        if len(offset) == 9 and not seconds:
            offset = offset[:6]
    matches = []
    for utc, local_time in possible.items():
        seconds = int(local_time.utcoffset().total_seconds())
        sign = "+" if seconds >= 0 else "-"
        hours, remainder = divmod(abs(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        resolved_offset = f"{sign}{hours:02}:{minutes:02}" + (f":{seconds:02}" if seconds else "")
        if offset is not None and offset != resolved_offset:
            continue
        if boundary.fold is not None and boundary.fold != local_time.fold:
            continue
        matches.append((utc, local_time, resolved_offset))
    if len(matches) != 1:
        if len(possible) > 1 and boundary.fold is None and boundary.offset is None:
            raise ValueError(f"{boundary.local} occurs twice in Europe/London; choose first/second occurrence or its UTC offset.")
        raise ValueError(f"The supplied offset/fold does not match {boundary.local} in Europe/London.")
    utc, local_time, resolved_offset = matches[0]
    return {"submitted": boundary.model_dump(), "local": local_time.replace(tzinfo=None).isoformat(),
            "utc": utc.isoformat(), "offset": resolved_offset, "fold": local_time.fold,
            "timezone": ZONE, "ambiguous": len(possible) > 1}


def union(windows):
    intervals = []
    for row in sorted(windows, key=lambda r: (r["start"]["utc"], r["end"]["utc"], r["id"])):
        start, end = row["start"]["utc"], row["end"]["utc"]
        if intervals and start <= intervals[-1]["end_utc"]:
            intervals[-1]["end_utc"] = max(end, intervals[-1]["end_utc"])
            intervals[-1]["source_window_ids"].append(row["id"])
        else:
            intervals.append({"start_utc": start, "end_utc": end, "source_window_ids": [row["id"]]})
    for row in intervals:
        row["elapsed_seconds"] = (datetime.fromisoformat(row["end_utc"]) - datetime.fromisoformat(row["start_utc"])).total_seconds()
    return intervals


def normalize(week, submission):
    first, last, week_start, week_end = week_bounds(week)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(7)]
    if sorted(d.date for d in submission.days) != dates:
        raise ValueError("Supply each of the seven dates in this Monday-to-Monday week exactly once.")
    rows, all_windows = [], []
    for day in sorted(submission.days, key=lambda d: d.date):
        windows = []
        for index, item in enumerate(day.windows):
            start, end = resolve(item.start), resolve(item.end)
            a, b = datetime.fromisoformat(start["utc"]), datetime.fromisoformat(end["utc"])
            if start["local"][:10] != day.date:
                raise ValueError("Each window must start on its declared local day.")
            if not week_start <= a < b <= week_end:
                raise ValueError("Windows must have a positive duration and stay inside the selected London week.")
            row = {"id": f"{day.date}:{index}", "start": start, "end": end,
                   "elapsed_seconds": (b - a).total_seconds()}
            windows.append(row)
            all_windows.append(row)
        rows.append({"date": day.date, "state": day.state, "max_sessions": day.max_sessions,
                     "doubles_allowed": day.doubles_allowed, "windows": windows})
    merged = union(all_windows)
    unknown = sum(row["state"] == "UNKNOWN" for row in rows)
    state = "UNKNOWN" if unknown == 7 else "PARTIAL" if unknown else "AVAILABLE" if merged else "EMPTY"
    return {"schema_version": "weekly-availability-v1", "policy_version": POLICY_VERSION,
            "week": week, "iso_week": f"{first.isocalendar().year}-W{first.isocalendar().week:02}",
            "timezone": ZONE, "week_start_utc": week_start.isoformat(), "week_end_utc": week_end.isoformat(),
            "interval_convention": "START_INCLUSIVE_END_EXCLUSIVE", "daily_limit_basis": "SESSION_START_LOCAL_DATE",
            "submission_state": "RECORDED", "state": state, "days": rows,
            "union_windows": merged, "union_elapsed_seconds": sum(r["elapsed_seconds"] for r in merged),
            "submitted": submission.model_dump(), "planner_action": "CALENDAR_REVISION_ONLY"}


def initialized(db):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='availability_revisions'").fetchone()


def read_version(db, week, version_id):
    """Immutable planner reader: no current-head or external-source resolution."""
    week_bounds(week)
    if not re.fullmatch(r"[a-f0-9]{64}", version_id):
        raise ValueError("Use a complete availability version ID.")
    row = initialized(db) and db.execute("SELECT payload FROM availability_revisions WHERE week=? AND id=?", (week, version_id)).fetchone()
    return json.loads(row["payload"]) if row else None


def read_current(db, week):
    week_bounds(week)
    row = initialized(db) and db.execute("SELECT payload FROM availability_revisions WHERE week=? ORDER BY revision DESC LIMIT 1", (week,)).fetchone()
    return json.loads(row["payload"]) if row else None


def read(store, week, version_id=None):
    first, _, start, end = week_bounds(week)
    with store.history() as db:
        result = read_version(db, week, version_id) if version_id else read_current(db, week)
        if version_id:
            return result
        versions = [dict(r) for r in db.execute("SELECT id,revision,previous_id,received_at,actor FROM availability_revisions WHERE week=? ORDER BY revision DESC", (week,))] if initialized(db) else []
    return {"week": week, "timezone": ZONE, "current": result, "versions": versions,
            "submission_state": "RECORDED" if result else "NOT_ENTERED", "state": result["state"] if result else "UNKNOWN",
            "week_start_utc": start.isoformat(), "week_end_utc": end.isoformat(),
            "dates": [(first + timedelta(days=i)).isoformat() for i in range(7)]}


def save(store, week, raw):
    submission = Submission.model_validate(raw)
    payload = normalize(week, submission)
    request_hash = digest({"week": week, "submitted": payload["submitted"]})
    with store.connect(write=True, create=True) as db:
        previous_request = db.execute("SELECT request_hash,payload FROM availability_revisions WHERE week=? AND request_id=?", (week, submission.request_id)).fetchone()
        if previous_request:
            if previous_request["request_hash"] != request_hash:
                raise Conflict("This request ID was already used with different content. Reload before editing.")
            return {"record": json.loads(previous_request["payload"]), "replayed": True}
        previous = read_current(db, week)
        if submission.expected_revision != (previous["version_id"] if previous else None):
            raise Conflict("Availability changed since this form was opened. Reload the current week before editing.")
        revision = previous["revision"] + 1 if previous else 1
        version_id = digest({"week": week, "revision": revision, "request_hash": request_hash})
        received_at = now()
        payload.update(version_id=version_id, revision=revision, previous_revision_id=submission.expected_revision,
                       received_at=received_at, known_at=received_at, actor=submission.actor, reason=submission.reason)
        db.execute("INSERT INTO availability_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                   (version_id, week, revision, submission.expected_revision, received_at, submission.actor,
                    submission.request_id, request_hash, encoded(payload)))
    return {"record": payload, "replayed": False}
