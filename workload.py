"""Whole-session training exposure with explicit clocks and frozen evidence."""
import argparse
from datetime import datetime, time, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import workload_inputs as inputs


VERSION = "g3-exposure-v1"
ZONE = ZoneInfo("Europe/London")
MEASURES = ("distance_m", "elapsed_seconds", "timer_seconds", "moving_seconds")
CONFIG = {"version": VERSION, "rolling_days": [7, 28, 56], "longest_days": [30, 56, 112],
          "calendar_weeks": 17, "timezone": "Europe/London", "rolling_bounds": "(T-W,T]",
          "weekly_fm_basis": "END", "planner_week_basis": "START",
          "timing": "accepted summary pair; missing pair may use selected FIT own span; invalid pair unassigned",
          "l30": "END local date in [proposed START local date - 30 days, proposed date), end <= event cutoff",
          "precision": "Decimal from source values; decimal strings in JSON; no planner rounding",
          "intensity": "supplied exact profile and band ledger; no inferred physiological thresholds"}


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes() + Path(inputs.__file__).read_bytes()).hexdigest()


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (ValueError, ArithmeticError):
        return None


def decimal(value):
    return str(value) if value is not None else None


def pair(value):
    if not value.get("start_utc") or not value.get("end_utc"):
        return None, "MISSING_TIMING"
    try:
        start, end = inputs.stamp(value["start_utc"]), inputs.stamp(value["end_utc"])
    except ValueError:
        return None, "INVALID_TIMING"
    if end < start:
        return None, "INVALID_TIMING"
    return {"start_utc": inputs.utc(value["start_utc"]), "end_utc": inputs.utc(value["end_utc"]),
            "end_basis": value.get("end_basis", "NOT_PROVIDED")}, None


def activity(row, selection):
    payload = json.loads(row["payload"])
    summary, header = payload["summary"], payload.get("header") or {}
    timing, issue = pair(summary)
    fit, fit_issue = pair(header)
    if header.get("end_basis") != "start_time_plus_total_elapsed_time":
        fit, fit_issue = None, "NO_VERIFIED_OWN_FIT_SPAN"
    basis = "CANONICAL_ACCEPTED_SUMMARY"
    if not timing and issue == "MISSING_TIMING" and fit:
        timing, issue, basis = fit.copy(), None, "SELECTED_FIT_OWN_SPAN_FALLBACK"
    measures = {key: decimal(number(summary.get(key))) for key in MEASURES}
    return {"activity_id": payload["activity_id_at_creation"], "canonical_version_id": row["id"],
            "canonical_created_at": row["created_at"], "selection": selection,
            "identity": {"owner_at_selection": payload["activity_id_at_creation"],
                         "merged_activity_ids": payload.get("merged_activity_ids", []),
                         "provider_ids": payload.get("provider_ids", []),
                         "known_at": selection["selected_at"], "basis": "IMMUTABLE_CANONICAL_SELECTION_PAYLOAD"},
            "running_sport": payload["running_sport"], "measures": measures,
            "missing_measures": [key for key, value in measures.items() if value is None],
            "timing": timing, "timing_basis": basis if timing else "UNASSIGNED", "timing_issue": issue,
            "accepted_summary": summary, "selected_fit_span": fit,
            "fit_span_issue": fit_issue, "fit_message_written_utc": header.get("message_timestamp_utc"),
            "timing_disagreement": bool(timing and fit and (timing["start_utc"], timing["end_utc"]) != (fit["start_utc"], fit["end_utc"])),
            "selected_source_version_id": payload.get("selected_source_version_id"),
            "selected_fit_sha256": payload.get("selected_fit_sha256"), "raw_sources": payload.get("raw_sources", []),
            "qc_config_version": payload["qc_config_version"], "summary_comparisons": payload.get("summary_comparisons", []),
            "canonical_payload": payload}


