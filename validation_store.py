"""Additive storage for frozen fitness inputs and the append-only validation ledger."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS fm_snapshots (
 id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, event_cutoff TEXT NOT NULL,
 knowledge_cutoff TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL,
 config_version TEXT NOT NULL, previous_id TEXT, inputs BLOB NOT NULL,
 result BLOB NOT NULL, changes BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS fm_snapshot_cutoffs ON fm_snapshots(event_cutoff,knowledge_cutoff);
CREATE TABLE IF NOT EXISTS fm_events (
 sequence INTEGER PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL,
 UNIQUE(source,source_key)
);
CREATE TABLE IF NOT EXISTS fm_config_selections (
 sequence INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, config_version TEXT NOT NULL,
 selected_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fm_refreshes (
 through_sequence INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES fm_snapshots(id)
);
CREATE TABLE IF NOT EXISTS fm_predictions (
 id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES fm_snapshots(id),
 created_at TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS fm_evaluations (
 id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL REFERENCES fm_predictions(id),
 created_at TEXT NOT NULL, previous_id TEXT, payload BLOB NOT NULL
);
"""

# These tables append immutable decision records. Replaying an import or a
# correction with its original key cannot create a second refresh event.
INPUT_EVENTS = {
    "canonical_selections": "id", "effort_selections": "id", "effort_inspections": "id",
    "effort_reviews": "id", "effort_contexts": "id", "workout_labels": "id",
    "workout_configs": "version", "context_definitions": "rowid", "context_versions": "rowid",
    "workload_input_versions": "id", "source_acquisitions": "id", "source_group_history": "id",
    "source_identity_reviews": "id", "provider_config_revisions": "revision",
    "fitness_run_context": "id", "fitness_configs": "version", "estimator_configs": "version",
    "fm_config_selections": "event_key", "qc_configs": "version", "effort_configs": "version",
}

for table, key in INPUT_EVENTS.items():
    SCHEMA += f"""
    CREATE TRIGGER IF NOT EXISTS fm_change_{table} AFTER INSERT ON {table}
    BEGIN INSERT OR IGNORE INTO fm_events(source,source_key) VALUES('{table}', NEW.{key}); END;
    """

for table, terminal in (("imports", "finished_at"), ("provider_attempts", "finished_at")):
    for action in ("INSERT", "UPDATE"):
        SCHEMA += f"""
        CREATE TRIGGER IF NOT EXISTS fm_terminal_{table}_{action} AFTER {action} ON {table}
        WHEN NEW.{terminal} IS NOT NULL
        BEGIN INSERT OR IGNORE INTO fm_events(source,source_key) VALUES('{table}',NEW.id || '/' || NEW.{terminal}); END;
        """
