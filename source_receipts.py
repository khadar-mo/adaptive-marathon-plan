"""Append-only source observations written inside the importer's transaction."""
import json

import workload_inputs as inputs


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_history_epoch (
    version INTEGER PRIMARY KEY, known_at TEXT NOT NULL, last_legacy_version INTEGER NOT NULL,
    last_legacy_selection INTEGER NOT NULL, last_legacy_upload INTEGER NOT NULL, last_legacy_trace INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_acquisitions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL, format TEXT NOT NULL,
    known_at TEXT NOT NULL, evidence TEXT NOT NULL, UNIQUE(kind,sha256,format)
);
CREATE TABLE IF NOT EXISTS source_group_history (
    sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL,
    previous_id TEXT, known_at TEXT NOT NULL, state_hash TEXT NOT NULL, payload TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_groups_by_owner ON source_group_history(owner_id,sequence);
CREATE TABLE IF NOT EXISTS source_identity_reviews (
    id TEXT PRIMARY KEY, issue_id INTEGER NOT NULL, review_key TEXT NOT NULL, action TEXT NOT NULL,
    known_at TEXT NOT NULL, evidence TEXT NOT NULL, UNIQUE(issue_id,review_key,action)
);
"""


def record_sources(db, sources, basis, *, import_id=None, provider_id=None, ordinal=None):
    known_at = inputs.utc(inputs.now())
    for kind, sha, fmt in sources:
        evidence = {"basis": basis, "known_at": known_at, "import_id": import_id, "provider_id": provider_id,
                    "upload_ordinal": ordinal}
        identity = inputs.digest({"kind": kind, "sha256": sha, "format": fmt, **evidence})
        db.execute("INSERT OR IGNORE INTO source_acquisitions VALUES(?,?,?,?,?,?)",
                   (identity, kind, sha, fmt, known_at, inputs.encoded(evidence)))


def group(owner, members, providers, sources, start, *, recordings=()):
    values = {(s["kind"], s["sha256"], s["format"]): s.get("acquired_at") for s in sources}
    for sha in recordings:
        values.setdefault(("fit", sha, "fit"), None)
    return {"owner_id": owner, "member_ids": sorted(set([owner, *members])), "provider_ids": providers,
            "claimed_event_start_utc": start,
            "sources": [{"kind": k, "sha256": sha, "format": fmt, "claimed_acquired_at": value}
                        for (k, sha, fmt), value in sorted(values.items())]}


def observe_group(db, owner, known_at, cause):
    row = db.execute("SELECT garmin_id,start_utc FROM activities WHERE id=?", (owner,)).fetchone()
    if row is None:
        return  # Merged donors are included in the retained owner's group.
    provider_ids = {r[0] for r in db.execute("SELECT garmin_id FROM garmin_aliases WHERE activity_id=?", (owner,))}
    if row["garmin_id"]:
        provider_ids.add(row["garmin_id"])
    members = [r[0] for r in db.execute("SELECT old_id FROM activity_redirects WHERE activity_id=?", (owner,))]
    sources = [dict(r) for r in db.execute("SELECT * FROM activity_sources WHERE activity_id=?", (owner,))]
    recordings = [r[0] for r in db.execute("""SELECT sha256 FROM fit_sources WHERE activity_id=?
        UNION SELECT m.fit_sha FROM original_members m JOIN activity_sources s ON s.sha256=m.original_sha
        WHERE s.activity_id=? AND s.kind IN ('original','original_unavailable') AND m.fit_sha IS NOT NULL""",
                                             (owner, owner))]
    payload = group(owner, members, [{"provider": "garmin", "id": g} for g in sorted(provider_ids)], sources,
                    row["start_utc"], recordings=recordings)
    state_hash = inputs.digest(payload)
    last = db.execute("SELECT id,state_hash FROM source_group_history WHERE owner_id=? ORDER BY sequence DESC LIMIT 1",
                      (owner,)).fetchone()
    if last and last["state_hash"] == state_hash:
        return
    previous = last["id"] if last else None
    identity = inputs.digest({"state": state_hash, "cause": cause, "known_at": known_at, "previous": previous})
    db.execute("""INSERT INTO source_group_history(id,owner_id,previous_id,known_at,state_hash,payload,evidence)
                  VALUES(?,?,?,?,?,?,?)""", (identity, owner, previous, known_at, state_hash, inputs.encoded(payload),
                                               inputs.encoded(cause)))


def observe_reviews(db, known_at, basis, triggering_import):
    # One decision can settle several reports. A merge may later rewrite mutable
    # candidate or target IDs; the original pending/resolved observations stay intact.
    rows = db.execute("""SELECT r.issue_id,i.import_id,f.* FROM fit_review_issues r
        JOIN import_issues i ON i.id=r.issue_id JOIN fit_reviews f ON f.key=r.review_key
        WHERE NOT EXISTS (SELECT 1 FROM source_identity_reviews s WHERE s.issue_id=r.issue_id
            AND s.review_key=f.key AND s.action=COALESCE(f.resolution,'PENDING'))
        ORDER BY r.issue_id""").fetchall()
    ids = []
    for row in rows:
        payload = json.loads(row["payload"])
        action = row["resolution"] or "PENDING"
        refs = sorted({(k, sha, fmt) for k, sha, fmt in payload.get("sources", [])})
        if payload.get("fit"):
            refs = sorted(set(refs) | {("fit", payload["fit"]["sha256"], "fit")})
        evidence = {"basis": basis, "action": action, "kind": row["kind"], "known_at": known_at,
                    "issue_id": row["issue_id"], "import_id": row["import_id"], "trigger_import_id": triggering_import,
                    "review_key": row["key"], "provider_id": payload.get("fields", {}).get("garmin_id"),
                    "owner_id": row["activity_id"], "candidates_at_observation": sorted(json.loads(row["candidates"])),
                    "local_date": row["local_date"],
                    "sources": [{"kind": k, "sha256": sha, "format": fmt} for k, sha, fmt in refs]}
        rid = inputs.digest(evidence)
        db.execute("INSERT INTO source_identity_reviews VALUES (?,?,?,?,?,?)",
                   (rid, row["issue_id"], row["key"], action, known_at, inputs.encoded(evidence)))
        ids.append(rid)
    return ids


def finish(db, owners, import_id, basis, *, review_issue=None):
    known = inputs.now()
    reviews = observe_reviews(db, known, basis, import_id)
    cause = {"basis": basis, "import_id": import_id, "issue_id": review_issue, "review_event_ids": reviews}
    for owner in sorted(set(n for n in owners if n is not None)):
        observe_group(db, owner, known, cause)


def initialize(db):
    if db.execute("SELECT 1 FROM source_history_epoch WHERE version=1").fetchone():
        return
    known = inputs.now()
    # Only legacy row sets lack normal import receipts. Never backdate current
    # mutable rows to their first reported acquisition or event date.
    db.execute("""INSERT INTO source_history_epoch VALUES (1,?,
        (SELECT COALESCE(MAX(rowid),0) FROM canonical_activity_versions),
        (SELECT COALESCE(MAX(id),0) FROM canonical_selections),
        (SELECT COALESCE(MAX(rowid),0) FROM fit_uploads),
        (SELECT COALESCE(MAX(rowid),0) FROM canonical_source_versions))""", (known,))
    basis = "LEGACY_STATE_OBSERVED"
    observe_reviews(db, known, basis, None)
    for row in db.execute("SELECT id FROM activities ORDER BY id"):
        observe_group(db, row[0], known, {"basis": basis, "import_id": None, "issue_id": None, "review_event_ids": []})