def collect(db, cutoff, knowledge, mode, proposal):
    """Read one immutable selection timeline, never current aliases or mutable heads."""
    choices, excluded = {}, []
    if inputs.table(db, "canonical_selections"):
        for row in db.execute("SELECT * FROM canonical_selections ORDER BY id"):
            if inputs.stamp(row["selected_at"]) <= inputs.stamp(knowledge):
                key = row["activity_id"]
                if key not in choices or (inputs.stamp(row["selected_at"]), row["id"]) > (inputs.stamp(choices[key]["selected_at"]), choices[key]["id"]):
                    choices[key] = dict(row)
    selected = []
    for aid, selection in sorted(choices.items()):
        row = db.execute("SELECT * FROM canonical_activity_versions WHERE id=?", (selection["version_id"],)).fetchone()
        if not row or inputs.stamp(row["created_at"]) > inputs.stamp(knowledge):
            excluded.append({"activity_id": aid, "reason": "VERSION_KNOWLEDGE_UNESTABLISHED"})
            continue
        item = activity(row, selection)
        config = db.execute("SELECT payload FROM qc_configs WHERE version=?", (item["qc_config_version"],)).fetchone()
        item["qc_config"] = json.loads(config[0]) if config else None
        selected.append(item)
    # Identity links are those explicitly retained in the selected payloads at K.
    # A later current redirect cannot merge two historical groups retroactively.
    merged = {old for item in selected for old in item["identity"]["merged_activity_ids"]}
    rows = []
    for item in selected:
        reason = "MERGED_CANONICAL_COPY" if item["activity_id"] in merged else "NONRUNNING" if not item["running_sport"] else None
        if reason:
            excluded.append({"activity_id": item["activity_id"], "canonical_version_id": item["canonical_version_id"], "reason": reason})
        else:
            rows.append(item)
    records = inputs.selected(db, knowledge)
    # A ledger keeps its exact profile version even after a profile correction.
    profile_ids = {r["profile_version_id"] for r in records if r["kind"] == "band_ledger"}
    profiles = []
    for pid in sorted(profile_ids):
        row = db.execute("SELECT * FROM workload_input_versions WHERE id=?", (pid,)).fetchone()
        if row and inputs.stamp(row["known_at"]) <= inputs.stamp(knowledge):
            profiles.append(inputs.decorate(row))
    return {"algorithm_version": VERSION, "code_sha256": code_hash(), "config": CONFIG,
            "event_cutoff": cutoff, "knowledge_cutoff": knowledge, "time_basis": mode,
            "proposal": proposal, "activities": rows, "selection_exclusions": excluded,
            "records": records, "referenced_profiles": profiles,
            "provider_lineage": provider_lineage(db, knowledge),
            "identity_policy": "Use only identity groups captured in immutable selected canonical payloads; no inferred earlier identity knowledge.",
            "legacy_policy": "Unknown knowledge excluded. Canonical creation/selection proves availability no earlier than that recorded event."}


def provider_lineage(db, knowledge):
    configs, attempts = [], []
    if inputs.table(db, "provider_config_revisions"):
        configs = [dict(r) for r in db.execute("SELECT * FROM provider_config_revisions ORDER BY revision")
                   if inputs.stamp(r["selected_at"]) <= inputs.stamp(knowledge)]
    if inputs.table(db, "provider_attempts"):
        for r in db.execute("SELECT * FROM provider_attempts ORDER BY id"):
            if inputs.stamp(r["started_at"]) > inputs.stamp(knowledge):
                continue
            row = dict(r)
            if row["finished_at"] and inputs.stamp(row["finished_at"]) <= inputs.stamp(knowledge) and row["result"]:
                row["result"] = json.loads(row["result"])
            else:
                row.update(finished_at=None, result=None, knowledge_state="NO_TERMINAL_RESULT_AT_CUTOFF")
            attempts.append(row)
    return {"selected_config": configs[-1] if configs else None, "config_revisions": configs, "attempts": attempts,
            "missing_reason": None if configs else "No timestamped provider mode selection available; no historical default inferred.",
            "policy": "Modes affect future imports, not retained actuals. Only persisted terminal capability/fallback results are evidence."}


