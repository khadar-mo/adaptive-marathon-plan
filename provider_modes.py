"""Versioned ingress choices; supported imports reuse Garmin/FIT canonical storage.

The decision function is pure so synthetic contracts can exercise an eligible
secondary source. The runtime Strava adapter always denies analytical ingestion;
there is no setting, credential or request field that can enable that branch.
"""
import json

import canonical
import garmin_coverage


VERSION = "provider-modes-v1"
MODES = ("GARMIN_ONLY", "STRAVA_ONLY", "BOTH", "GARMIN_PRIMARY_FALLBACK")
POLICY = {"state": "POLICY_BLOCKED", "activity_ids": [], "coverage": "UNAVAILABLE",
          "reason": "Strava activity analytics and persistent imports are unavailable under the current API policy.",
          "policy_checked_at": "2026-11-25", "policy_effective_date": "2026-06-01",
          "policy_url": "https://www.strava.com/legal/api_policy", "external_issue": 33}
SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_config_revisions (
    revision INTEGER PRIMARY KEY, mode TEXT NOT NULL, selected_at TEXT NOT NULL,
    contract_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_attempts (
    id INTEGER PRIMARY KEY, config_revision INTEGER NOT NULL, mode TEXT NOT NULL,
    contract_version TEXT NOT NULL, requested_from TEXT, requested_to TEXT,
    import_id INTEGER UNIQUE REFERENCES imports(id), started_at TEXT NOT NULL,
    finished_at TEXT, result TEXT
);
"""


class StaleConfiguration(ValueError):
    pass


def selected(db):
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_config_revisions'").fetchone():
        row = db.execute("SELECT * FROM provider_config_revisions ORDER BY revision DESC LIMIT 1").fetchone()
        if row:
            return dict(row)
    return {"revision": 0, "mode": "GARMIN_ONLY", "selected_at": None, "contract_version": VERSION}


def configure(store, mode, expected_revision):
    if mode not in MODES or type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Choose a supported source mode and its current revision.")
    with store.recovery_lock, store.connect(write=True, create=True) as db:
        current = selected(db)
        if current["revision"] != expected_revision:
            raise StaleConfiguration("Source settings changed. Reload this page before saving again.")
        if mode == current["mode"]:
            return current
        revision = db.execute("INSERT INTO provider_config_revisions(mode, selected_at, contract_version) VALUES (?, ?, ?)",
                              (mode, canonical.knowledge_time(), VERSION)).lastrowid
        return dict(db.execute("SELECT * FROM provider_config_revisions WHERE revision=?", (revision,)).fetchone())


def strava_analytics():
    """Deny before loading credentials, constructing a REST client or accepting data."""
    return {**POLICY, "activity_ids": []}


def decide(mode, garmin, strava):
    """Combine observations referencing G1 IDs, never match or ingest raw payloads.

    Canonical IDs supplied by ingestion already account for explicit source links;
    unresolved copies stay in G1 review and are not supplied as completed actuals.
    An empty relevant response is a fallback cause, never an outage diagnosis.
    """
    if mode not in MODES:
        raise ValueError("Unsupported provider mode.")
    skipped = {"state": "NOT_SELECTED", "activity_ids": [], "coverage": "NOT_CHECKED"}
    primary = garmin if mode != "STRAVA_ONLY" else skipped
    fallback_reason = None
    if mode == "GARMIN_PRIMARY_FALLBACK":
        if primary["state"] == "EMPTY_RELEVANT":
            fallback_reason = "GARMIN_EMPTY_RELEVANT"
        elif primary["state"] not in ("AVAILABLE", "RUNNING"):
            fallback_reason = "GARMIN_" + primary["state"]
    wants_strava = mode in ("STRAVA_ONLY", "BOTH") or fallback_reason is not None
    secondary = strava if wants_strava else skipped
    ids = list(dict.fromkeys([*primary.get("activity_ids", []), *secondary.get("activity_ids", [])]))
    sources = [name for name, item in (("garmin", primary), ("strava", secondary)) if item.get("activity_ids")]
    failures = [p["state"] for p in (primary, secondary) if p["state"] not in
                ("AVAILABLE", "EMPTY_RELEVANT", "NOT_SELECTED")]
    if "RUNNING" in failures:
        state = "RUNNING"
    elif failures:
        state = "PARTIAL" if ids or primary["state"] == "EMPTY_RELEVANT" else failures[0]
    else:
        state = "COMPLETE"
    return {"state": state, "providers": {"garmin": primary, "strava": secondary},
            "activity_ids": ids, "selected_sources": sources, "fallback_reason": fallback_reason,
            "fallback_state": secondary["state"] if fallback_reason else "NOT_NEEDED",
            "source_precedence": ["garmin", "strava"], "contract_version": VERSION}


def start(store, first, last):
    """Snapshot mode before dispatch; share the importer's single-writer guard."""
    from storage import ActiveImport

    with store.recovery_lock:
        if store.active_import is not None:
            raise ActiveImport(store.active_import)
        with store.history() as db:
            config = selected(db)
        import_id = None
        if config["mode"] != "STRAVA_ONLY":
            import_id = store.start(first, last)
        try:
            with store.connect(write=True, create=True) as db:
                attempt_id = db.execute("""INSERT INTO provider_attempts
                    (config_revision, mode, contract_version, requested_from, requested_to, import_id, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (config["revision"], config["mode"], VERSION, first, last,
                                                     import_id, canonical.knowledge_time())).lastrowid
                if import_id is None:
                    result = decide(config["mode"], {}, strava_analytics())
                    db.execute("UPDATE provider_attempts SET result=?, finished_at=? WHERE id=?",
                               (canonical.encoded(result), canonical.knowledge_time(), attempt_id))
            return attempt_id, import_id
        except Exception:
            if import_id is not None:
                store.finish(import_id, "failed")
            raise


def run(store, archive, attempt_id, import_id):
    from garmin_import import run_import

    with store.recovery_lock:
        store.active_provider_attempts.add(attempt_id)
    try:
        observation = run_import(store, archive, import_id, observation=True)
        with store.connect(write=True) as db:
            mode = db.execute("SELECT mode FROM provider_attempts WHERE id=?", (attempt_id,)).fetchone()[0]
            # Policy is checked independently of Garmin's outcome, never via OAuth.
            result = decide(mode, observation, strava_analytics())
            db.execute("UPDATE provider_attempts SET result=?, finished_at=? WHERE id=? AND result IS NULL",
                       (canonical.encoded(result), canonical.knowledge_time(), attempt_id))
    finally:
        with store.recovery_lock:
            store.active_provider_attempts.discard(attempt_id)


def attempt_row(row, active_attempts, active_import):
    result = dict(row)
    if row["result"] is not None:
        result["result"] = json.loads(row["result"])
    else:
        # The import guard covers queued work; explicit outer ownership covers
        # result persistence after Garmin finishes. Failed workers clear ownership,
        # and a restarted Store owns neither, even if the inner row was terminal.
        live = row["id"] in active_attempts or row["import_id"] is not None and row["import_id"] == active_import
        state = "RUNNING" if live else "INTERRUPTED"
        result["result"] = decide(row["mode"], {"state": state, "activity_ids": [], "coverage": "INCOMPLETE"},
                                  strava_analytics())
    result.update(html_url=f'/providers?attempt={row["id"]}', attempt_url=f'/api/provider-attempts/{row["id"]}')
    if row["import_id"]:
        result["import_html_url"] = f'/imports/{row["import_id"]}'
        result["import_url"] = f'/api/imports/{row["import_id"]}'
    return result


def read_attempt(store, attempt_id):
    from storage import local_id

    # Match ownership to the same read boundary as row visibility. Keep the
    # importer's recovery-lock-before-SQLite order so starts cannot race this read.
    with store.recovery_lock, store.history() as db:
        ownership = frozenset(store.active_provider_attempts), store.active_import
        if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_attempts'").fetchone():
            return None
        row = db.execute("SELECT * FROM provider_attempts WHERE id=?", (local_id(attempt_id),)).fetchone()
        return attempt_row(row, *ownership) if row else None


def capabilities(payload, trace, qc):
    channels = trace["coverage"]["samples"] if trace else {}
    complete = lambda key: channels.get(key, {}).get("status") == "RECORDED"
    dense = complete("timestamp_utc") and complete("distance_m") and trace["samples"]["total"] >= 2
    events = trace["coverage"]["timer_events"] if trace else "NOT_PROVIDED"
    tier = "C" if dense and complete("heart_rate_bpm") and complete("elevation_m") and events == "RECORDED" else "B" if dense else "A"
    quality = qc.get("state", "INSUFFICIENT")
    if quality == "VALID" and any(c["requires_distance_review"] for c in payload["summary_comparisons"]):
        quality = "SUSPECT"
    return {"tier": tier, "summary_totals": "AVAILABLE" if payload["summary"]["distance_m"] is not None else "MISSING",
            "continuous_efforts": quality if dense else "INSUFFICIENT", "timer_event_coverage": events,
            "channels": channels, "wellness_feedback": "NOT_ASSESSED", "scientific_validation": "UNVERIFIED"}


def status(store):
    """Local persisted evidence only; reads never test credentials or contact APIs."""
    with store.recovery_lock, store.history() as db:
        ownership = frozenset(store.active_provider_attempts), store.active_import
        config = selected(db)
        revisions, attempts, activities, verified, pending = [], [], [], [], []
        if db is not None:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "provider_config_revisions" in tables:
                revisions = [dict(r) for r in db.execute("SELECT * FROM provider_config_revisions ORDER BY revision DESC")]
                attempts = [attempt_row(r, *ownership) for r in db.execute("SELECT * FROM provider_attempts ORDER BY id DESC LIMIT 20")]
            if "garmin_scans" in tables:
                verified, pending = garmin_coverage.coverage_ranges(db)
            if "canonical_activity_heads" in tables:
                for row in db.execute("""SELECT v.id, v.created_at, v.payload FROM canonical_activity_heads h
                        JOIN canonical_activity_versions v ON v.id=h.version_id JOIN activities a ON a.id=h.activity_id
                        ORDER BY a.id"""):
                    payload = json.loads(row["payload"])
                    if not payload["running_sport"]:
                        continue
                    source = next((s for s in payload["sources"] if s["source_version_id"] == payload["selected_source_version_id"]), None)
                    trace, qc = None, {}
                    if source and source["source_version_id"]:
                        trace = json.loads(db.execute("SELECT payload FROM canonical_source_versions WHERE id=?", (source["source_version_id"],)).fetchone()[0])["trace"]
                        qc = json.loads(db.execute("SELECT payload FROM canonical_qc_versions WHERE id=?", (source["qc_version_id"],)).fetchone()[0])
                    activities.append({"activity_id": payload["canonical_activity_id"], "canonical_version_id": row["id"],
                                       "known_at": row["created_at"], "provider_ids": payload["provider_ids"],
                                       "selected_fit_sha256": payload["selected_fit_sha256"],
                                       "selection_policy": payload["selection_policy"], "raw_sources": payload["raw_sources"],
                                       "capability": capabilities(payload, trace, qc)})
    ranges = lambda pairs: [{"from": garmin_coverage.iso(a), "to": garmin_coverage.iso(b)} for a, b in pairs]
    return {"config": config, "revisions": revisions, "modes": list(MODES), "attempts": attempts,
            "providers": {"garmin": {"state": "SUPPORTED", "connection": "NOT_CHECKED", "fit_exports": "SUPPORTED",
                                      "checked_ranges": ranges(verified), "incomplete_ranges": ranges(pending)},
                          "strava": strava_analytics()},
            "activities": activities, "unresolved_matches": store.pending(),
            "history_policy": "Mode changes affect future selected-source imports; saved actuals and source lineage are retained."}
