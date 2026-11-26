"""Coalesce committed input events and transactionally publish an up-to-date snapshot."""
import asyncio
import logging
import sqlite3

import forecast_validation
import fitness_inputs
import fm_snapshots
import workload_inputs as wi

log = logging.getLogger(__name__)


def watermark(db):
    row = db.execute("SELECT MAX(sequence) FROM fm_events").fetchone()
    return row[0] or 0


def refresh(store):
    # Skip fresh/read-only installations with no history. An older existing
    # history gets its first dated inventory on migration, never backdated.
    with store.history() as db:
        if db is None:
            return {"status": "EMPTY_HISTORY"}
        if wi.table(db, "fm_refreshes"):
            through = watermark(db)
            row = db.execute("SELECT * FROM fm_refreshes ORDER BY through_sequence DESC LIMIT 1").fetchone()
            if row and row["through_sequence"] == through:
                return {"status": "CURRENT", **dict(row)}
    # Recheck inside the single writer. A failed attempt leaves events pending;
    # multiple schedulers and concurrent request retries converge on one result.
    with store.connect(write=True, create=True) as db:
        if db.execute("SELECT 1 FROM imports WHERE state='running'").fetchone() or store.active_provider_attempts:
            return {"status": "INGRESS_IN_PROGRESS"}
        through = watermark(db)
        prior = db.execute("SELECT * FROM fm_refreshes WHERE through_sequence=?", (through,)).fetchone()
        if prior:
            return {"status": "CURRENT", **dict(prior)}
        selected = db.execute("SELECT config_version FROM fm_config_selections ORDER BY sequence DESC LIMIT 1").fetchone()
        row = fm_snapshots.build_fitness_snapshot(db, store.archive,
            config_version=selected[0] if selected else fitness_inputs.DEFAULT_VERSION)
        forecast_validation.audit_in_db(db, row["knowledge_cutoff"])
        db.execute("INSERT INTO fm_refreshes VALUES (?,?)", (through, row["id"]))
        return {"status": "CAPTURED", "through_sequence": through, "snapshot_id": row["id"]}


async def run(store, interval=15):
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(refresh, store)
        except (sqlite3.Error, OSError, ValueError, LookupError):
            log.exception("Fitness snapshot refresh failed. Committed events will be retried.")


def activate(store, config_version, key, actor, reason):
    with store.connect(write=True, create=True) as db:
        at = wi.now()
        fitness_inputs.configuration(db, config_version, at)
        previous = db.execute("SELECT * FROM fm_config_selections WHERE event_key=?", (key,)).fetchone()
        if previous:
            if [previous[k] for k in ("config_version", "actor", "reason")] != [config_version, actor, reason]:
                raise wi.Conflict("This activation key already identifies a different configuration selection.")
            return dict(previous)
        db.execute("INSERT INTO fm_config_selections(event_key,config_version,selected_at,actor,reason) VALUES(?,?,?,?,?)",
                   (key, config_version, at, actor, reason))
        return dict(db.execute("SELECT * FROM fm_config_selections WHERE event_key=?", (key,)).fetchone())