def current_diagnostics(db):
    """Operational guidance only, excluded from every frozen/hashable calculation."""
    if not inputs.table(db, "activities"):
        return None
    from garmin_activity import RUN_TYPES
    known = {r[0] for r in db.execute("SELECT activity_id FROM canonical_selections")} if inputs.table(db, "canonical_selections") else set()
    legacy = [r["id"] for r in db.execute("SELECT id,sport FROM activities ORDER BY id") if r["sport"] in RUN_TYPES and r["id"] not in known]
    if legacy:
        return {"state": "INCOMPLETE_PROCESSING", "unprocessed_running_ids": legacy,
                "time_basis": "CURRENT_DIAGNOSTIC_NOT_HISTORICAL_EVIDENCE",
                "guidance": "Explicitly recompute these legacy canonical activities before using present workload as complete. Unknown original knowledge is not backdated. This diagnostic is outside the input hash."}
    return None


def interval(lower, upper, lower_inclusive=True, upper_inclusive=False):
    return (inputs.stamp(lower), inputs.stamp(upper), lower_inclusive, upper_inclusive)


def valid_span(span):
    return span[0] < span[1] or (span[0] == span[1] and span[2] and span[3])


def intersect(a, b):
    lower, upper = max(a[0], b[0]), min(a[1], b[1])
    left = (a[2] if a[0] == lower else True) and (b[2] if b[0] == lower else True)
    right = (a[3] if a[1] == upper else True) and (b[3] if b[1] == upper else True)
    result = (lower, upper, left, right)
    return result if valid_span(result) else None


def subtract(spans, cut):
    result = []
    for span in spans:
        overlap = intersect(span, cut)
        if overlap is None:
            result.append(span)
            continue
        result.extend(s for s in ((span[0], overlap[0], span[2], not overlap[2]),
                                  (overlap[1], span[1], not overlap[3], span[3])) if valid_span(s))
    return result


def bounds(span):
    return {"lower_utc": span[0].isoformat(timespec="microseconds"), "upper_utc": span[1].isoformat(timespec="microseconds"),
            "lower_inclusive": span[2], "upper_inclusive": span[3]}


def inside(t, span):
    return (t > span[0] or t == span[0] and span[2]) and (t < span[1] or t == span[1] and span[3])


def coverage(bundle, span, basis):
    gaps, evidence, blockers = [span], [], []
    for row in bundle["records"]:
        if row["kind"] != "coverage" or row["basis"] != basis:
            continue
        stated = interval(row["lower_utc"], row["upper_utc"], row["lower_inclusive"], row["upper_inclusive"])
        hit = intersect(span, stated)
        if hit is None:
            continue
        evidence.append(row)
        if row["status"] == "COMPLETE" and not row["exclusions"]:
            observed = min(inputs.stamp(row["observed_through"]), inputs.stamp(bundle["event_cutoff"]))
            available = intersect(stated, (stated[0], observed, stated[2], True))
            if available:
                gaps = subtract(gaps, available)
        else:
            blockers.append(hit)
    # Conflicting or unresolved census evidence is visible, never silently overridden.
    complete = not gaps and not blockers
    return {"status": "COMPLETE" if complete else "INCOMPLETE_HISTORY", "complete": complete,
            "basis": basis, **bounds(span), "evidence": evidence,
            "gaps": [bounds(s) for s in gaps], "unresolved_intervals": [bounds(s) for s in blockers]}


def summarize(rows, census, unassigned):
    result = {"run_count": len(rows), "activity_ids": [r["activity_id"] for r in rows],
              "canonical_version_ids": [r["canonical_version_id"] for r in rows],
              "coverage": census, "unassigned_timing_count": unassigned, "measures": {}}
    for key in MEASURES:
        values = [number(r["measures"][key]) for r in rows if r["measures"][key] is not None]
        missing = len(rows) - len(values)
        subtotal = sum(values, Decimal(0))
        complete = census["complete"] and not missing and not unassigned
        result["measures"][key] = {"known_subtotal": str(subtotal), "missing_count": missing,
                                    "value": str(subtotal) if complete else None,
                                    "status": "COMPLETE" if complete else "MISSING_MEASUREMENT" if missing else "INCOMPLETE_HISTORY"}
    known = [r for r in rows if r["measures"]["distance_m"] is not None]
    longest = max((number(r["measures"]["distance_m"]) for r in known), default=None)
    complete = census["complete"] and len(known) == len(rows) and not unassigned
    result["longest"] = {"observed_distance_m": decimal(longest), "distance_m": decimal(longest) if complete else None,
                         "activity_ids": [r["activity_id"] for r in known if number(r["measures"]["distance_m"]) == longest],
                         "status": "COMPLETE" if complete else "INCOMPLETE_HISTORY"}
    result["frequency_status"] = "COMPLETE" if census["complete"] and not unassigned else "INCOMPLETE_HISTORY"
    return result


