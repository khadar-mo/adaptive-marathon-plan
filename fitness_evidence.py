"""Evaluate the existing G2/G4 selectors against identities and revisions visible at T,K."""
from contextlib import contextmanager

import canonical
import efforts
import estimators
import workload_inputs as wi
import workout_labels


@contextmanager
def selected_history(db, activities, event, knowledge):
    """Connection-local TEMP relations, built inside the caller's transaction.

    Published selectors read heads and owner tables. Reconstruct those relations
    from immutable selections for this call without changing the durable store.
    """
    if not wi.table(db, "effort_versions"):
        yield
        return
    tables, views = [], []

    def table(name, schema):
        db.execute(f"CREATE TEMP TABLE {name} ({schema})")
        tables.append(name)

    def view(name, sql):
        db.execute(f"CREATE TEMP VIEW {name} AS {sql}")
        views.append(name)

    try:
        table("fitness_bounds", "event TEXT, knowledge TEXT")
        db.execute("INSERT INTO fitness_bounds VALUES (?,?)", (event, knowledge))
        table("fitness_owners", "id INTEGER PRIMARY KEY, canonical TEXT")
        table("activity_redirects", "old_id INTEGER PRIMARY KEY, activity_id INTEGER")
        table("garmin_aliases", "garmin_id TEXT, activity_id INTEGER")
        for row in activities:
            aid = row["activity_id"]
            db.execute("INSERT INTO fitness_owners VALUES (?,?)", (aid, row["canonical_version_id"]))
            db.executemany("INSERT INTO activity_redirects VALUES (?,?)", [(i, aid) for i in row["identity"]["merged_activity_ids"]])
            db.executemany("INSERT INTO garmin_aliases VALUES (?,?)", [(i["id"], aid) for i in row["identity"]["provider_ids"] if i["provider"] == "garmin"])
        view("activities", "SELECT id FROM fitness_owners ORDER BY id")
        view("canonical_activity_heads", "SELECT id AS activity_id,canonical AS version_id FROM fitness_owners")
        k = "(SELECT knowledge FROM fitness_bounds)"
        t = "(SELECT event FROM fitness_bounds)"
        for name, clock in (("effort_versions", "created_at"), ("effort_selections", "selected_at"),
                            ("effort_inspections", "created_at")):
            view(name, f"SELECT rowid AS rowid,* FROM main.{name} WHERE {clock}<={k}")
        # An interrupted extraction against an earlier canonical version cannot
        # turn a superseded trace into an achieved best for the new selection.
        view("effort_heads", """SELECT o.id AS activity_id,e.version_id FROM fitness_owners o
            JOIN effort_selections e ON e.id=(SELECT z.id FROM effort_selections z WHERE z.activity_id=o.id
            ORDER BY z.selected_at DESC,z.id DESC LIMIT 1)
            JOIN effort_versions v ON v.id=e.version_id AND v.canonical_version_id=o.canonical""")
        view("effort_reviews", f"SELECT * FROM main.effort_reviews WHERE created_at<=min({t},{k})")
        # An existing annotation commonly has no separately supplied event time.
        # The adapter will then use the source candidate's end as its event time.
        view("effort_contexts", f"""SELECT rowid AS rowid,* FROM main.effort_contexts
            WHERE known_at<={k} AND (json_extract(payload,'$.event_utc') IS NULL OR
            fitness_utc(json_extract(payload,'$.event_utc'))<={t})""")
        if wi.table(db, "workout_labels"):
            view("workout_labels", f"""SELECT rowid AS rowid,* FROM main.workout_labels
                WHERE known_at<={k} AND (event_utc IS NULL OR fitness_utc(event_utc)<={t})""")
        yield
    finally:
        for name in reversed(views):
            db.execute(f"DROP VIEW temp.{name}")
        for name in reversed(tables):
            db.execute(f"DROP TABLE temp.{name}")


def capture(db, archive, workload_bundle, estimator_config):
    event, knowledge = workload_bundle["event_cutoff"], workload_bundle["knowledge_cutoff"]
    # Exclude a session with an accepted future start/end before querying its
    # labels, geometry or extraction. G2 also checks its own FIT/sample clocks.
    activities = [row for row in workload_bundle["activities"] if not row["timing"] or row["timing"]["end_utc"] <= event]
    if db is not None:
        db.create_function("fitness_utc", 1, wi.utc, deterministic=True)
    with selected_history(db, activities, event, knowledge):
        config = estimators.configuration(db, estimator_config)
        request = estimators.Calculation(cutoff=event, config_version=estimator_config)
        session = estimators.Capture(db, archive, event)
        session.context_read_at = knowledge
        g4 = estimators.build(session, request, config)
        traces, labels = {}, {}
        if wi.table(db, "effort_versions"):
            for row in activities:
                aid = row["activity_id"]
                labels[str(aid)] = workout_labels.read_in_db(db, aid, as_of=event, knowledge_as_of=knowledge)
                extracted = session.load(aid)
                if extracted["state"] != "PROCESSED" or extracted["recompute_required"]:
                    continue
                source = efforts.select_source(extracted)
                if not source or not source.get("source_version_id") or not source.get("header", {}).get("end_utc"):
                    continue
                if any(wi.stamp(clock) > wi.stamp(event) for clock in (source["header"]["end_utc"], source.get("last_sample_utc")) if clock):
                    continue
                sha = source["fit_sha256"]
                if sha not in session.archive_available:
                    session.archive_available[sha] = canonical.source_bytes(archive, source) is not None
                if not session.archive_available[sha]:
                    continue
                data, qc, source_created_at = efforts.source_data(db, source)
                traces[str(aid)] = {"source_sha256": sha, "source_version_id": source["source_version_id"],
                    "source_created_at": source_created_at, "qc_version_id": source["qc_version_id"],
                    "canonical_version_id": extracted["canonical_version_id"], "extraction_version_id": extracted["version_id"],
                    "config": extracted["qc_config"], "parser": data["parser"], "trace": data["trace"], "qc": qc,
                    "global_flags": efforts.global_flags(row["canonical_payload"], source, data["trace"]),
                    "rejected_intervals": sorted(efforts.rejected_intervals(db, extracted, source))}
    return {"estimator": g4, "traces": traces, "labels": labels,
            "selection_policy": "Existing G2/G4 calculations over canonical identities and selected extraction versions known at K. Session and label events must be eligible at T. Review decisions use their recorded instant. Captured source bytes verified when assembling."}
