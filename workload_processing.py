"""Timestamped processing inventories; legacy existence is never backdated."""
from datetime import timedelta
import re

import canonical
from garmin_activity import RUN_TYPES
import workload_inputs as inputs


VERSION = "g3-processing-inventory-v2"


def clock_evidence(value, observed_by=None):
    """A legacy truncated clock cannot establish a precise earlier-known instant."""
    recorded = inputs.stamp(value)
    fraction = re.search(r"[T ]\d{2}:\d{2}:\d{2}[.,](\d+)", value)
    digits = len(fraction[1]) if fraction else 0
    precise = digits >= 6
    upper = recorded if precise else recorded + timedelta(microseconds=10 ** (6 - digits))
    available = upper
    basis = "PRECISE_IMPORT_CLOCK" if precise else "TRUNCATED_INTERVAL_ELAPSED"
    if observed_by and inputs.stamp(observed_by) < available:
        available, basis = inputs.stamp(observed_by), "IMMUTABLE_INVENTORY_OBSERVATION"
    return {"recorded_at": value, "precision": "MICROSECOND" if precise else "SECOND_TRUNCATED" if not digits else "FRACTION_TRUNCATED",
            "recorded_lower_utc": recorded.isoformat(timespec="microseconds"),
            "recorded_upper_utc": upper.isoformat(timespec="microseconds"), "upper_exclusive": not precise,
            "available_by": available.isoformat(timespec="microseconds"), "availability_basis": basis}


def import_boundary(db, cutoff=None, inventory=None):
    rows = []
    if not inputs.table(db, "imports"):
        return rows
    previous = {row["id"]: row for row in inventory["import_boundary"]} if inventory else {}
    for source in db.execute("SELECT id, started_at, finished_at FROM imports ORDER BY id"):
        row = dict(source)
        if cutoff:
            old = previous.get(row["id"], {})
            proof = inventory["known_at"] if inventory and old.get("started_at") == row["started_at"] else None
            if inputs.stamp(clock_evidence(row["started_at"], proof)["available_by"]) > inputs.stamp(cutoff):
                continue
            if row["finished_at"]:
                proof = inventory["known_at"] if inventory and old.get("finished_at") == row["finished_at"] else None
                if inputs.stamp(clock_evidence(row["finished_at"], proof)["available_by"]) > inputs.stamp(cutoff):
                    row["finished_at"] = None
        rows.append(row)
    return rows


def observe(db, *, persist=False):
    """Observe inside the caller's transaction. GET never persists an inventory."""
    rows = []
    if inputs.table(db, "activities"):
        for row in db.execute("SELECT * FROM activities ORDER BY id"):
            if row["sport"] not in RUN_TYPES:
                continue
            summary = canonical.summary(dict(row))
            start, end, issue = None, None, "MISSING_TIMING"
            try:
                start = inputs.utc(summary["start_utc"]) if summary["start_utc"] else None
                end = inputs.utc(summary["end_utc"]) if summary["end_utc"] else None
                if start and end:
                    issue = None if inputs.stamp(end) >= inputs.stamp(start) else "INVALID_TIMING"
            except (ValueError, OverflowError):
                issue = "INVALID_TIMING"
            timing = {"start_utc": start, "end_utc": end, "end_basis": summary["end_basis"]} if issue is None else None
            sources = [dict(r) for r in db.execute("SELECT kind, sha256, format FROM activity_sources WHERE activity_id=? ORDER BY kind,sha256", (row["id"],))]
            rows.append({"activity_id": row["id"], "summary_at_observation": summary, "timing": timing,
                         "known_start_utc": start if issue != "INVALID_TIMING" else None, "timing_issue": issue,
                         "source_refs_at_observation": sources,
                         "identity_basis": "LOCAL_ROW_OBSERVED_NOW_NOT_ORIGINAL_HISTORICAL_IDENTITY"})
    payload = {"version": VERSION, "activities": rows, "import_boundary": import_boundary(db),
               "import_clock_policy": "New import clocks retain microseconds. Legacy truncated clocks are intervals, not precise availability instants; an immutable inventory can independently prove an earlier-known state."}
    known = inputs.utc(inputs.now())  # After reading the transaction's state, never a source event date.
    identity = inputs.digest({"payload": payload, "known_at": known})
    observation = {"id": identity, "known_at": known, **payload}
    if persist:
        db.execute("INSERT OR IGNORE INTO workload_processing_inventories VALUES (?,?,?)",
                   (identity, known, inputs.encoded(payload)))
    return observation


def select(db, cutoff):
    if not inputs.table(db, "workload_processing_inventories"):
        return None
    import json
    row = db.execute("SELECT * FROM workload_processing_inventories WHERE known_at<=? ORDER BY known_at DESC,rowid DESC LIMIT 1", (inputs.utc(cutoff),)).fetchone()
    return {"id": row["id"], "known_at": row["known_at"], **json.loads(row["payload"])} if row else None


def gaps(db, inventory, selected, cutoff):
    """Resolve only against canonical selections/identity groups actually known at K."""
    if inventory is None:
        return [{"reason": "PROCESSING_INVENTORY_UNKNOWN_AT_CUTOFF", "known_at": None,
                 "timing": None, "known_start_utc": None, "activity_id": None,
                 "guidance": "No timestamped processing inventory establishes completeness at this knowledge cutoff. Use a current calculation or a recorded inventory; do not backdate legacy knowledge."}]
    processed = {identity for row in selected for identity in
                 [row["activity_id"], *row["identity"]["merged_activity_ids"]]}
    result = [{**row, "reason": "CANONICAL_PROCESSING_MISSING", "known_at": inventory["known_at"],
               "inventory_id": inventory["id"], "guidance": "Recompute this canonical activity, then calculate again.",
               "recompute_url": f'/api/activities/{row["activity_id"]}/canonical/recompute'}
              for row in inventory["activities"] if row["activity_id"] not in processed]
    previous = {row["id"]: row for row in inventory["import_boundary"]}
    for row in import_boundary(db, cutoff, inventory):
        if previous.get(row["id"]) != row or row["finished_at"] is None:
            field = "finished_at" if row["finished_at"] else "started_at"
            proof = inventory["known_at"] if previous.get(row["id"], {}).get(field) == row[field] else None
            availability = clock_evidence(row[field], proof)
            result.append({"reason": "IMPORT_PROCESSING_NOT_OBSERVED", "import_id": row["id"],
                           "known_at": availability["available_by"], "source_availability": availability, "timing": None,
                           "known_start_utc": None, "activity_id": None, "inventory_id": inventory["id"],
                           "guidance": "This import has no processing inventory for its state at K. Calculate with current knowledge after processing completes; no activity timing or alias is inferred."})
    return result


def affects(gap, span, basis, cutoff):
    """Supported timing narrows uncertainty; an unknown instant stays unknown."""
    timing = gap.get("timing")
    if timing:
        if inputs.stamp(timing["end_utc"]) > cutoff:
            return False
        value = inputs.stamp(timing["end_utc" if basis == "END" else "start_utc"])
        return (value > span[0] or value == span[0] and span[2]) and (value < span[1] or value == span[1] and span[3])
    start = gap.get("known_start_utc")
    if start:
        value = inputs.stamp(start)
        if value > cutoff or value > span[1] or value == span[1] and not span[3]:
            return False
        if basis == "START":
            return value > span[0] or value == span[0] and span[2]
    return True


def status(gaps):
    return {"status": "INCOMPLETE_PROCESSING" if gaps else "COMPLETE", "complete": not gaps,
            "gaps": gaps, "unknown_instant_count": sum(not gap.get("timing") for gap in gaps)}