def intensity(bundle, rows):
    versions = {row["canonical_version_id"]: row for row in rows}
    profiles = {r["version_id"]: r for r in [*bundle["records"], *bundle["referenced_profiles"]] if r["kind"] == "zone_profile"}
    ledgers = [r for r in bundle["records"] if r["kind"] == "band_ledger"]
    groups, used, excluded = {}, set(), []
    for ledger in ledgers:
        row = versions.get(ledger["canonical_version_id"])
        if row is None:
            excluded.append({"version_id": ledger["version_id"], "reason": "CANONICAL_VERSION_NOT_IN_WINDOW"})
            continue
        profile = profiles.get(ledger["profile_version_id"])
        if not profile:
            excluded.append({"version_id": ledger["version_id"], "reason": "PROFILE_NOT_KNOWN"})
            continue
        key = (profile["version_id"], ledger["duration_basis"])
        group = groups.setdefault(key, {"profile": profile, "duration_basis": ledger["duration_basis"], "activities": [],
                                       "seconds": {b["label"]: Decimal(0) for b in profile["bands"]}, "unknown_seconds": Decimal(0)})
        duration = number(row["measures"][ledger["duration_basis"]])
        stated = sum((number(v) for v in ledger["seconds"].values()), Decimal(0)) + number(ledger["unclassified_seconds"])
        conflicts = [r for r in ledgers if (r["canonical_version_id"], r["profile_version_id"], r["duration_basis"]) ==
                     (ledger["canonical_version_id"], ledger["profile_version_id"], ledger["duration_basis"])]
        reason = None
        if len(conflicts) > 1:
            reason = "CONFLICTING_LEDGERS"
        elif duration is None:
            reason = "DURATION_NOT_PROVIDED"
        elif stated > duration:
            reason = "BAND_SECONDS_EXCEED_DURATION"
        elif inputs.stamp(profile["effective_from"]) > inputs.stamp(row["timing"]["start_utc"]) or (profile["effective_to"] and inputs.stamp(profile["effective_to"]) < inputs.stamp(row["timing"]["end_utc"])):
            reason = "PROFILE_DOES_NOT_COVER_SESSION"
        elif profile["reliability"] == "UNCALIBRATED" or profile["system"] == "RESEARCH_THREE" and profile["reliability"] != "CALIBRATED":
            reason = "UNCALIBRATED_METHOD"
        item = {"activity_id": row["activity_id"], "canonical_version_id": row["canonical_version_id"],
                "ledger": ledger, "status": reason or "SUPPORTED_SUPPLIED_METHOD",
                "unknown_seconds": decimal(duration if reason else duration - sum((number(v) for v in ledger["seconds"].values()), Decimal(0)))}
        group["activities"].append(item)
        # A conflict group contributes unknown duration only once, not per source.
        counted = (key, row["canonical_version_id"])
        if counted not in used:
            used.add(counted)
            if not reason:
                for label, value in ledger["seconds"].items():
                    group["seconds"][label] += number(value)
            if item["unknown_seconds"] is not None:
                group["unknown_seconds"] += number(item["unknown_seconds"])
    output = []
    for group in groups.values():
        covered = {r["canonical_version_id"] for r in group["activities"]}
        missing = [r for r in rows if r["canonical_version_id"] not in covered]
        group["unknown_seconds"] += sum((number(r["measures"][group["duration_basis"]]) or Decimal(0) for r in missing), Decimal(0))
        group["uncovered_activity_ids"] = [r["activity_id"] for r in missing]
        group["missing_duration_count"] = sum(r["measures"][group["duration_basis"]] is None for r in rows)
        group["seconds"] = {key: str(value) for key, value in group["seconds"].items()}
        group["unknown_seconds"] = str(group["unknown_seconds"])
        output.append(group)
    uncovered = [r for r in rows if not any(k[1] == r["canonical_version_id"] for k in used)]
    return {"groups": output, "excluded": excluded,
            "uncovered_activity_ids": [r["activity_id"] for r in uncovered],
            "uncovered_time": {key: {"known_unknown_seconds": str(sum((number(r["measures"][key]) or Decimal(0) for r in uncovered), Decimal(0))),
                                     "missing_duration_count": sum(r["measures"][key] is None for r in uncovered)} for key in MEASURES if key != "distance_m"},
            "status": "AVAILABLE_WITH_MISSINGNESS" if groups else "UNKNOWN",
            "interpretation": "Sourced measured band seconds; native five-zone and supplied calibrated three-zone methods remain separate. No default physiological thresholds or training prescription."}


