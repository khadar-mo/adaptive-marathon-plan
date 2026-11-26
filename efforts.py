"""Immutable G2 extraction, read-only selection, interval inspection and review audit."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import canonical
import effort_math


DEFAULT_CONFIG_VERSION = "g2-engineering-v1"
DEFAULT_CONFIG = {"distances_m": [400, 800, 1000, 1500, 1609.344, 3000, 5000, 10000, 15000, 21097.5, 30000, 42195],
                  "whole_activity_tolerance_m": 0, "recent_days": 112, "comparison_days": [56, 84, 168]}
CODE_VERSION = "g2-extraction-v1"
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes() + Path(effort_math.__file__).read_bytes()).hexdigest()
SCHEMA = """
CREATE TABLE IF NOT EXISTS effort_configs (version TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS effort_versions (
    id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL, canonical_version_id TEXT NOT NULL,
    config_version TEXT NOT NULL REFERENCES effort_configs(version), payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS effort_history ON effort_versions(activity_id);
CREATE TABLE IF NOT EXISTS effort_heads (activity_id INTEGER PRIMARY KEY, version_id TEXT NOT NULL REFERENCES effort_versions(id));
CREATE TABLE IF NOT EXISTS effort_selections (
    id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, version_id TEXT NOT NULL REFERENCES effort_versions(id), selected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effort_inspections (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES effort_versions(id), source_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effort_reviews (
    id INTEGER PRIMARY KEY, version_id TEXT NOT NULL REFERENCES effort_versions(id), candidate_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
    created_at TEXT NOT NULL, previous_review_id INTEGER REFERENCES effort_reviews(id)
);
CREATE TABLE IF NOT EXISTS effort_contexts (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES effort_versions(id), candidate_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL, payload TEXT NOT NULL, known_at TEXT NOT NULL,
    previous_context_id TEXT REFERENCES effort_contexts(id)
);
"""
PROVENANCE_CATEGORIES = {"VERIFIED_TEST", "RACE", "TRAINING_FAST", "FATIGUED_BLOCK", "EASY_OR_UNKNOWN"}


class NotFound(LookupError):
    pass


def register(db, name, config):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise ValueError("Use a configuration version of 1–80 letters, digits, dots, underscores or hyphens.")
    if not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG):
        raise ValueError("Supply all G2 configuration fields and no extra fields.")
    ds, comparisons = config["distances_m"], config["comparison_days"]
    if not isinstance(ds, list) or not 1 <= len(ds) <= 64 or not all(effort_math.positive(d) for d in ds) or len(set(ds)) != len(ds):
        raise ValueError("Supply 1–64 distinct positive finite target distances in metres.")
    tolerance = config["whole_activity_tolerance_m"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Whole-activity tolerance must be finite and nonnegative.")
    if (type(config["recent_days"]) is not int or not 1 <= config["recent_days"] <= 36500 or
            not isinstance(comparisons, list) or len(comparisons) > 10 or
            not all(type(d) is int and 1 <= d <= 36500 for d in comparisons) or len(set(comparisons)) != len(comparisons)):
        raise ValueError("Use integer windows of 1–36500 days, with at most ten distinct comparisons.")
    old = db.execute("SELECT payload FROM effort_configs WHERE version=?", (name,)).fetchone()
    if (old and json.loads(old[0]) != config) or (name == DEFAULT_CONFIG_VERSION and config != DEFAULT_CONFIG):
        raise ValueError("That extraction configuration version is immutable; choose a new version.")
    db.execute("INSERT OR IGNORE INTO effort_configs VALUES (?, ?)", (name, canonical.encoded(config)))


def source_data(db, source):
    saved = db.execute("SELECT payload, created_at FROM canonical_source_versions WHERE id=?", (source["source_version_id"],)).fetchone()
    qc = db.execute("SELECT payload FROM canonical_qc_versions WHERE id=?", (source["qc_version_id"],)).fetchone()
    return json.loads(saved[0]), json.loads(qc[0]), saved[1]


def global_flags(payload, source, trace=None):
    flags = []
    if not payload["running_sport"]:
        flags.append({"code": "unsupported_sport", "effect": "INVALID", "sample_indices": [],
                      "calculation": {"reason": "Canonical activity is not running."}})
    if source["fit_sha256"] == payload["selected_fit_sha256"]:
        for comparison in payload["summary_comparisons"]:
            if comparison["requires_distance_review"]:
                flags.append({"code": "summary_distance_disagreement", "effect": "SUSPECT", "sample_indices": [],
                              "calculation": comparison})
    if trace:
        header = trace["header"]
        for sample in trace["samples"]["rows"]:
            t = sample["timestamp_utc"]
            if t and ((header["start_utc"] and effort_math.stamp(t) < effort_math.stamp(header["start_utc"])) or
                      (header["end_utc"] and effort_math.stamp(t) > effort_math.stamp(header["end_utc"]))):
                boundary = header["start_utc"] if header["start_utc"] and effort_math.stamp(t) < effort_math.stamp(header["start_utc"]) else header["end_utc"]
                flags.append({"code": "sample_outside_recorded_session", "effect": "SUSPECT",
                              "sample_indices": [sample["record_index"]], "sample_times_utc": [t], "scope": "RAW_SAMPLE_SUPPORT",
                              "calculation": {"record_index": sample["record_index"], "timestamp_utc": t,
                                              "header_start_utc": header["start_utc"], "header_end_utc": header["end_utc"],
                                              "end_basis": header.get("end_basis"),
                                              "difference_from_boundary_seconds": (effort_math.stamp(t) - effort_math.stamp(boundary)).total_seconds()}})
    return flags


def candidates(result):
    for row in result["distances"]:
        for name in ("best", "flagged_candidate", "best_whole_activity", "best_internal_segment", "attained_candidate", "unattained_limit"):
            if row.get(name):
                yield row[name]


def identify(candidate, version, sha):
    # Stable geometry/timing identity; QC/code are fixed by the extraction version.
    # Context annotations must not create a second identity for the same interval.
    for flag in candidate["flags"]:
        flag.setdefault("raw_references", [{"sha256": sha, "message_group": "record_mesgs", "index": i}
                                            for i in flag.get("sample_indices", ())])
    candidate["id"] = canonical.fingerprint({"version": version, "sha256": sha,
        "interval": effort_math.interval_key(candidate), "block_index": candidate["block_index"],
        "attainment": candidate["attainment"]})
    return candidate


def persist(db, canonical_id, *, config_version=None, config=None):
    saved = db.execute("SELECT payload, activity_id, created_at FROM canonical_activity_versions WHERE id=?", (canonical_id,)).fetchone()
    payload, aid, canonical_created_at = json.loads(saved[0]), saved[1], saved[2]
    previous = db.execute("SELECT v.config_version FROM effort_heads h JOIN effort_versions v ON v.id=h.version_id WHERE h.activity_id=?", (aid,)).fetchone()
    config_version = config_version or (previous[0] if previous else DEFAULT_CONFIG_VERSION)
    if config is None:
        old = db.execute("SELECT payload FROM effort_configs WHERE version=?", (config_version,)).fetchone()
        config = json.loads(old[0]) if old else DEFAULT_CONFIG
    register(db, config_version, config)
    qc_config = json.loads(db.execute("SELECT payload FROM qc_configs WHERE version=?", (payload["qc_config_version"],)).fetchone()[0])
    identity = {"canonical_version_id": canonical_id, "config_version": config_version, "config": config,
                "code_version": CODE_VERSION, "code_sha256": CODE_SHA256, "algorithm_version": effort_math.ALGORITHM_VERSION}
    version_id = canonical.fingerprint(identity)
    if not db.execute("SELECT 1 FROM effort_versions WHERE id=?", (version_id,)).fetchone():
        sources = []
        for source in payload["sources"]:
            entry = {**source, "result": {"distances": [], "unusable_blocks": []}}
            if source["source_version_id"]:
                data, qc, created_at = source_data(db, source)
                result = effort_math.extract(data["trace"], qc, qc_config, config["distances_m"],
                                            whole_tolerance=config["whole_activity_tolerance_m"], global_flags=global_flags(payload, source, data["trace"]))
                for c in candidates(result):
                    identify(c, version_id, source["fit_sha256"])
                entry.update(result=result, header=data["trace"]["header"], qc=qc, parser=data["parser"],
                             source_created_at=created_at, original_sample_count=data["trace"]["samples"]["total"])
                entry["last_sample_utc"] = max((s["timestamp_utc"] for s in data["trace"]["samples"]["rows"] if s["timestamp_utc"]), default=None)
            sources.append(entry)
        result = {**identity, "version_id": version_id, "activity_id_at_creation": aid,
                  "canonical_created_at": canonical_created_at, "selected_fit_sha256": payload["selected_fit_sha256"],
                  "selected_source_version_id": payload["selected_source_version_id"], "running_sport": payload["running_sport"],
                  "raw_sources": payload["raw_sources"], "provider_ids": payload["provider_ids"], "sources": sources,
                  "summary": payload["summary"], "qc_config_version": payload["qc_config_version"], "qc_config": qc_config,
                  "precision_convention": effort_math.PRECISION, "human_validation": "UNVERIFIED", "scientific_validation": "UNVERIFIED"}
        db.execute("INSERT INTO effort_versions VALUES (?, ?, ?, ?, ?, ?)",
                   (version_id, aid, canonical_id, config_version, canonical.encoded(result), canonical.knowledge_time()))
    changed = db.execute("""INSERT INTO effort_heads VALUES (?, ?) ON CONFLICT(activity_id)
        DO UPDATE SET version_id=excluded.version_id WHERE version_id!=excluded.version_id""", (aid, version_id)).rowcount
    if changed:
        db.execute("INSERT INTO effort_selections(activity_id, version_id, selected_at) VALUES (?, ?, ?)",
                   (aid, version_id, canonical.knowledge_time()))
    return version_id


def owner(db, requested):
    from storage import local_id
    requested = local_id(str(requested))
    row = db and db.execute("""SELECT id FROM activities WHERE id=COALESCE(
            (SELECT activity_id FROM activity_redirects WHERE old_id=?), ?)""", (requested, requested)).fetchone()
    if not row:
        raise NotFound("Activity not found.")
    return row[0]


def load(db, requested, version=None):
    aid = owner(db, requested)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='effort_versions'").fetchone():
        return {"activity_id": aid, "state": "NOT_PROCESSED", "versions": []}
    owners = [aid, *[r[0] for r in db.execute("SELECT old_id FROM activity_redirects WHERE activity_id=?", (aid,))]]
    versions = [dict(r) for r in db.execute("""SELECT id, config_version, canonical_version_id, created_at, activity_id AS activity_id_at_creation
        FROM effort_versions WHERE activity_id IN (SELECT value FROM json_each(?)) ORDER BY rowid DESC""", (canonical.encoded(owners),))]
    head = db.execute("SELECT version_id FROM effort_heads WHERE activity_id=?", (aid,)).fetchone()
    chosen = version or (head[0] if head else None)
    if chosen is None:
        return {"activity_id": aid, "state": "NOT_PROCESSED", "versions": versions}
    if chosen not in {v["id"] for v in versions}:
        raise NotFound("That extraction version does not belong to this activity.")
    row = db.execute("SELECT payload, created_at FROM effort_versions WHERE id=?", (chosen,)).fetchone()
    result = json.loads(row[0])
    result["recompute_required"] = canonical.requires_recompute(db, result["canonical_version_id"])
    result.update(activity_id=aid, state="PROCESSED", versions=versions, current_version_id=head[0] if head else None,
                  created_at=row[1], selection_history=[dict(r) for r in db.execute("""SELECT activity_id, version_id, selected_at FROM effort_selections
                      WHERE activity_id IN (SELECT value FROM json_each(?)) ORDER BY id""", (canonical.encoded(owners),))])
    return result


def reviewed(db, c, version):
    rows = [dict(r) for r in db.execute("SELECT * FROM effort_reviews WHERE version_id=? AND candidate_id=? ORDER BY id", (version, c["id"]))]
    decision = next((r for r in reversed(rows) if r["action"] != "annotate"), None)
    c["reviews"] = rows
    c["review_verdict"] = decision["action"] if decision else "UNREVIEWED"
    c["effective_quality_state"] = "INVALID" if decision and decision["action"] == "reject" else c["computed_quality_state"]
    contexts = [{**json.loads(r["payload"]), "id": r["id"], "known_at": r["known_at"], "previous_context_id": r["previous_context_id"]}
                for r in db.execute("SELECT * FROM effort_contexts WHERE version_id=? AND candidate_id=? ORDER BY rowid", (version, c["id"]))]
    c["context_history"] = contexts
    c["computed_provenance"] = c.get("computed_provenance", c["provenance"])
    if contexts:
        latest = contexts[-1]
        c["provenance"] = {**c["computed_provenance"], "category": latest["category"], "tags": latest["tags"],
                           "user_correction": latest["id"], "known_at": latest["known_at"], "protocol": latest["protocol"],
                           "verified_maximal": latest["maximal_confirmed"], "declaration_basis": "EXPLICIT_USER_ATTESTATION",
                           "scientific_validation": "UNVERIFIED"}
    return c


def select_source(result, source=None):
    if result["state"] != "PROCESSED":
        raise NotFound("No persisted extraction for this activity.")
    sha = source or result["selected_fit_sha256"]
    selected = next((s for s in result["sources"] if s["fit_sha256"] == sha), None)
    if source and selected is None:
        raise NotFound("That source does not belong to this extraction version.")
    return selected


def read(store, activity_id, version=None, source=None):
    with store.history() as db:
        result = load(db, activity_id, version)
        if result["state"] != "PROCESSED":
            return result
        selected = select_source(result, source)
        result["inspected_source"] = selected
        result["archive_status_now"] = "VERIFIED" if selected and canonical.source_bytes(store.archive, selected) is not None else "UNAVAILABLE"
        if selected:
            for c in candidates(selected["result"]):
                reviewed(db, c, result["version_id"])
        return result


def find_candidate(db, result, candidate_id):
    if result["state"] != "PROCESSED":
        raise NotFound("No persisted extraction for this activity.")
    for source in result["sources"]:
        for c in candidates(source["result"]):
            if c["id"] == candidate_id:
                return c, source
    row = db.execute("SELECT payload, source_sha256 FROM effort_inspections WHERE id=? AND version_id=?", (candidate_id, result["version_id"])).fetchone()
    if row:
        return json.loads(row[0]), select_source(result, row[1])
    raise NotFound("That effort does not belong to this extraction version.")


def inspect(store, activity_id, version, candidate_id, *, offset=0, limit=200):
    with store.history() as db:
        result = load(db, activity_id, version)
        if result["state"] != "PROCESSED":
            raise NotFound("No persisted extraction for this activity.")
        c, source = find_candidate(db, result, candidate_id)
        reviewed(db, c, version)
        data, _, _ = source_data(db, source)
        first, last = c["audit"]["sample_range"]
        excerpt = data["trace"]["samples"]["rows"][max(0, first - 1):last + 2]
        return {"activity_id": result["activity_id"], "version_id": version, "candidate": c, "source": source,
                "recompute_required": result["recompute_required"],
                "canonical_version_id": result["canonical_version_id"], "qc_config": result["qc_config"],
                "qc_config_version": result["qc_config_version"], "config_version": result["config_version"],
                "algorithm_version": result["algorithm_version"], "code_sha256": result["code_sha256"],
                "created_at": result["created_at"], "raw_sources": result["raw_sources"],
                "archive_status_now": "VERIFIED" if canonical.source_bytes(store.archive, source) is not None else "UNAVAILABLE",
                "precision_convention": result["precision_convention"],
                "samples": {"total": len(excerpt), "offset": offset, "limit": limit, "rows": excerpt[offset:offset + limit]},
                "events": data["trace"]["events"]}


def save_inspection(store, activity_id, version, source, start_distance_m, distance_m, start_coordinate_m=None):
    if not effort_math.positive(distance_m) or type(start_distance_m) not in (int, float) or not math.isfinite(start_distance_m) or start_distance_m < 0:
        raise ValueError("Use a finite nonnegative cumulative start and positive target distance in metres.")
    try:
        start = effort_math.coordinate(start_coordinate_m if start_coordinate_m is not None else start_distance_m)
    except (ValueError, ZeroDivisionError):
        raise ValueError("Use a valid rational start coordinate.") from None
    if start < 0 or float(start) != start_distance_m:
        raise ValueError("The exact start coordinate must match the displayed cumulative metres.")
    with store.connect(write=True, create=True) as db:
        result = load(db, activity_id, version)
        selected = select_source(result, source)
        if result["recompute_required"] or result["code_sha256"] != CODE_SHA256:
            raise ValueError("This extraction used another code version. Inspect its saved candidates or explicitly recompute before selecting a new interval.")
        if not selected or not selected["source_version_id"]:
            raise ValueError("No timestamped trace is available for this source.")
        data, qc, _ = source_data(db, selected)
        payload = json.loads(db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (result["canonical_version_id"],)).fetchone()[0])
        blocks, _ = effort_math.prepare(data["trace"], qc, result["qc_config"], global_flags(payload, selected, data["trace"]))
        for block in blocks:
            if block.coordinates[0] <= start and start + effort_math.coordinate(distance_m) <= block.coordinates[-1]:
                c = block.candidate(start, distance_m)
                if c:
                    for saved in candidates(selected["result"]):
                        if (saved["attainment"] == "ATTAINED" and saved["kind"] == "CONTINUOUS_SEGMENT" and
                                effort_math.interval_key(saved) == effort_math.interval_key(c)):
                            return saved
                    c["provenance"]["category"] = "EASY_OR_UNKNOWN"
                    block.audit(c)
                    identify(c, version, selected["fit_sha256"])
                    db.execute("INSERT OR IGNORE INTO effort_inspections VALUES (?, ?, ?, ?, ?)",
                               (c["id"], version, selected["fit_sha256"], canonical.encoded(c), canonical.knowledge_time()))
                    return c
        raise ValueError("The requested interval is not contained in a single usable running block.")


def review(store, activity_id, version, candidate_id, action, actor, note):
    if action not in {"accept", "reject", "annotate"} or not actor.strip() or not note.strip() or len(actor) > 120 or len(note) > 4000:
        raise ValueError("Supply an accept/reject/annotate action, actor (1–120 characters) and note (1–4000 characters).")
    with store.connect(write=True, create=True) as db:
        result = load(db, activity_id, version)
        c, source = find_candidate(db, result, candidate_id)
        previous = db.execute("SELECT id FROM effort_reviews WHERE version_id=? AND candidate_id=? ORDER BY id DESC LIMIT 1", (version, candidate_id)).fetchone()
        db.execute("INSERT INTO effort_reviews(version_id, candidate_id, source_sha256, action, actor, note, created_at, previous_review_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (version, candidate_id, source["fit_sha256"], action, actor.strip(), note.strip(), canonical.knowledge_time(), previous[0] if previous else None))
        return reviewed(db, c, version)


def context(store, activity_id, version, candidate_id, *, category, tags, actor, note, protocol=None, maximal_confirmed=False):
    if (category not in PROVENANCE_CATEGORIES or not isinstance(tags, list) or len(tags) > 20 or
            any(not isinstance(tag, str) or not tag.strip() or len(tag) > 80 for tag in tags) or
            not actor.strip() or len(actor) > 120 or not note.strip() or len(note) > 4000 or
            (protocol is not None and (not isinstance(protocol, str) or len(protocol) > 4000)) or
            type(maximal_confirmed) is not bool):
        raise ValueError("Supply an effort category, up to twenty short tags, actor and context note.")
    if maximal_confirmed and (category != "VERIFIED_TEST" or not protocol or not protocol.strip()):
        raise ValueError("Explicit maximal confirmation requires VERIFIED_TEST and a recorded benchmark protocol.")
    with store.connect(write=True, create=True) as db:
        result = load(db, activity_id, version)
        c, source = find_candidate(db, result, candidate_id)
        previous = db.execute("SELECT id FROM effort_contexts WHERE version_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1", (version, candidate_id)).fetchone()
        payload = {"category": category, "tags": list(dict.fromkeys(t.strip() for t in tags)), "actor": actor.strip(),
                   "note": note.strip(), "protocol": protocol, "maximal_confirmed": maximal_confirmed}
        known_at = canonical.knowledge_time()
        cid = canonical.fingerprint({"version": version, "candidate": candidate_id, "source": source["fit_sha256"],
                                     "payload": payload, "known_at": known_at, "previous": previous[0] if previous else None})
        db.execute("INSERT INTO effort_contexts VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (cid, version, candidate_id, source["fit_sha256"], canonical.encoded(payload), known_at, previous[0] if previous else None))
        return reviewed(db, c, version)


def rejected_intervals(db, result, source):
    decisions = {}
    for row in db.execute("SELECT candidate_id, action FROM effort_reviews WHERE version_id=? AND source_sha256=? ORDER BY id",
                          (result["version_id"], source["fit_sha256"])):
        if row["action"] != "annotate":
            decisions[row["candidate_id"]] = row["action"]
    rejected = set()
    for cid, action in decisions.items():
        if action == "reject":
            c, _ = find_candidate(db, result, cid)
            rejected.add(effort_math.interval_key(c))
    return rejected


def recompute(store, activity_ids=None, *, config_version=None, config=None):
    from fit_import import index_originals_in_db
    # Canonical backfill, extraction/configuration and all selections belong to
    # one requested batch. A later extraction failure must roll back earlier rows.
    results = []
    with store.connect(write=True, create=True) as db:
        if config_version is not None or config is not None:
            config_version = config_version or DEFAULT_CONFIG_VERSION
            if config is None:
                saved = db.execute("SELECT payload FROM effort_configs WHERE version=?", (config_version,)).fetchone()
                config = json.loads(saved[0]) if saved else DEFAULT_CONFIG
            register(db, config_version, config)
        ids = activity_ids if activity_ids is not None else [r[0] for r in db.execute("SELECT id FROM activities ORDER BY id")]
        owners = [owner(db, requested) for requested in ids]
        if any(not db.execute("SELECT 1 FROM canonical_activity_heads WHERE activity_id=?", (aid,)).fetchone() for aid in owners):
            index_originals_in_db(db, store.archive)
        for aid in owners:
            head = db.execute("SELECT version_id FROM canonical_activity_heads WHERE activity_id=?", (aid,)).fetchone()
            canonical_id = (head[0] if head and not canonical.requires_recompute(db, head[0])
                            else canonical.persist(db, store.archive, aid))
            vid = persist(db, canonical_id, config_version=config_version, config=config)
            results.append({"activity_id": aid, "version_id": vid})
    return results


def bests(store, cutoff=None, days=None):
    cutoff_dt = effort_math.stamp(cutoff) if cutoff else datetime.now(timezone.utc)
    cutoff = cutoff_dt.astimezone(timezone.utc).isoformat()
    if days is not None and (type(days) is not int or not 1 <= days <= 36500):
        raise ValueError("Use a recent window of 1–36500 days.")
    report = {"cutoff_utc": cutoff, "time_basis": "EVENT_TIME_RECONSTRUCTION", "knowledge_time_frozen": False,
              "human_validation": "UNVERIFIED", "scientific_validation": "UNVERIFIED",
              "current_capability": "NOT_ESTIMATED", "distances": [], "excluded": [], "inputs": [],
              "eligibility": "Segment end inclusively [T−W,T]; entire source activity end ≤ T. Active selected source only."}
    report["evaluation_code_sha256"] = CODE_SHA256
    report["requested_recent_days"] = days or DEFAULT_CONFIG["recent_days"]
    pools = {}
    with store.history() as db:
        if not db or not db.execute("SELECT 1 FROM sqlite_master WHERE name='effort_versions'").fetchone():
            return report
        activities = [(r[0], load(db, r[0])) for r in db.execute("SELECT id FROM activities ORDER BY id").fetchall()]
        windows = {days or DEFAULT_CONFIG["recent_days"], *DEFAULT_CONFIG["comparison_days"]}
        for _, result in activities:
            if result["state"] == "PROCESSED":
                windows.update([result["config"]["recent_days"], *result["config"]["comparison_days"]])
        windows = sorted(windows)
        for aid, result in activities:
            row = (aid,)
            if result["state"] != "PROCESSED":
                report["excluded"].append({"activity_id": row[0], "reason": "NOT_PROCESSED"})
                continue
            source = select_source(result)
            report["inputs"].append({"activity_id": row[0], "version_id": result["version_id"],
                                     "canonical_version_id": result["canonical_version_id"], "created_at": result["created_at"],
                                     "raw_sources": result["raw_sources"], "selection_history": result["selection_history"]})
            for distance in result["config"]["distances_m"]:
                item = pools.setdefault(distance, {"all_time": [], "whole": [], "internal": [], "flagged": [], "limits": [], "recent": {}, "recent_limits": {}})
                for window in windows:
                    item["recent"].setdefault(window, [])
                    item["recent_limits"].setdefault(window, [])
            if result["recompute_required"]:
                report["excluded"].append({"activity_id": aid, "reason": "SOURCE_NORMALIZATION_REBUILD_REQUIRED",
                                          "version_id": result["version_id"]})
                continue
            if not source or not source.get("header") or not source["header"]["end_utc"]:
                report["excluded"].append({"activity_id": row[0], "reason": "MISSING_SOURCE_ACTIVITY_END"})
                continue
            end = max(effort_math.stamp(t) for t in (source["header"]["end_utc"], source.get("last_sample_utc")) if t)
            if end > cutoff_dt or not result["running_sport"] or canonical.source_bytes(store.archive, source) is None:
                report["excluded"].append({"activity_id": row[0], "reason": "FUTURE_ACTIVITY" if end > cutoff_dt else "NONRUNNING_OR_ARCHIVE_UNAVAILABLE"})
                continue
            rejections = rejected_intervals(db, result, source)
            if rejections and result["code_sha256"] != CODE_SHA256:
                report["excluded"].append({"activity_id": aid, "reason": "REVIEW_SELECTION_REBUILD_REQUIRED", "version_id": result["version_id"]})
                continue
            selected_result = source["result"]
            if rejections:
                data, qc, _ = source_data(db, source)
                canonical_payload = json.loads(db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (result["canonical_version_id"],)).fetchone()[0])
                selected_result = effort_math.extract(data["trace"], qc, result["qc_config"], result["config"]["distances_m"],
                    whole_tolerance=result["config"]["whole_activity_tolerance_m"], global_flags=global_flags(canonical_payload, source, data["trace"]), rejected=rejections)
            activity_limits = {}
            for r in selected_result["distances"]:
                if r.get("unattained_limit"):
                    diagnostic = {"activity_id": aid, "distance_m": r["distance_m"], "state": r["state"],
                                  "version_id": result["version_id"], "source_sha256": source["fit_sha256"],
                                  "reason": "REJECTED_OPTIMUM_NOT_REPLACED" if r["state"] == "REVIEW_REJECTION_LIMIT" else "QC_INFIMUM_NOT_ATTAINED",
                                  "limit": r["unattained_limit"], "attained_candidate": r["attained_candidate"]}
                    activity_limits[r["distance_m"]] = diagnostic
                    report["excluded"].append(diagnostic)
                    pools[r["distance_m"]]["limits"].append(diagnostic)

            def contribute(rows, window=None):
                for r in rows:
                    pool = pools[r["distance_m"]]
                    for field, bucket in (("best", "all_time"), ("best_whole_activity", "whole"), ("best_internal_segment", "internal"), ("flagged_candidate", "flagged")):
                        c = r.get(field)
                        if c is None or effort_math.stamp(c["end_utc"]) > cutoff_dt:
                            continue
                        if "id" not in c:
                            identify(c, result["version_id"], source["fit_sha256"])
                        reviewed(db, c, result["version_id"])
                        # A reviewer rejection remains visible, never an authoritative PB.
                        if field != "flagged_candidate" and c["effective_quality_state"] != "VALID":
                            continue
                        entry = {**c, "activity_id": row[0], "version_id": result["version_id"], "source_sha256": source["fit_sha256"],
                                 "canonical_version_id": result["canonical_version_id"], "activity_end_utc": source["header"]["end_utc"],
                                 "selection_window_days": window,
                                 "inspection_required": c["id"] not in {saved["id"] for saved in candidates(source["result"])}}
                        if window is None:
                            pool[bucket].append(entry)
                        elif field == "best" and effort_math.stamp(c["end_utc"]) >= cutoff_dt - timedelta(days=window):
                            pool["recent"][window].append(entry)
            contribute(selected_result["distances"])
            for r in selected_result["distances"]:
                if r.get("attained_candidate"):
                    contribute([{"distance_m": r["distance_m"], "best": r["attained_candidate"]}])
            for window in windows:
                lower = cutoff_dt - timedelta(days=window)
                if end < lower:
                    continue
                start = source["header"].get("start_utc")
                if start and effort_math.stamp(start) >= lower:
                    contribute(selected_result["distances"], window)
                    for target, diagnostic in activity_limits.items():
                        pools[target]["recent_limits"][window].append(diagnostic)
                        if diagnostic["attained_candidate"]:
                            contribute([{"distance_m": target, "best": diagnostic["attained_candidate"]}], window)
                else:
                    if result["code_sha256"] != CODE_SHA256:
                        report["excluded"].append({"activity_id": aid, "window_days": window, "reason": "BOUNDARY_REBUILD_REQUIRED",
                                                  "version_id": result["version_id"]})
                        continue
                    data, qc, _ = source_data(db, source)
                    canonical_payload = json.loads(db.execute("SELECT payload FROM canonical_activity_versions WHERE id=?", (result["canonical_version_id"],)).fetchone()[0])
                    bounded = effort_math.extract(data["trace"], qc, result["qc_config"], result["config"]["distances_m"],
                        whole_tolerance=result["config"]["whole_activity_tolerance_m"], global_flags=global_flags(canonical_payload, source, data["trace"]), min_end=lower.isoformat(), rejected=rejections)
                    contribute(bounded["distances"], window)
                    for r in bounded["distances"]:
                        if r["state"] == "REVIEW_RECENCY_LIMIT":
                            if r["attained_candidate"]:
                                contribute([{"distance_m": r["distance_m"], "best": r["attained_candidate"]}], window)
                            diagnostic = {"activity_id": row[0], "distance_m": r["distance_m"], "window_days": window,
                                          "version_id": result["version_id"], "source_sha256": source["fit_sha256"],
                                          "reason": "RECENCY_INFIMUM_NOT_ATTAINED", "limit": r["unattained_limit"],
                                          "attained_candidate": r["attained_candidate"]}
                            report["excluded"].append(diagnostic)
                            pools[r["distance_m"]]["recent_limits"][window].append(diagnostic)
    def choose(rows):
        return min(rows, key=lambda c: (round(c["elapsed_seconds"], 9), c["activity_id"], c["start_utc"], c["id"])) if rows else None
    for distance, pool in sorted(pools.items()):
        best = choose(pool["all_time"])
        blocking_all = [d for d in pool["limits"] if best is None or round(d["limit"]["elapsed_seconds"], 9) < round(best["elapsed_seconds"], 9)]
        internal = choose(pool["internal"])
        blocking_internal = [d for d in pool["limits"] if internal is None or
                             round(d["limit"]["elapsed_seconds"], 9) < round(internal["elapsed_seconds"], 9)]
        recent = []
        for window, values in sorted(pool["recent"].items()):
            winner = choose(values)
            diagnostics = pool["recent_limits"][window]
            blocking = [d for d in diagnostics if winner is None or
                        round(d["limit"]["elapsed_seconds"], 9) < round(winner["elapsed_seconds"], 9)]
            recent.append({"days": window, "state": "REVIEW_RECENCY_LIMIT" if blocking else "VALID" if winner else "MISSING_RECENT",
                           "best": None if blocking else winner, "attained_candidate": winner if blocking else None,
                           "unattained_limits": diagnostics, "lower_inclusive_utc": (cutoff_dt - timedelta(days=window)).isoformat()})
        limit_state = "REVIEW_ELIGIBILITY_LIMIT" if any(d["state"] == "REVIEW_ELIGIBILITY_LIMIT" for d in blocking_all) else "REVIEW_REJECTION_LIMIT"
        report["distances"].append({"distance_m": distance, "state": limit_state if blocking_all else "VALID" if best else "NOT_CALCULABLE", "all_time": None if blocking_all else best,
            "attained_candidate": best if blocking_all else None, "unattained_limits": pool["limits"],
            "best_whole_activity": choose(pool["whole"]), "best_internal_segment": None if blocking_internal else internal,
            "internal_attained_candidate": internal if blocking_internal else None,
            "internal_unattained_limits": blocking_internal,
            "flagged_candidate": choose(pool["flagged"]), "recent": recent})
    report["selection_id"] = canonical.fingerprint(report)
    return report


def main():
    from storage import Archive, Store
    parser = argparse.ArgumentParser(description="Offline exact continuous-effort extraction from persisted canonical traces.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--activity", type=int, action="append")
    parser.add_argument("--config-version", help="Explicit extraction configuration; omitted preserves each current selection")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text()) if args.config else None
        print(json.dumps({"activities": recompute(Store(args.database, Archive(args.archive)), args.activity,
                                                  config_version=args.config_version, config=config)}, indent=2))
    except (ValueError, NotFound) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
