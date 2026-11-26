"""Provisional readiness interpretation of immutable G6 evidence, without plan actions."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

import context_data as context
import fitness
import fitness_inputs as fi
import fitness_recovery as recovery
import fm_snapshots as snapshots
import workload_inputs as wi

VERSION = "snapshot-readiness-v1"
DEFAULT_POLICY = "readiness-unconfigured-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS readiness_policies (
 version TEXT PRIMARY KEY, payload TEXT NOT NULL, known_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readiness_records (
 id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, policy_version TEXT NOT NULL,
 created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class SubjectiveRule(fi.Model):
    id: fi.Version
    kind: Literal["post_run", "preweek"]
    source: fi.Text
    field: Literal["fatigue", "soreness", "readiness", "rpe"]
    scale_version: fi.Version
    ordinary: fi.Range | None = None
    concern: fi.Range | None = None
    required_for_ordinary: bool = True

    @model_validator(mode="after")
    def ranges(self):
        if not self.ordinary and not self.concern:
            raise ValueError("Supply an explicit ordinary or concern range.")
        if self.ordinary and self.concern and max(self.ordinary.low, self.concern.low) <= min(self.ordinary.high, self.concern.high):
            raise ValueError("Ordinary and concern ranges must not overlap.")
        return self


class Stream(fi.Stream):
    origin: Literal["device", "self_report", "laboratory", "unknown"]
    interpretation: Literal["DEVICE_OUTPUT", "REPORTED_OBSERVATION"]
    value_type: Literal["decimal"] = "decimal"
    timezone: fi.Text
    policy_version: fi.Version

    @model_validator(mode="after")
    def zone(self):
        context.Dated.zone(self.timezone)
        return self


class WellnessRule(fi.Model):
    id: fi.Version
    stream: Stream
    direction: Literal["HIGHER", "LOWER", "BIDIRECTIONAL"]
    adverse_at_z: fi.Positive
    ordinary_abs_z_max: fi.Number | None = Field(None, ge=0)

    @model_validator(mode="after")
    def disjoint(self):
        if self.ordinary_abs_z_max is not None and self.ordinary_abs_z_max >= self.adverse_at_z:
            raise ValueError("The ordinary band must be strictly inside the adverse threshold.")
        return self


class Configuration(fi.Model):
    ordinary_basis: Literal["UNSPECIFIED", "SUBJECTIVE_WITH_LIMITATIONS", "SUBJECTIVE_AND_WELLNESS"] = "UNSPECIFIED"
    report_max_age_days: int | None = Field(None, ge=0, le=366, strict=True)
    post_run_lookback_days: int | None = Field(None, ge=1, le=366, strict=True)
    include_preweek_for_run: bool = False
    wellness_max_age_days: int | None = Field(None, ge=0, le=366, strict=True)
    consecutive_report_count: int = Field(2, ge=2, le=30, strict=True)
    consecutive_wellness_days: int | None = Field(None, ge=2, le=30, strict=True)
    subjective_rules: list[SubjectiveRule] = Field(default_factory=list, max_length=40)
    wellness_rules: list[WellnessRule] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def unique_rules(self):
        rules = self.subjective_rules + self.wellness_rules
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("Rule IDs must be unique.")
        keys = [(r.kind, r.source, r.field, r.scale_version) for r in self.subjective_rules]
        streams = [wi.encoded(r.stream.model_dump(mode="json")) for r in self.wellness_rules]
        if len(set(keys)) != len(keys) or len(set(streams)) != len(streams):
            raise ValueError("Use one rule per compatible rating or wellness stream.")
        return self


class Definition(fi.Model):
    version: fi.Version
    actor: fi.Text
    reason: fi.Note
    config: Configuration


class Scope(fi.Model):
    week: str | None = None
    activity_id: int | None = Field(None, strict=True, gt=0, le=context.MAX_SQLITE_INTEGER)

    @model_validator(mode="after")
    def target(self):
        if (self.week is None) == (self.activity_id is None):
            raise ValueError("Choose exactly one Monday target week or completed activity.")
        if self.week is not None and date.fromisoformat(context.day(self.week)).weekday() != 0:
            raise ValueError("The target week must start on Monday.")
        return self


class Request(fi.Model):
    snapshot_id: fi.Hash
    policy_version: fi.Version = DEFAULT_POLICY
    scope: Scope


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def policy(db, version):
    if version == DEFAULT_POLICY:
        return {"version": version, "actor": "Master v1.0", "reason": "No personal thresholds or freshness assumptions supplied. Two consecutive post-run checks is a provisional example only.",
                "config": Configuration().model_dump(mode="json"), "scale_definitions": {}, "known_at": None, "raw_sha256": None}
    row = db.execute("SELECT * FROM readiness_policies WHERE version=?", (version,)).fetchone() if wi.table(db, "readiness_policies") else None
    if not row:
        raise ValueError("Readiness policy not found. Register the explicit version first.")
    return {**json.loads(row["payload"]), "known_at": row["known_at"], "raw_sha256": row["raw_sha256"]}


def register(store, raw):
    definition = Definition.model_validate(fi.parse(raw))
    value = definition.model_dump(mode="json")
    if definition.version == DEFAULT_POLICY:
        raise wi.Conflict("The unconfigured default is immutable. Choose a new policy version.")
    with store.connect(write=True, create=True) as db:
        scales = {}
        for rule in definition.config.subjective_rules:
            scale = context.definition(db, "scale", rule.scale_version)
            for band in (rule.ordinary, rule.concern):
                if band and (band.low < Decimal(scale["minimum"]) or band.high > Decimal(scale["maximum"])):
                    raise ValueError("Rating ranges must lie within the named immutable scale.")
            scales[rule.scale_version] = scale
        value["scale_definitions"] = scales
        prior = db.execute("SELECT payload FROM readiness_policies WHERE version=?", (definition.version,)).fetchone()
        if prior and prior[0] != wi.encoded(value):
            raise wi.Conflict("That policy version already exists. Save a new version.")
        if not prior:
            sha = store.archive.put(raw)
            db.execute("INSERT INTO readiness_policies VALUES (?,?,?,?)", (definition.version, wi.encoded(value), wi.now(), sha))
        return policy(db, definition.version)


def policies(store):
    with store.history() as db:
        versions = [r[0] for r in db.execute("SELECT version FROM readiness_policies ORDER BY known_at,version")] if wi.table(db, "readiness_policies") else []
        return [policy(db, v) for v in [DEFAULT_POLICY, *versions]]


def inside(value, band):
    return band is not None and Decimal(band["low"]) <= Decimal(value) <= Decimal(band["high"])


def scope_data(snapshot, scope, cfg):
    actuals = snapshot["planner"]["actuals"]
    by_member = {member: row for row in actuals for member in row["identity"]["member_ids"]}
    target = by_member.get(scope.activity_id) if scope.activity_id else None
    if scope.activity_id and target is None:
        raise ValueError("The target must be a completed run present in this frozen snapshot.")
    week = scope.week
    if target:
        day = wi.stamp(target["start_utc"]).astimezone(ZoneInfo("Europe/London")).date()
        week = (day - timedelta(days=day.weekday())).isoformat()
    end = datetime.combine(date.fromisoformat(week) + timedelta(days=7), time(), ZoneInfo("Europe/London"))
    anchor = min(wi.stamp(snapshot["event_cutoff"]), wi.stamp(target["end_utc"]) if target else end)
    recent = []
    for row in actuals:
        run_end = wi.stamp(row["end_utc"])
        if cfg["post_run_lookback_days"] is not None and anchor - timedelta(days=cfg["post_run_lookback_days"]) < run_end <= anchor:
            recent.append(row)
        elif target and row["activity_id"] == target["activity_id"]:
            recent.append(row)
    return {"week": week, "activity_id": scope.activity_id, "resolved_activity_id": target["activity_id"] if target else None,
            "timezone": "Europe/London", "reference_time": snapshot["event_cutoff"], "run_window_end": wi.utc(anchor.isoformat()),
            "report_rule": "Exact preweek target; post-run target membership and optional dated completed-run lookback. No current redirects are consulted."}, by_member, sorted(recent, key=lambda r:(r["end_utc"],r["activity_id"]))


def feedback_evidence(snapshot, scope, cfg, definition, scope_info, by_member, runs):
    rows = []
    recent_ids = {r["activity_id"] for r in runs}
    for record in snapshot["fitness"]["recovery"]["feedback"]:
        reasons, signals = [], []
        owner = (by_member.get(record.get("activity_id")) or {}).get("activity_id")
        if record["kind"] == "preweek":
            if record["week"] != scope_info["week"] or (scope.activity_id and not cfg["include_preweek_for_run"]):
                reasons.append("DIFFERENT_TARGET")
        elif owner is None or owner not in recent_ids:
            reasons.append("OUTSIDE_COMPLETED_RUN_SCOPE")
        age = (wi.stamp(snapshot["event_cutoff"]) - wi.stamp(record["reported_at"])).total_seconds()
        if age < 0 or wi.utc(record["known_at"]) > snapshot["knowledge_cutoff"]:
            reasons.append("AFTER_SNAPSHOT_CUTOFF")
        if cfg["report_max_age_days"] is not None and age > cfg["report_max_age_days"] * 86400:
            reasons.append("STALE_REPORT")
        applicable = not reasons
        state = "EXCLUDED" if reasons else "UNKNOWN"
        if applicable:
            if record["attention"]:
                state = "PAIN_OR_ILLNESS_REVIEW"
                signals = [{"kind": flag, "basis": "EXPLICIT_RUNNER_FLAG"} for flag in record["attention"]]
            elif record.get("missing_reason"):
                reasons.append("EXPLICIT_MISSING_REPORT")
            elif cfg["report_max_age_days"] is None:
                reasons.append("REPORT_FRESHNESS_UNCONFIGURED")
            else:
                rules = [r for r in cfg["subjective_rules"] if r["kind"] == record["kind"] and r["source"] == record["source"]]
                matched_fields = set()
                for rule in rules:
                    rating = record["ratings"].get(rule["field"])
                    if not rating:
                        if rule["required_for_ordinary"]:
                            reasons.append("MISSING_REQUIRED_RATING:" + rule["id"])
                        continue
                    if rating["scale_version"] != rule["scale_version"] or rating["scale_definition"] != definition["scale_definitions"][rule["scale_version"]]:
                        continue
                    matched_fields.add(rule["field"])
                    level = "CONCERN" if inside(rating["value"], rule["concern"]) else "ORDINARY" if inside(rating["value"], rule["ordinary"]) else "UNKNOWN"
                    signals.append({"rule_id": rule["id"], "field": rule["field"], "value": rating["value"], "scale_version": rating["scale_version"], "level": level})
                    if level == "UNKNOWN":
                        reasons.append("OUTSIDE_DEFINED_RANGES:" + rule["id"])
                for field in sorted(record["ratings"].keys() - matched_fields):
                    reasons.append("UNCONFIGURED_OR_INCOMPATIBLE_RATING:" + field)
                if any((record.get(k) or "").strip() for k in ("pain_discomfort", "pain_location")):
                    reasons.append("UNCLASSIFIED_SYMPTOM_TEXT")
                if any(s.get("level") == "CONCERN" for s in signals):
                    state = "CONCERN"
                elif any(s.get("level") == "ORDINARY" for s in signals) and not reasons:
                    state = "ORDINARY"
                elif not reasons:
                    reasons.append("NO_EXPLICIT_RATING_RULE")
        rows.append({"record": record, "resolved_activity_id": owner, "applicable": applicable,
                     "age_seconds": str(age), "state": state, "reasons": reasons, "signals": signals})
    return rows


def wellness_evidence(snapshot, bundle, cfg):
    evidence = []
    metrics = snapshot["fitness"]["recovery"]["wellness"]
    all_streams = [s for metric in metrics.values() for s in metric["streams"]]
    for rule in cfg["wellness_rules"]:
        found = next((s for s in all_streams if s["stream"] == rule["stream"]), None)
        result = {"rule": rule, "state": "UNKNOWN", "reasons": [], "days": [], "persistence": False}
        evidence.append(result)
        if not found:
            result["reasons"].append("COMPATIBLE_STREAM_MISSING")
            continue
        result["frozen_g5_stream"] = found
        if cfg["wellness_max_age_days"] is None:
            result["reasons"].append("WELLNESS_FRESHNESS_UNCONFIGURED")
            continue
        latest = found["latest_observed_local_date"]
        today = date.fromisoformat(found["today_local_date"])
        if latest is None or (today - date.fromisoformat(latest)).days > cfg["wellness_max_age_days"]:
            result["reasons"].append("CURRENT_MISSING_OR_STALE")
            continue
        rows = [r for r in bundle["g5"]["context_records"] if r["kind"] == "wellness" and {k:r.get(k) for k in recovery.STREAM_FIELDS} == rule["stream"]]
        number = cfg["consecutive_wellness_days"] or 1
        with localcontext() as precision:
            precision.prec = 400
            for offset in reversed(range(number)):
                day = date.fromisoformat(latest) - timedelta(days=offset)
                if day == today:
                    descriptor = found
                else:
                    # This anchor selects a calendar day, never an observation instant.
                    anchor = datetime.combine(day, time(), ZoneInfo(rule["stream"]["timezone"])).isoformat()
                    descriptor = recovery.wellness_stream(rows, anchor, bundle["g5"]["definition"]["config"])
                robust = descriptor["robust_deviation"]
                level, reason = "UNKNOWN", robust["status"]
                if robust["status"] == "DESCRIPTIVE" and robust["rule"]["direction"] == rule["direction"]:
                    z = Decimal(robust["value"])
                    adverse = z if rule["direction"] == "HIGHER" else -z if rule["direction"] == "LOWER" else abs(z)
                    level = "CONCERN" if adverse >= Decimal(rule["adverse_at_z"]) else "ORDINARY" if rule["ordinary_abs_z_max"] is not None and abs(z) <= Decimal(rule["ordinary_abs_z_max"]) else "UNKNOWN"
                    reason = "EXPLICIT_PERSONAL_RANGE" if level != "UNKNOWN" else "OUTSIDE_DEFINED_RANGES"
                elif robust["rule"] and robust["rule"]["direction"] != rule["direction"]:
                    reason = "INCOMPATIBLE_G5_DIRECTION"
                result["days"].append({"local_date": day.isoformat(), "level": level, "reason": reason, "g5_descriptor": descriptor})
        result["state"] = result["days"][-1]["level"]
        result["persistence"] = number >= 2 and all(d["level"] == "CONCERN" for d in result["days"])
        result["day_interpretation"] = "Descriptive daily recomputation from the same frozen G5 rows and configuration at K, not prospective historical assessments."
    return evidence


def calculate(snapshot, bundle, definition, scope):
    cfg = definition["config"]
    scope_info, by_member, runs = scope_data(snapshot, scope, cfg)
    reports = feedback_evidence(snapshot, scope, cfg, definition, scope_info, by_member, runs)
    wellness = wellness_evidence(snapshot, bundle, cfg)
    applicable = [r for r in reports if r["applicable"]]
    persistent = []
    n = cfg["consecutive_report_count"]
    for rule in cfg["subjective_rules"]:
        if rule["kind"] != "post_run" or rule["field"] not in {"fatigue", "soreness"} or len(runs) < n:
            continue
        chosen = []
        for run in runs[-n:]:
            matches = [r for r in applicable if r["resolved_activity_id"] == run["activity_id"] and r["record"]["source"] == rule["source"]]
            if len(matches) != 1 or not any(s.get("rule_id") == rule["id"] and s["level"] == "CONCERN" for s in matches[0]["signals"]):
                break
            chosen.append(matches[0]["record"]["version_id"])
        if len(chosen) == n:
            persistent.append({"basis": "CONSECUTIVE_COMPLETED_RUN_REPORTS", "rule_id": rule["id"], "count": n, "version_ids": chosen})
    persistent.extend({"basis": "CONSECUTIVE_COMPATIBLE_WELLNESS_DAYS", "rule_id": r["rule"]["id"], "days": [d["local_date"] for d in r["days"]]} for r in wellness if r["persistence"])
    concern = any(r["state"] == "CONCERN" for r in applicable + wellness)
    ordinary = any(r["state"] == "ORDINARY" for r in applicable)
    unclear = any(r["state"] == "UNKNOWN" for r in applicable)
    both = ordinary and any(r["state"] == "ORDINARY" for r in wellness) and all(r["state"] == "ORDINARY" for r in wellness)
    if any(r["state"] == "PAIN_OR_ILLNESS_REVIEW" for r in applicable):
        state, reason = "PAIN_OR_ILLNESS_REVIEW", "An applicable explicit runner pain, illness or worsening-symptom flag takes priority."
    elif persistent:
        state, reason = "PERSISTENT_CONCERN", "Repeated compatible concerns meet this explicit provisional persistence policy."
    elif concern:
        state, reason = "MIXED", "An applicable signal is concerning or supplied subjective/wellness evidence disagrees."
    elif ordinary and not unclear and (cfg["ordinary_basis"] == "SUBJECTIVE_WITH_LIMITATIONS" or cfg["ordinary_basis"] == "SUBJECTIVE_AND_WELLNESS" and both):
        state, reason = "ORDINARY", "The configured subjective ordinary rule is met; no applicable interpreted concern is present."
    else:
        state, reason = "UNKNOWN", "Adequate applicable ordinary or concerning evidence is unavailable under this policy."
    missing = [{"kind": "feedback", "reason": "No applicable runner check-in."}] if not applicable else []
    missing += [{"kind": "wellness", "metric": key, "reason": value["reason"] or "No current supplied observation."} for key,value in snapshot["fitness"]["recovery"]["wellness"].items() if value["state"] == "MISSING"]
    return {"algorithm_version": VERSION, "code_sha256": code_hash(), "snapshot_id": snapshot["id"], "snapshot_input_hash": snapshot["input_hash"],
            "event_cutoff": snapshot["event_cutoff"], "knowledge_cutoff": snapshot["knowledge_cutoff"], "snapshot_kind": snapshot["kind"],
            "scope": scope_info, "state": state, "reason": reason, "policy": definition,
            "policy_time_basis": "LATER_POLICY_REANALYSIS" if definition["known_at"] and wi.utc(definition["known_at"]) > snapshot["knowledge_cutoff"] else "POLICY_KNOWN_BY_SNAPSHOT_K",
            "runner_reports": reports, "wellness_evidence": wellness, "persistence": persistent,
            "missingness": missing, "unlinked_feedback": snapshot["fitness"]["recovery"]["unlinked_feedback"],
            "unconfigured_wellness_streams": [s for metric in snapshot["fitness"]["recovery"]["wellness"].values() for s in metric["streams"] if not any(r["stream"] == s["stream"] for r in cfg["wellness_rules"])],
            "uncertain_context": bundle["uncertain_context"], "review_required": state != "ORDINARY",
            "limitations": ["Provisional runner-relative interpretation; not a medical clearance or injury probability.",
                            "ORDINARY is not approval to increase distance. Missing observations are never positive readiness.",
                            "No target, schedule, distance, athlete approval or clinical action is changed."]}


def evaluate_in_db(db, request):
    row = snapshots.get_row(db, request.snapshot_id)
    frozen = {**snapshots.header(row), **fitness.unpack(row["result"])}
    return calculate(frozen, fitness.unpack(row["inputs"]), policy(db, request.policy_version), request.scope)


def evaluate(store, choice):
    request = Request.model_validate(choice)
    with store.history() as db:
        return evaluate_in_db(db, request)


def save(store, choice):
    request = Request.model_validate(choice)
    with store.connect(write=True, create=True) as db:
        value = evaluate_in_db(db, request)
        identity = wi.digest(value)
        db.execute("INSERT OR IGNORE INTO readiness_records VALUES (?,?,?,?,?)", (identity, request.snapshot_id, request.policy_version, wi.now(), wi.encoded(value)))
        row = db.execute("SELECT * FROM readiness_records WHERE id=?", (identity,)).fetchone()
        return {"id": identity, "created_at": row["created_at"], "result": json.loads(row["payload"])}


def saved(store, identity):
    with store.history() as db:
        row = db.execute("SELECT * FROM readiness_records WHERE id=?", (identity,)).fetchone() if wi.table(db, "readiness_records") else None
    if not row:
        raise KeyError("Readiness record not found.")
    return {"id": row["id"], "created_at": row["created_at"], "result": json.loads(row["payload"])}