def calculate(bundle):
    with localcontext() as ctx:
        ctx.prec = 128
        return _calculate(bundle)


def _calculate(bundle):
    cutoff = inputs.stamp(bundle["event_cutoff"])
    ready, unassigned, future = [], [], []
    for item in bundle["activities"]:
        if not item["timing"]:
            unassigned.append(item)
        elif inputs.stamp(item["timing"]["end_utc"]) <= cutoff:
            ready.append(item)
        else:
            future.append({"activity_id": item["activity_id"], "canonical_version_id": item["canonical_version_id"], "reason": "NOT_COMPLETED_BY_CUTOFF"})
    ready.sort(key=lambda row: (row["timing"]["start_utc"], row["activity_id"]))
    unknown = len(unassigned)

    def window(span, basis="END"):
        key = "end_utc" if basis == "END" else "start_utc"
        rows = [r for r in ready if inside(inputs.stamp(r["timing"][key]), span)]
        result = summarize(rows, coverage(bundle, span, basis), unknown)
        result["intensity"] = intensity(bundle, rows)
        result["fit_membership_disagreements"] = [r["activity_id"] for r in ready if r["selected_fit_span"] and
            inside(inputs.stamp(r["selected_fit_span"][key]), span) != inside(inputs.stamp(r["timing"][key]), span)]
        return result

    rolling = [{"days": days, **window((cutoff - timedelta(days=days), cutoff, False, True))} for days in CONFIG["rolling_days"]]
    longest = [{"days": days, **window((cutoff - timedelta(days=days), cutoff, False, True))} for days in CONFIG["longest_days"]]
    current = cutoff.astimezone(ZONE).date()
    monday = current - timedelta(days=current.weekday())
    weeks = []
    for offset in range(CONFIG["calendar_weeks"] - 1, -1, -1):
        day = monday - timedelta(weeks=offset)
        start = datetime.combine(day, time(), ZONE)
        end = datetime.combine(day + timedelta(days=7), time(), ZONE)
        start, end = inputs.stamp(start.isoformat()), inputs.stamp(end.isoformat())
        closed = end <= cutoff
        observed = (start, end if closed else cutoff, True, not closed)
        weeks.append({"iso_week": f"{day.isocalendar().year}-W{day.isocalendar().week:02}",
                      "local_from": day.isoformat(), "local_to_exclusive": (day + timedelta(days=7)).isoformat(),
                      "full_week_bounds": bounds((start, end, True, False)), "week_state": "CLOSED" if closed else "OPEN",
                      "fm_end": window(observed), "planner_start": window(observed, "START")})
    spacing = []
    for previous, following in zip(ready, ready[1:]):
        end, start = inputs.stamp(previous["timing"]["end_utc"]), inputs.stamp(following["timing"]["start_utc"])
        seconds = Decimal(str((start - end).total_seconds()))
        spacing.append({"previous_activity_id": previous["activity_id"], "next_activity_id": following["activity_id"],
                        "previous_end_utc": previous["timing"]["end_utc"], "next_start_utc": following["timing"]["start_utc"],
                        "seconds": str(seconds), "overlap": seconds < 0,
                        "coverage": coverage(bundle, (end, start, False, False), "END") if seconds > 0 else None,
                        "interpretation": "Observed spacing; an uncovered interval is not verified rest."})
    l30 = None
    proposal = bundle["proposal"]
    if proposal:
        day = inputs.stamp(proposal["start_utc"]).astimezone(ZONE).date()
        start = datetime.combine(day - timedelta(days=30), time(), ZONE)
        end = datetime.combine(day, time(), ZONE)
        report = window(interval(start.isoformat(), end.isoformat()))
        maximum = number(report["longest"]["observed_distance_m"])
        complete = report["coverage"]["complete"] and not unknown and not report["measures"]["distance_m"]["missing_count"]
        status = "INCOMPLETE_HISTORY" if not complete else "NOT_CALCULABLE" if not maximum else "CALCULABLE"
        ratio = number(proposal["distance_m"]) / maximum if status == "CALCULABLE" else None
        l30 = {**report, "status": status, "proposed_start_utc": proposal["start_utc"], "proposed_local_date": day.isoformat(),
               "proposed_distance_m": proposal["distance_m"], "ratio": decimal(ratio),
               "percentage_change": decimal(100 * (ratio - 1)) if ratio is not None else None,
               "denominator_m": decimal(maximum) if status == "CALCULABLE" else None,
               "future_history_unobserved": inputs.stamp(end.isoformat()) > cutoff,
               "interpretation": "Exposure comparison only; no injury probability, safety threshold or automatic mileage change."}
    last = max(ready, key=lambda r: r["timing"]["end_utc"], default=None)
    trailing = None
    if last:
        end = inputs.stamp(last["timing"]["end_utc"])
        trailing = {"from_activity_id": last["activity_id"], "seconds": str(Decimal(str((cutoff - end).total_seconds()))),
                    "coverage": coverage(bundle, (end, cutoff, False, True), "END") if end < cutoff else None,
                    "interpretation": "Time since latest observed completed run, not inferred rest."}
    return {"algorithm_version": VERSION, "event_cutoff": bundle["event_cutoff"], "knowledge_cutoff": bundle["knowledge_cutoff"],
            "time_basis": bundle["time_basis"], "input_hash": inputs.digest(bundle), "config": bundle["config"],
            "rolling": rolling, "longest": longest, "weeks": weeks, "l30": l30, "spacing": spacing,
            "since_last_run": trailing, "activities": [{**r, "completed_by_cutoff": True} for r in ready],
            "unassigned": unassigned, "unassigned_known_subtotals": {key: str(sum((number(r["measures"][key]) or Decimal(0) for r in unassigned), Decimal(0))) for key in MEASURES},
            "excluded": [*bundle["selection_exclusions"], *future],
            "input_records": bundle["records"], "human_validation": "UNVERIFIED", "scientific_validation": "NOT_APPLICABLE_DESCRIPTIVE_EXPOSURE",
            "zone_profiles_at_cutoff": [r for r in bundle["records"] if r["kind"] == "zone_profile" and inputs.stamp(r["effective_from"]) <= cutoff
                                        and (not r["effective_to"] or cutoff < inputs.stamp(r["effective_to"]))],
            "provider_lineage": bundle["provider_lineage"],
            "accounting": "FM end-week; planner start-week; legacy dashboard recorded local date. Whole completed sessions, fractional metres.",
            "missingness": "Census completeness and each measurement's completeness are independent. Missing runs or timing never mean rest.",
            "strength": "Separate context modality; never added to running kilometres."}


