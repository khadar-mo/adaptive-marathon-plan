"""Optional completed-run fueling evidence; no nutritional or fitness decisions."""
import json

import canonical
import context_data as context
from garmin_activity import RUN_TYPES


def entry_target(db, activity_id, known_at):
    """Capture the recorded session once; corrections keep this original reference."""
    target = context.target_state(db, activity_id)
    if target["sport"] not in RUN_TYPES:
        raise ValueError("Fueling practice must reference a recorded running activity.")
    if target["canonical_version_id"]:
        payload = db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (target["canonical_version_id"],)).fetchone()[0]
        summary = json.loads(payload)["summary"]
    else:
        summary = canonical.summary(dict(db.execute("SELECT * FROM activities WHERE id=?", (target["activity_id"],)).fetchone()))
    end = summary.get("end_utc")
    start = summary.get("start_utc")
    if (start and context.moment(start) > known_at) or (end and context.moment(end) > known_at):
        raise ValueError("Fueling history requires a completed activity, not a future run.")
    sources = [dict(row) for row in db.execute("SELECT kind,sha256,format,acquired_at FROM activity_sources WHERE activity_id=? ORDER BY kind,sha256", (target["activity_id"],))]
    return {**target, "activity_end_utc": end, "end_basis": summary.get("end_basis", "NOT_PROVIDED"),
            "completion_time_state": "RECORDED" if end else "UNKNOWN", "sources": sources}


def read(store, activity_id, *, as_of=None):
    if type(activity_id) is not int or not 1 <= activity_id <= context.MAX_SQLITE_INTEGER:
        raise ValueError("Use a positive signed-64-bit activity ID.")
    data = context.read(store, as_of=as_of)
    with store.history() as db:
        target = context.target_state(db, activity_id) if db else None
        if target is None or target["status"] == "UNAVAILABLE":
            raise ValueError("Activity not found.")
        def selected(rows):
            return [r for r in rows if r["kind"] == "fueling" and context.owner(db, r["activity_id"]) == target["activity_id"]]
        reports, withheld = selected(data["records"]), selected(data["withheld"])
    return {"contract_version": "fueling-practice-v1", "activity_id": activity_id,
            "target_now": {**target, "time_basis": "current_identity_not_as_of_evidence"}, "as_of": data["as_of"],
            "state": "MULTIPLE_REPORTS" if len(reports) > 1 else "REPORTED" if reports else "MISSING",
            "reports": reports, "withheld": withheld,
            "practice_status": reports[0]["practice_status"] if len(reports) == 1 else "UNKNOWN",
            "tolerance_status": reports[0]["tolerance_status"] if len(reports) == 1 else "UNKNOWN",
            "missing_reason": None if reports else "No fueling entry supplied at this cutoff; practice and tolerance are unknown.",
            "research_status": "RESEARCH_PENDING", "optional": True,
            "identity_basis": "Current redirects group reports; original activity, canonical version and source references remain in target_at_entry.",
            "planned_session_link": "Not assigned here. Consumers can link record_id and exact version_id without rewriting this history."}


def revision(store, version_id, *, as_of=None):
    cutoff = context.moment(as_of) if as_of else context.now()
    with store.history() as db:
        if not context.initialized(db):
            return None
        row = db.execute("""SELECT v.* FROM context_versions v JOIN context_records r ON r.id=v.record_id
            WHERE v.id=? AND r.kind='fueling' AND v.known_at<=?""", (version_id, cutoff)).fetchone()
        return context.decorate(db, row, cutoff) if row else None
