"""Timestamped processing inventories; legacy existence is never backdated."""
import canonical
from garmin_activity import RUN_TYPES
import workload_inputs as inputs


VERSION = "g3-processing-inventory-v1"


def import_boundary(db, cutoff=None):
    rows = []
    if not inputs.table(db, "imports"):
        return rows
    for source in db.execute("SELECT id, started_at, finished_at FROM imports ORDER BY id"):
        row = dict(source)
        if cutoff and inputs.stamp(row["started_at"]) > inputs.stamp(cutoff):
            continue
        if cutoff and row["finished_at"] and inputs.stamp(row["finished_at"]) > inputs.stamp(cutoff):
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
    payload = {"version": VERSION, "activities": rows, "import_boundary": import_boundary(db)}
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
    for row in import_boundary(db, cutoff):
        if previous.get(row["id"]) != row or row["finished_at"] is None:
            result.append({"reason": "IMPORT_PROCESSING_NOT_OBSERVED", "import_id": row["id"],
                           "known_at": row["finished_at"] or row["started_at"], "timing": None,
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
