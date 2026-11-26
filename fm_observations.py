"""Freeze eligible observations and full-workout outcome choices, including slower non-PBs."""
import canonical
import efforts
import fitness_evidence


def extract(db, archive, g5):
    wb = g5["workload"]
    t, k = g5["event_cutoff"], g5["knowledge_cutoff"]
    rows = [r for r in wb["activities"] if r["timing"] and r["timing"]["end_utc"] <= t]
    fixed = {o["id"]: o for o in g5["capability_evidence"]["estimator"]["evidence"]["observations"]}
    if rows:
        with fitness_evidence.selected_history(db, rows, t, k):
            session = None
            for row in rows:
                version = efforts.load(db, row["activity_id"])
                if version["state"] != "PROCESSED" or version["recompute_required"]:
                    continue
                source = efforts.select_source(version)
                if not source or not source["source_version_id"]:
                    continue
                for c in efforts.candidates(source["result"]):
                    if c["kind"] != "WHOLE_ACTIVITY":
                        continue
                    ref = {"activity_id": row["activity_id"], "version_id": version["version_id"],
                           "source_sha256": source["fit_sha256"], "candidate_id": c["id"]}
                    key = canonical.fingerprint(ref)
                    if key in fixed:
                        continue
                    if session is None:
                        session = fitness_evidence.BoundedCapture(db, archive, t)
                        session.context_read_at = k
                    fixed[key] = session.resolve(ref)
    return [{"id": o["id"], "ref": o["ref"], "candidate": o["candidate"], "age_coordinate_s": o["age_coordinate_s"],
             "canonical_version_id": o["canonical_version_id"], "source_version_id": o["source_version_id"],
             "extraction_config": o["extraction"].get("config"), "extraction_selected": o["extraction"].get("version_id"),
             "exact_end_clock": o["exact_end_clock"], "qualification": o["qualification"],
             "unavailable_reasons": o["unavailable_reasons"]} for o in sorted(fixed.values(), key=lambda o: o["id"])]
