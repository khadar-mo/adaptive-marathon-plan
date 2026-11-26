"""Descriptive recovery evidence from immutable context revisions."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, localcontext
import json
from statistics import median
from zoneinfo import ZoneInfo

import context_data as context
import workload_inputs as wi


STREAM_FIELDS = ("metric", "unit", "source", "method", "calibration", "origin",
                 "interpretation", "value_type", "timezone", "policy_version")


def records_at(db, event, knowledge):
    """Select the revision known at K, then apply its own measurement/report clock at T."""
    if not context.initialized(db):
        return []
    rows = db.execute("""SELECT v.* FROM context_versions v JOIN (
        SELECT record_id,max(revision) AS revision FROM context_versions WHERE known_at<=? GROUP BY record_id
        ) s ON s.record_id=v.record_id AND s.revision=v.revision ORDER BY v.record_id""", (knowledge,))
    result = []
    for row in rows:
        value = json.loads(row["payload"])
        if not context.eligible(value, event):
            continue
        sources = [dict(ref) for ref in db.execute("""SELECT s.import_id,s.item_index,i.raw_sha256,i.parser_version,i.known_at
            FROM context_sources s JOIN context_imports i ON i.id=s.import_id
            WHERE s.version_id=? AND i.known_at<=? ORDER BY i.known_at,s.import_id,s.item_index""", (row["id"], knowledge))]
        result.append({**value, "record_id": row["record_id"], "version_id": row["id"],
                       "known_at": row["known_at"], "revision": row["revision"], "previous_id": row["previous_id"],
                       "correction_reason": row["correction_reason"], "sources": sources})
    return result


def wellness_stream(rows, event, config):
    stream = {k: rows[0].get(k) for k in STREAM_FIELDS}
    today = wi.stamp(event).astimezone(ZoneInfo(stream["timezone"])).date()
    start = today - timedelta(days=config["baseline_days"])
    daily = context.daily_groups(rows, rows[0]["policy"])
    days = [r for r in daily if start.isoformat() <= r["local_date"] <= today.isoformat()]
    previous = [r for r in days if r["local_date"] < today.isoformat() and r["value"] is not None]
    current = next((r for r in days if r["local_date"] == today.isoformat()), None)
    numeric = stream["value_type"] == "decimal"
    values = [Decimal(r["value"]) for r in previous] if numeric else []
    base = median(values) if len(values) >= config["minimum_days"] else None
    now = current["value"] if current else None
    delta = Decimal(now) - base if base is not None and now is not None else None
    matcher = {k: stream[k] for k in ("metric", "unit", "source", "method", "calibration")}
    rule = next((rule for rule in config["robust_deviations"] if rule["stream"] == matcher), None)
    robust = {"status": "UNCONFIGURED", "value": None, "rule": rule, "mad": None}
    if rule:
        robust["status"] = "BASELINE_UNKNOWN" if base is None else "CURRENT_MISSING"
        if base is not None:
            mad = median([abs(v - base) for v in values])
            robust["mad"] = str(mad)
            if delta is not None:
                robust.update(status="DESCRIPTIVE", value=str(delta / max(Decimal("1.4826") * mad, Decimal(rule["epsilon"]))))
    return {"stream": stream, "baseline_status": "NON_NUMERIC" if not numeric else "BASELINE_UNKNOWN" if base is None else "AVAILABLE",
            "baseline": str(base) if base is not None else None, "current_status": current["state"] if current else "MISSING",
            "current": now, "delta": str(delta) if delta is not None else None, "unit": stream["unit"],
            "observed_days": len(previous), "expected_days": config["baseline_days"], "minimum_days": config["minimum_days"],
            "missing_days": config["baseline_days"] - len(previous), "first_local_date": start.isoformat(),
            "today_local_date": today.isoformat(), "latest_observed_local_date": max((r["local_date"] for r in days if r["value"] is not None), default=None),
            "baseline_reason": None if base is not None else "Categorical device output." if not numeric else "Insufficient compatible valid daily values.",
            "current_reason": None if now is not None else (current["missing_reasons"] or [current["state"]]) if current else ["No supplied observation on the target local date."],
            "daily_evidence": days, "robust_deviation": robust}


def calculate(records, event, config):
    groups = defaultdict(list)
    for row in records:
        if row["kind"] == "wellness":
            groups[tuple(row.get(k) for k in STREAM_FIELDS)].append(row)
    with localcontext() as ctx:
        ctx.prec = 400  # Context decimal values retain as many as 200 digits.
        streams = [wellness_stream(group, event, config) for group in groups.values()]
    expected = set(config["expected_metrics"]) | {r["stream"]["metric"] for r in streams}
    metrics = {}
    for metric in sorted(expected):
        relevant = [r for r in streams if r["stream"]["metric"] == metric]
        metrics[metric] = {"streams": relevant, "state": "OBSERVED" if any(r["current"] is not None for r in relevant) else "MISSING",
                           "reason": None if relevant else "No compatible stream supplied."}
    feedback = [r for r in records if r["kind"] in ("post_run", "preweek")]
    signals = [{"kind": flag, "version_id": r["version_id"], "reported_at": r["reported_at"],
                "known_at": r["known_at"], "target": r.get("activity_id") or r.get("week"),
                "pain_location": r["pain_location"], "pain_discomfort": r["pain_discomfort"], "context": r["context"],
                "action": "PLANNER_REVIEW_ONLY"} for r in feedback for flag in r["attention"]]
    return {"wellness": metrics, "feedback": feedback, "feedback_status": "OBSERVED" if any(not r["missing_reason"] for r in feedback) else "MISSING",
            "attention_signals": signals, "strength": [r for r in records if r["kind"] == "strength"],
            "coverage_reports": [r for r in records if r["kind"] == "coverage"],
            "interpretation": "Device estimates and subjective reports retain their sources and scales. Strength is a separate exposure stream. Missing data have no recovery interpretation."}