def parameters(as_of=None, knowledge_cutoff=None, mode="EVENT_TIME_RECONSTRUCTION", proposed_start=None, proposed_distance_m=None):
    created = inputs.utc(inputs.now())
    cutoff = inputs.utc(as_of or created)
    if mode not in ("EVENT_TIME_RECONSTRUCTION", "AS_KNOWN"):
        raise ValueError("Choose EVENT_TIME_RECONSTRUCTION or AS_KNOWN.")
    knowledge = inputs.utc(knowledge_cutoff or (cutoff if mode == "AS_KNOWN" else created))
    if inputs.stamp(knowledge) > inputs.stamp(created):
        raise ValueError("Knowledge cutoff cannot be in the future.")
    if mode == "AS_KNOWN" and inputs.stamp(knowledge) > inputs.stamp(cutoff):
        raise ValueError("AS_KNOWN cannot use knowledge after the event cutoff.")
    if bool(proposed_start) != (proposed_distance_m is not None):
        raise ValueError("Supply both proposed start and distance in metres.")
    proposal = None
    if proposed_start:
        distance = number(proposed_distance_m)
        if distance is None or len(str(distance)) > 80:
            raise ValueError("Proposed distance must be finite, nonnegative metres.")
        proposal = {"start_utc": inputs.utc(proposed_start), "distance_m": str(distance)}
    return cutoff, knowledge, mode, proposal, created


