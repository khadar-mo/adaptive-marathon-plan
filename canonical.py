"""Append-only canonical source/QC versions and explicit offline recomputation."""
import argparse
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import hashlib
import json
import re

from canonical_qc import ALGORITHM_VERSION, DEFAULT_CONFIG, DEFAULT_CONFIG_VERSION, assess, validate_config
from fit_data import DECODE_OPTIONS
import garmin_activity
import recording


CANONICAL_VERSION = "g1-canonical-v2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_configs (version TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS canonical_source_versions (
    id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_qc_versions (
    id TEXT PRIMARY KEY, source_version_id TEXT NOT NULL REFERENCES canonical_source_versions(id),
    config_version TEXT NOT NULL REFERENCES qc_configs(version), payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_activity_versions (
    id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL,
    config_version TEXT NOT NULL REFERENCES qc_configs(version), payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS canonical_activity_history ON canonical_activity_versions(activity_id);
CREATE TABLE IF NOT EXISTS canonical_activity_heads (
    activity_id INTEGER PRIMARY KEY, version_id TEXT NOT NULL REFERENCES canonical_activity_versions(id)
);
CREATE TABLE IF NOT EXISTS canonical_selections (
    id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL,
    version_id TEXT NOT NULL REFERENCES canonical_activity_versions(id), selected_at TEXT NOT NULL
);
"""
# Historical activity IDs deliberately have no FK to the mutable dedup table:
# explicit merges delete its donor row, but may never delete or rewrite provenance.


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def knowledge_time():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def register_config(db, config_version, config):
    if not isinstance(config_version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", config_version):
        raise ValueError("Use a QC version of 1–80 letters, digits, dots, underscores or hyphens.")
    validate_config(config)
    payload = encoded(config)
    old = db.execute("SELECT payload FROM qc_configs WHERE version=?", (config_version,)).fetchone()
    if old and json.loads(old[0]) != config:
        raise ValueError("That QC version already has different settings. Choose a new version.")
    if config_version == DEFAULT_CONFIG_VERSION and config != DEFAULT_CONFIG:
        raise ValueError("The default QC version is immutable. Choose a new version.")
    db.execute("INSERT OR IGNORE INTO qc_configs VALUES (?, ?)", (config_version, payload))


def source_bytes(archive, item):
    for outer in [None, *(s["sha256"] for s in item["sources"] if s["kind"] != "fit")]:
        try:
            data = garmin_activity.archived_fit(archive, item["fit_sha256"], outer)
            if data is not None:
                return data
        except (OSError, ValueError):
            continue
    return None


def activity_input(db, archive, activity_id):
    row = db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    if not row:
        return None
    aliases = garmin_activity.active_aliases(db, activity_id)[activity_id]
    activity = garmin_activity.running_headline(db, archive, dict(row), aliases)
    activity["sources"] = [dict(r) for r in db.execute(
        "SELECT kind, sha256, format, acquired_at FROM activity_sources WHERE activity_id=? ORDER BY kind, sha256", (activity_id,))]
    ids = [r[0] for r in db.execute("SELECT garmin_id FROM garmin_aliases WHERE activity_id=? ORDER BY garmin_id", (activity_id,))]
    if row["garmin_id"]:
        ids.append(row["garmin_id"])
    activity["provider_ids"] = [{"provider": "garmin", "id": gid} for gid in sorted(set(ids))]
    return activity


def summary(activity):
    result = {key: activity[key] for key in ("sport", "start_utc", "start_local", "local_date", "distance_m",
                                            "elapsed_seconds", "timer_seconds", "moving_seconds")}
    # Garmin's named GMT contract is UTC. FIT clocks were already required to be absolute.
    start = garmin_activity.timestamp(result["start_utc"])
    if start and start.tzinfo:
        start = start.astimezone(timezone.utc)
        result["start_utc"] = start.isoformat()
    else:
        result["start_utc"] = None
    result["end_utc"] = None
    result["end_basis"] = "NOT_PROVIDED"
    if result["start_utc"] and result["elapsed_seconds"] is not None:
        try:
            result["end_utc"] = (start + timedelta(seconds=result["elapsed_seconds"])).isoformat()
            result["end_basis"] = "start_utc_plus_reported_elapsed_seconds"
        except OverflowError:
            pass
    result["metric_sources"] = json.loads(activity["metric_sources"])
    return result


def configuration(db, activity_id, config_version=None, config=None):
    """An omitted configuration preserves the selection; a named one is explicit."""
    if config_version is None and config is None:
        previous = db.execute("""SELECT v.config_version FROM canonical_activity_heads h
            JOIN canonical_activity_versions v ON v.id=h.version_id WHERE h.activity_id=?""", (activity_id,)).fetchone()
        config_version = previous[0] if previous else DEFAULT_CONFIG_VERSION
    config_version = config_version or DEFAULT_CONFIG_VERSION
    if config is None:
        saved = db.execute("SELECT payload FROM qc_configs WHERE version=?", (config_version,)).fetchone()
        config = json.loads(saved[0]) if saved else DEFAULT_CONFIG
    return config_version, dict(config)


def requires_recompute(db, version_id):
    payload = json.loads(db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (version_id,)).fetchone()[0])
    if payload["canonical_algorithm_version"] != CANONICAL_VERSION or payload["qc_algorithm_version"] != ALGORITHM_VERSION:
        return True
    for source in payload["sources"]:
        if source["source_version_id"]:
            saved = json.loads(db.execute("SELECT payload FROM canonical_source_versions WHERE id=?", (source["source_version_id"],)).fetchone()[0])
            if saved["parser"]["normalization_version"] != recording.INSPECTION_VERSION:
                return True
    return False


def persist(db, archive, activity_id, config_version=None, config=None):
    """Called inside Store's serialized write transaction; never opens another handle."""
    config_version, config = configuration(db, activity_id, config_version, config)
    register_config(db, config_version, config)
    activity = activity_input(db, archive, activity_id)
    if activity is None:
        return None
    items = recording.sources_in_db(db, activity)
    parser = {"decoder": "garmin-fit-sdk", "decoder_version": version("garmin-fit-sdk"),
              "decode_options": dict(DECODE_OPTIONS), "normalization_version": recording.INSPECTION_VERSION}
    sources = []
    for item in items:
        sha = item["fit_sha256"]
        source_id = fingerprint({"raw_sha256": sha, "parser": parser})
        data = source_bytes(archive, item)
        source = {**item, "source_version_id": None, "qc_version_id": None, "archive_status": "UNAVAILABLE"}
        if data is not None:
            saved = db.execute("SELECT payload FROM canonical_source_versions WHERE id=?", (source_id,)).fetchone()
            if saved:
                trace = json.loads(saved[0])["trace"]
            else:
                try:
                    trace = recording.inspect(data, 0, 2**63 - 1)
                except (ValueError, KeyError):
                    trace = None
                if trace is not None:
                    # Page size is a response concern, not part of source identity.
                    trace["samples"]["limit"] = trace["samples"]["total"]
                    db.execute("INSERT INTO canonical_source_versions VALUES (?, ?, ?, ?)",
                               (source_id, sha, encoded({"parser": parser, "trace": trace}), knowledge_time()))
            if trace is not None:
                qc_id = fingerprint({"source_version_id": source_id, "config_version": config_version,
                                     "config": config, "algorithm_version": ALGORITHM_VERSION})
                if not db.execute("SELECT 1 FROM canonical_qc_versions WHERE id=?", (qc_id,)).fetchone():
                    qc = assess(trace, sha, config)
                    db.execute("INSERT INTO canonical_qc_versions VALUES (?, ?, ?, ?, ?)",
                               (qc_id, source_id, config_version, encoded(qc), knowledge_time()))
                source.update(source_version_id=source_id, qc_version_id=qc_id, archive_status="VERIFIED")
        sources.append(source)
    # Reuse G0's primary-Garmin selection, otherwise its deterministic owned-checksum
    # order. Do not let a duplicate upload silently choose a different original.
    selected = sources[0] if sources else None
    headline = summary(activity)
    selected_header = None
    if selected and selected["source_version_id"]:
        selected_header = json.loads(db.execute("SELECT payload FROM canonical_source_versions WHERE id=?",
                                               (selected["source_version_id"],)).fetchone()[0])["trace"]["header"]
    comparisons = []
    if selected_header:
        for key in ("distance_m", "elapsed_seconds", "timer_seconds", "moving_seconds"):
            a, b = headline[key], selected_header[key]
            if a is not None and b is not None and a != b:
                tolerance = max(config["summary_mismatch_absolute_m"], config["summary_mismatch_fraction"] * a) if key == "distance_m" else None
                comparisons.append({"field": key, "activity_summary": a, "selected_fit_header": b,
                                    "difference": abs(a - b), "tolerance": tolerance,
                                    "requires_distance_review": tolerance is not None and abs(a - b) > tolerance,
                                    "summary_input_refs": activity["sources"],
                                    "fit_header_ref": {"sha256": selected["fit_sha256"], "message_group": "session_mesgs", "index": 0}})
    payload = {"canonical_activity_id": activity_id, "activity_id_at_creation": activity_id,
               "canonical_algorithm_version": CANONICAL_VERSION, "qc_algorithm_version": ALGORITHM_VERSION,
               "qc_config_version": config_version, "provider_ids": activity["provider_ids"],
               "running_sport": activity["sport"] in garmin_activity.RUN_TYPES,
               "raw_sources": activity["sources"], "summary": headline, "sources": sources,
               "header": selected_header, "summary_comparisons": comparisons,
               "selected_source_version_id": selected["source_version_id"] if selected else None,
               "selected_fit_sha256": selected["fit_sha256"] if selected else None,
               "selection_policy": "primary Garmin original; otherwise owned FIT checksum order (G0)",
               "merged_activity_ids": [r[0] for r in db.execute(
                   "SELECT old_id FROM activity_redirects WHERE activity_id=? ORDER BY old_id", (activity_id,))],
               "human_validation": "UNVERIFIED", "scientific_validation": "UNVERIFIED"}
    version_id = fingerprint(payload)
    db.execute("INSERT OR IGNORE INTO canonical_activity_versions VALUES (?, ?, ?, ?, ?)",
               (version_id, activity_id, config_version, encoded(payload), knowledge_time()))
    changed = db.execute("""INSERT INTO canonical_activity_heads VALUES (?, ?) ON CONFLICT(activity_id)
                  DO UPDATE SET version_id=excluded.version_id WHERE version_id!=excluded.version_id""", (activity_id, version_id)).rowcount
    if changed:
        db.execute("INSERT INTO canonical_selections(activity_id, version_id, selected_at) VALUES (?, ?, ?)",
                   (activity_id, version_id, knowledge_time()))
    # Same serialized import/correction transaction; includes linked-owner refreshes.
    import efforts
    efforts.persist(db, version_id)
    return version_id


def recompute(store, activity_ids=None, *, config_version=None, config=None):
    """Explicit offline backfill: no upstream access, no work on GET or startup."""
    from fit_import import index_originals_in_db

    result = []
    with store.connect(write=True, create=True) as db:
        if config_version is not None or config is not None:
            config_version, config = configuration(db, None, config_version, config)
            register_config(db, config_version, config)
        index_originals_in_db(db, store.archive)
        ids = activity_ids if activity_ids is not None else [r[0] for r in db.execute("SELECT id FROM activities ORDER BY id")]
        for activity_id in ids:
            row = db.execute("SELECT activity_id FROM activity_redirects WHERE old_id=?", (activity_id,)).fetchone()
            owner = row[0] if row else activity_id
            version_id = persist(db, store.archive, owner, config_version, config)
            if version_id is None:
                raise KeyError("Activity not found.")
            result.append({"activity_id": owner, "version_id": version_id})
    return result


class UnknownVersion(LookupError):
    pass


def read(store, activity_id, version_id=None, source=None, *, offset=0, limit=200):
    from storage import local_id

    with store.history() as db:
        requested = local_id(activity_id)
        row = db and db.execute("""SELECT id FROM activities WHERE id=COALESCE(
            (SELECT activity_id FROM activity_redirects WHERE old_id=?), ?)""", (requested, requested)).fetchone()
        if not row:
            return None
        owner = row[0]
        empty = {"activity_id": owner, "state": "NOT_PROCESSED", "versions": [],
                 "message": "Canonical data not processed. Use explicit offline recompute to backfill this activity."}
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_activity_versions'").fetchone():
            return empty
        owners = [owner, *[r[0] for r in db.execute("SELECT old_id FROM activity_redirects WHERE activity_id=?", (owner,))]]
        versions = [dict(r) for r in db.execute("""SELECT id, activity_id AS activity_id_at_creation, config_version, created_at
            FROM canonical_activity_versions WHERE activity_id IN (SELECT value FROM json_each(?)) ORDER BY rowid DESC""",
                                               (encoded(owners),))]
        head = db.execute("SELECT version_id FROM canonical_activity_heads WHERE activity_id=?", (owner,)).fetchone()
        chosen = version_id or (head[0] if head else None)
        if chosen is None:
            return {**empty, "versions": versions}
        if chosen not in {v["id"] for v in versions}:
            raise UnknownVersion("That canonical version does not belong to this activity.")
        payload = json.loads(db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (chosen,)).fetchone()[0])
        config = json.loads(db.execute("SELECT payload FROM qc_configs WHERE version=?", (payload["qc_config_version"],)).fetchone()[0])
        selected = next((s for s in payload["sources"] if s["fit_sha256"] == (source or payload["selected_fit_sha256"])), None)
        if source and selected is None:
            raise UnknownVersion("That recording does not belong to this canonical version.")
        result = {**payload, "activity_id": owner, "version_id": chosen, "current_version_id": head[0] if head else None,
                  "versions": versions, "qc_config": config, "state": "PROCESSED", "trace": None,
                  "inspected_fit_sha256": selected["fit_sha256"] if selected else None,
                  "qc": {"state": "INSUFFICIENT", "timer_event_coverage": "NOT_PROVIDED", "flags": [], "blocks": []},
                  "message": "No verified original trace in this version; valid summary data is retained."}
        result["created_at"] = next(v["created_at"] for v in versions if v["id"] == chosen)
        result["recompute_required"] = requires_recompute(db, chosen)
        result["selection_history"] = [dict(r) for r in db.execute("""SELECT activity_id, version_id, selected_at
            FROM canonical_selections WHERE activity_id IN (SELECT value FROM json_each(?)) ORDER BY id""", (encoded(owners),))]
        result["archive_status_now"] = "VERIFIED" if selected and source_bytes(store.archive, selected) is not None else "UNAVAILABLE"
        if selected and selected["source_version_id"]:
            source_row = db.execute("SELECT payload, created_at FROM canonical_source_versions WHERE id=?",
                                    (selected["source_version_id"],)).fetchone()
            saved = json.loads(source_row[0])
            trace = saved["trace"]
            trace["samples"].update(offset=offset, limit=limit, rows=trace["samples"]["rows"][offset:offset + limit])
            qc = json.loads(db.execute("SELECT payload FROM canonical_qc_versions WHERE id=?", (selected["qc_version_id"],)).fetchone()[0])
            result.update(trace=trace, parser=saved["parser"], qc=qc, message=None, source_created_at=source_row[1])
        result["source_qc_state"] = result["qc"]["state"]
        canonical_source = next((s for s in payload["sources"] if s["fit_sha256"] == payload["selected_fit_sha256"]), None)
        result["canonical_quality_state"] = "INSUFFICIENT"
        if canonical_source and canonical_source["qc_version_id"]:
            result["canonical_quality_state"] = json.loads(db.execute(
                "SELECT payload FROM canonical_qc_versions WHERE id=?", (canonical_source["qc_version_id"],)).fetchone()[0])["state"]
        if (result["canonical_quality_state"] == "VALID" and
                any(c["requires_distance_review"] for c in payload["summary_comparisons"])):
            result["canonical_quality_state"] = "SUSPECT"
        return result


def main():
    from storage import Archive, Store

    parser = argparse.ArgumentParser(description="Offline canonical backfill/recompute; never contacts a provider.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--activity", type=int, action="append")
    parser.add_argument("--config-version", help="Explicit QC configuration; omitted preserves each current selection")
    parser.add_argument("--config", help="JSON file containing all QC settings; use a new version for changed settings")
    args = parser.parse_args()
    try:
        config = None
        if args.config:
            with open(args.config, encoding="utf-8") as saved:
                config = json.load(saved)
        result = recompute(Store(args.database, Archive(args.archive)), args.activity,
                           config_version=args.config_version, config=config)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({"activities": result}, indent=2))


if __name__ == "__main__":
    main()