def read(store, **kwargs):
    cutoff, knowledge, mode, proposal, _ = parameters(**kwargs)
    with store.history() as db:
        bundle = collect(db, cutoff, knowledge, mode, proposal)
        result = calculate(bundle)
        diagnostic = current_diagnostics(db) if mode == "EVENT_TIME_RECONSTRUCTION" else None
        return {**result, "current_diagnostics": diagnostic} if diagnostic else result


def capture(store, **kwargs):
    cutoff, knowledge, mode, proposal, created = parameters(**kwargs)
    with store.connect(write=True, create=True) as db:
        bundle = collect(db, cutoff, knowledge, mode, proposal)
        result = calculate(bundle)
        identity = inputs.digest({"input_hash": result["input_hash"], "result": result})
        db.execute("INSERT OR IGNORE INTO workload_snapshots VALUES (?,?,?,?,?)",
                   (identity, result["input_hash"], inputs.encoded(bundle), inputs.encoded(result), created))
        saved = db.execute("SELECT created_at FROM workload_snapshots WHERE id=?", (identity,)).fetchone()[0]
        return {"snapshot_id": identity, "created_at": saved, "inputs": bundle, "result": result}


def snapshot(store, identity):
    with store.history() as db:
        row = db.execute("SELECT * FROM workload_snapshots WHERE id=?", (identity,)).fetchone() if inputs.table(db, "workload_snapshots") else None
        if row is None:
            return None
        return {"snapshot_id": row["id"], "created_at": row["created_at"], "inputs": json.loads(row["inputs"]), "result": json.loads(row["result"])}


def replay(store, identity):
    saved = snapshot(store, identity)
    if saved is None:
        return None
    if saved["inputs"]["code_sha256"] != code_hash():
        return {"status": "REQUIRES_RECORDED_CODE", "snapshot": saved}
    rebuilt = calculate(saved["inputs"])
    return {"status": "IDENTICAL" if rebuilt == saved["result"] else "MISMATCH", "snapshot_id": identity,
            "input_hash": saved["result"]["input_hash"], "result": rebuilt}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline G3 workload and immutable input ledger; no provider requests.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--archive")
    parser.add_argument("--as-of")
    parser.add_argument("--knowledge-cutoff")
    parser.add_argument("--mode", choices=("EVENT_TIME_RECONSTRUCTION", "AS_KNOWN"), default="EVENT_TIME_RECONSTRUCTION")
    parser.add_argument("--proposed-start")
    parser.add_argument("--proposed-distance-m")
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--capture", action="store_true")
    command.add_argument("--replay")
    command.add_argument("--import-json")
    args = parser.parse_args(argv)
    from storage import Archive, Store
    store = Store(args.db, Archive(args.archive) if args.archive else None)
    try:
        if args.import_json:
            value = inputs.ingest(store, Path(args.import_json).read_bytes())
        elif args.replay:
            value = replay(store, args.replay)
            if value is None:
                raise ValueError("Snapshot not found.")
        else:
            value = (capture if args.capture else read)(store, as_of=args.as_of, knowledge_cutoff=args.knowledge_cutoff,
                mode=args.mode, proposed_start=args.proposed_start, proposed_distance_m=args.proposed_distance_m)
        print(inputs.encoded(value))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
