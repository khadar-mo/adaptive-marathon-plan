"""Reconcile one incoming run in the same transaction as its evidence, laps and report."""
from datetime import datetime, timedelta
import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS fit_sources (
    sha256 TEXT PRIMARY KEY, identity TEXT, activity_id INTEGER NOT NULL REFERENCES activities(id)
);
CREATE INDEX IF NOT EXISTS fit_identity ON fit_sources(identity);
CREATE TABLE IF NOT EXISTS original_members (
    original_sha TEXT PRIMARY KEY, fit_sha TEXT, identity TEXT
);
CREATE TABLE IF NOT EXISTS garmin_aliases (
    garmin_id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL REFERENCES activities(id)
);
CREATE TABLE IF NOT EXISTS activity_redirects (
    old_id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL REFERENCES activities(id)
);
CREATE TABLE IF NOT EXISTS laps (
    activity_id INTEGER NOT NULL REFERENCES activities(id), sequence INTEGER NOT NULL,
    distance_m REAL, timer_seconds REAL, elapsed_seconds REAL, moving_seconds REAL,
    PRIMARY KEY (activity_id, sequence)
);
CREATE TABLE IF NOT EXISTS fit_uploads (
    import_id INTEGER NOT NULL REFERENCES imports(id), ordinal INTEGER NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT, activity_id INTEGER REFERENCES activities(id),
    PRIMARY KEY (import_id, ordinal)
);
CREATE TABLE IF NOT EXISTS fit_reviews (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, candidates TEXT NOT NULL,
    resolution TEXT, activity_id INTEGER REFERENCES activities(id), local_date TEXT
);
CREATE TABLE IF NOT EXISTS fit_review_issues (
    issue_id INTEGER PRIMARY KEY REFERENCES import_issues(id), review_key TEXT NOT NULL REFERENCES fit_reviews(key)
);
"""


OPEN_ISSUES = """SELECT 1 FROM import_issues i LEFT JOIN fit_review_issues r ON r.issue_id=i.id
                 LEFT JOIN fit_reviews f ON f.key=r.review_key WHERE i.import_id=imports.id AND
                 (i.severity='error' OR (i.severity='review' AND f.resolution IS NULL))"""


class ResolutionError(ValueError):
    pass


def remember_issue(db, import_id, key, kind, item, payload, candidates):
    db.execute("""INSERT INTO fit_reviews(key, kind, payload, candidates) VALUES (?, ?, ?, ?)
                  ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, candidates=excluded.candidates
                  WHERE resolution IS NULL""", (key, kind, json.dumps(payload), json.dumps(sorted(candidates))))
    if db.execute("""SELECT 1 FROM fit_review_issues r JOIN import_issues i ON i.id=r.issue_id
                     WHERE i.import_id=? AND r.review_key=?""", (import_id, key)).fetchone():
        return
    message = ("Confirm the activity's local date." if kind == "date" else
               "Review the possible match before this run is counted.")
    issue = db.execute("""INSERT INTO import_issues(import_id, severity, item, step, message)
                          VALUES (?, 'review', ?, ?, ?)""", (import_id, item, "Local date" if kind == "date" else
                                                             "Activity identity", message)).lastrowid
    db.execute("INSERT INTO fit_review_issues VALUES (?, ?)", (issue, key))


def fit_owners(db, column, value):
    if value is None:
        return set()
    # A pre-upgrade database can already contain conflicting Garmin IDs for one original.
    original_column = "fit_sha" if column == "sha256" else "identity"
    return {r[0] for r in db.execute(f"""SELECT activity_id FROM fit_sources WHERE {column}=?
               UNION SELECT s.activity_id FROM original_members m JOIN activity_sources s ON s.sha256=m.original_sha
               WHERE s.kind='original' AND m.{original_column}=?""", (value, value))}


def garmin_owner(db, gid):
    return db.execute("""SELECT id FROM activities WHERE garmin_id=?
                         UNION SELECT activity_id FROM garmin_aliases WHERE garmin_id=?""", (gid, gid)).fetchone()


def identify(db, payload):
    fields, fit = payload["fields"], payload["fit"]
    gid = fields["garmin_id"]
    known = garmin_owner(db, gid)
    hashes = fit_owners(db, "sha256", fit["sha256"]) if fit else set()
    exact = hashes or (fit_owners(db, "identity", fit["identity"]) if fit else set())
    candidates = exact | ({known["id"]} if known else set())
    if candidates:
        if len(candidates) == 1:
            target = next(iter(candidates))
            existing = db.execute("SELECT garmin_id FROM activities WHERE id=?", (target,)).fetchone()[0]
            if known or not gid or not existing:
                return target, set(), hashes
        return None, candidates, hashes
    start_utc = fields["start_utc"] or (fit and fit["fields"]["start_utc"])
    if start_utc:
        start = datetime.fromisoformat(start_utc)
        tolerance = timedelta(seconds=60)
        candidates = {r[0] for r in db.execute("""SELECT id FROM activities WHERE start_utc BETWEEN ? AND ?
                     AND (? IS NULL OR garmin_id IS NULL)""",
                     ((start - tolerance).isoformat(), (start + tolerance).isoformat(), gid))}
    return None, candidates, hashes


def merge_fields(fields, fallback):
    if not fallback:
        return fields
    fields = dict(fields)
    origins = json.loads(fields["metric_sources"])
    fallback_origins = json.loads(fallback["metric_sources"])
    for key in ("start_utc", "start_local", "local_date", "distance_m", "timer_seconds", "elapsed_seconds", "moving_seconds"):
        if key == "start_local" and fields["local_date"] and fields["local_date"] != fallback["local_date"]:
            continue
        if fields[key] is None and fallback[key] is not None:
            fields[key] = fallback[key]
            origins[key] = fallback_origins.get(key, "fit")
    fields["metric_sources"] = json.dumps(origins, sort_keys=True)
    return fields


def record_activity(db, import_id, activity_id, outcome):
    db.execute("""INSERT INTO import_activities(import_id, activity_id, outcome) VALUES (?, ?, ?)
                  ON CONFLICT(import_id, activity_id) DO UPDATE SET outcome=excluded.outcome
                  WHERE import_activities.outcome='unchanged'""", (import_id, activity_id, outcome))


def store_laps(db, activity_id, laps):
    db.execute("DELETE FROM laps WHERE activity_id=?", (activity_id,))
    db.executemany("""INSERT INTO laps(activity_id, sequence, distance_m, timer_seconds, elapsed_seconds, moving_seconds)
                      VALUES (:activity_id, :sequence, :distance_m, :timer_seconds, :elapsed_seconds, :moving_seconds)""",
                   ({**lap, "activity_id": activity_id, "sequence": n} for n, lap in enumerate(laps, 1)))


def save(db, import_id, payload, target, acquired_at):
    fields, fit, warning = payload["fields"], payload["fit"], payload["warning"]
    previous = db.execute("SELECT * FROM activities WHERE id=?", (target,)).fetchone()
    if fit:
        fields = merge_fields(fields, fit["fields"])
    if previous:
        if fields["garmin_id"] is None or (previous["garmin_id"] and previous["garmin_id"] != fields["garmin_id"]):
            # An uploaded file or explicitly linked second Garmin ID cannot erase edited Garmin headlines.
            fields = merge_fields({k: previous[k] for k in fields}, fields)
            warning = previous["original_warning"]
        else:
            # Keep FIT-only values when a Garmin update lacks its optional original.
            origins = json.loads(previous["metric_sources"])
            old_fit = {k: previous[k] if origins.get(k) == "fit" else None for k in fields}
            old_fit["metric_sources"] = previous["metric_sources"]
            fields = merge_fields(fields, old_fit)
        db.execute(f"""UPDATE activities SET {', '.join(k+'=:'+k for k in fields)}, original_warning=:warning
                       WHERE id=:target""", {**fields, "warning": warning, "target": target})
    else:
        # A bookmarked ID redirected by an explicit merge must never be reused.
        target = db.execute(f"""INSERT INTO activities(id, {', '.join(fields)}, original_warning)
                                VALUES ((SELECT MAX(id)+1 FROM (SELECT MAX(id) id FROM activities UNION ALL
                                       SELECT MAX(old_id) FROM activity_redirects)),
                                       {', '.join(':'+k for k in fields)}, :warning)""",
                            {**fields, "warning": warning}).lastrowid
    changed = not previous or previous["original_warning"] != warning or any(previous[k] != v for k, v in fields.items())
    for kind, digest, file_format in payload["sources"]:
        if kind == "original_error":
            db.execute("DELETE FROM activity_sources WHERE activity_id=? AND kind='original' AND sha256=?", (target, digest))
        added = db.execute("""INSERT INTO activity_sources(activity_id, kind, sha256, format, acquired_at)
                              VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""", (target, kind, digest, file_format, acquired_at))
        if added.rowcount and kind != "list":
            changed = True
    if fit:
        db.execute("""INSERT INTO fit_sources(sha256, identity, activity_id) VALUES (?, ?, ?)
                      ON CONFLICT(sha256) DO NOTHING""", (fit["sha256"], fit["identity"], target))
        authoritative = fields["garmin_id"] is not None and payload["fields"]["garmin_id"] == fields["garmin_id"]
        if fit["laps"] and (authoritative or not db.execute("SELECT 1 FROM laps WHERE activity_id=?", (target,)).fetchone()):
            store_laps(db, target, fit["laps"])
        for kind, digest, _ in payload["sources"]:
            if kind == "original":
                db.execute("INSERT OR REPLACE INTO original_members VALUES (?, ?, ?)", (digest, fit["sha256"], fit["identity"]))
        db.execute("UPDATE fit_uploads SET activity_id=? WHERE import_id=? AND sha256=?", (target, import_id, fit["sha256"]))
    outcome = ("updated" if changed else "unchanged") if previous else "inserted"
    record_activity(db, import_id, target, outcome)
    date_key = f"date:{target}"
    if not fields["local_date"]:
        remember_issue(db, import_id, date_key, "date", payload["item"], {}, [target])
    else:
        unresolved_dates = db.execute("""SELECT key FROM fit_reviews WHERE resolution IS NULL AND kind='date'
                                         AND candidates=?""", (json.dumps([target]),)).fetchall()
        for (key,) in unresolved_dates:
            apply_review(db, key, "recorded_date", target, fields["local_date"])
    return target


def apply_review(db, key, action, target, confirmed_date=None):
    db.execute("UPDATE fit_reviews SET resolution=?, activity_id=?, local_date=? WHERE key=?",
               (action, target, confirmed_date, key))
    imports = db.execute("""SELECT DISTINCT i.import_id FROM fit_review_issues r JOIN import_issues i ON i.id=r.issue_id
                            WHERE r.review_key=?""", (key,)).fetchall()
    for (import_id,) in imports:
        record_activity(db, import_id, target, "unchanged")
        db.execute("""UPDATE fit_uploads SET activity_id=? WHERE import_id=? AND sha256 IN
                      (SELECT sha256 FROM fit_sources WHERE activity_id=?)""", (target, import_id, target))
        db.execute(f"UPDATE imports SET state='complete' WHERE id=? AND state='partial' AND NOT EXISTS ({OPEN_ISSUES})",
                   (import_id,))


def ingest(db, import_id, payload, acquired_at):
    fit, gid = payload["fit"], payload["fields"]["garmin_id"]
    if payload["warning"]:
        db.execute("""INSERT INTO import_issues(import_id, severity, item, step, message)
                      VALUES (?, 'warning', ?, 'Original download', ?)""", (import_id, payload["item"], payload["warning"]))
    key = f"garmin:{gid}" if gid else "fit:" + fit["sha256"]
    target, candidates, hashes = identify(db, payload)
    review = db.execute("SELECT * FROM fit_reviews WHERE key=?", (key,)).fetchone()
    if not fit and gid:
        # A summary cannot choose between versions of a previously linked original.
        outstanding = db.execute("""SELECT f.* FROM fit_reviews f JOIN fit_review_issues r ON r.review_key=f.key
                                    WHERE f.resolution IS NULL AND (f.key=? OR f.key GLOB ?)
                                    ORDER BY r.issue_id DESC LIMIT 1""", (key, key + ":*")).fetchone()
        if outstanding:
            key, review = outstanding["key"], outstanding
        elif review and review["resolution"] and garmin_owner(db, gid):
            # The current alias can reflect a later, explicitly resolved original.
            review = None
    if review and review["resolution"] and hashes and review["activity_id"] not in hashes:
        # The provider changed a previously resolved ID to bytes owned by a different run.
        key += ":" + fit["sha256"]
        review = db.execute("SELECT * FROM fit_reviews WHERE key=?", (key,)).fetchone()
    if review and review["resolution"]:
        target, candidates = review["activity_id"], set()
    elif review:
        previous = json.loads(review["payload"])
        # Keep the latest valid Garmin headlines and all their source versions.
        # A missing original leaves the earlier FIT and its pending choice intact.
        prior_sources = [s for s in previous["sources"] if s[0] != "original" or not fit]
        sources = list(dict.fromkeys(tuple(s) for s in [*prior_sources, *payload["sources"]]))
        payload = {**payload, "sources": sources}
        if not fit:
            candidates = set(json.loads(review["candidates"]))
            payload["fit"] = previous["fit"]
            if payload["fit"]:
                payload["warning"] = None
    if candidates:
        remember_issue(db, import_id, key, "match", payload["item"], payload, candidates)
        return None
    target = save(db, import_id, payload, target, acquired_at)
    if review and not review["resolution"]:
        apply_review(db, key, "exact_match", target)
    return target


def merge_activity(db, donor, target):
    """Move an explicitly linked existing row and keep its old local URL usable."""
    existing = db.execute("SELECT * FROM activities WHERE id=?", (target,)).fetchone()
    old = db.execute("SELECT * FROM activities WHERE id=?", (donor,)).fetchone()
    merged = merge_fields(existing, old)
    columns = ("start_utc", "start_local", "local_date", "distance_m", "timer_seconds", "elapsed_seconds",
               "moving_seconds", "metric_sources")
    db.execute(f"UPDATE activities SET {', '.join(k+'=:'+k for k in columns)} WHERE id=:id", merged)
    for statement in (
        """INSERT OR IGNORE INTO activity_sources SELECT ?, kind, sha256, format, acquired_at
           FROM activity_sources WHERE activity_id=?""",
        """INSERT OR IGNORE INTO import_activities SELECT import_id, ?, outcome
           FROM import_activities WHERE activity_id=?""",
    ):
        db.execute(statement, (target, donor))
    if not db.execute("SELECT 1 FROM laps WHERE activity_id=?", (target,)).fetchone():
        db.execute("UPDATE laps SET activity_id=? WHERE activity_id=?", (target, donor))
    for table in ("activity_sources", "import_activities", "laps"):
        db.execute(f"DELETE FROM {table} WHERE activity_id=?", (donor,))
    for table in ("fit_sources", "fit_uploads", "fit_reviews", "activity_redirects", "garmin_aliases"):
        db.execute(f"UPDATE {table} SET activity_id=? WHERE activity_id=?", (target, donor))
    rows = db.execute("""SELECT key, candidates FROM fit_reviews WHERE ? IN (SELECT value FROM json_each(candidates))""",
                      (donor,)).fetchall()
    for key, candidates in rows:
        ids = (set(json.loads(candidates)) - {donor}) | {target}
        db.execute("UPDATE fit_reviews SET candidates=? WHERE key=?", (json.dumps(sorted(ids)), key))
    db.execute("""INSERT OR IGNORE INTO garmin_aliases SELECT garmin_id, ? FROM activities
                  WHERE id=? AND garmin_id IS NOT NULL""", (target, donor))
    db.execute("DELETE FROM activities WHERE id=?", (donor,))
    db.execute("INSERT INTO activity_redirects VALUES (?, ?)", (donor, target))


def resolve(db, import_id, issue_id, action, activity_id, local_date, acquired_at):
    row = db.execute("""SELECT f.* FROM import_issues i JOIN fit_review_issues r ON r.issue_id=i.id
                        JOIN fit_reviews f ON f.key=r.review_key WHERE i.import_id=? AND i.id=?""",
                     (import_id, issue_id)).fetchone()
    if not row:
        raise KeyError
    if row["resolution"]:
        if (row["resolution"] == action and (activity_id is None or activity_id == row["activity_id"])
                and (local_date is None or local_date == row["local_date"])):
            return row["activity_id"]
        raise ResolutionError("This issue already has a resolution. Refresh the report.")
    choices = json.loads(row["candidates"])
    if row["kind"] == "date":
        if action != "set_local_date":
            raise ResolutionError("Confirm a local date for this activity.")
        target, = choices
        db.execute("""UPDATE activities SET local_date=?, metric_sources=json_set(metric_sources, '$.local_date', 'user')
                      WHERE id=?""", (local_date, target))
    else:
        payload = json.loads(row["payload"])
        exact, _, hashes = identify(db, payload)
        if action == "link":
            # An earlier alias can point at a separate, already identified Garmin run.
            # Only a primary row for this incoming ID is a row to consolidate.
            primary = db.execute("SELECT id FROM activities WHERE garmin_id=?", (payload["fields"]["garmin_id"],)).fetchone()
            owners = hashes | ({primary[0]} if primary else set())
            if primary and not hashes and payload["fit"]:
                tuples = fit_owners(db, "identity", payload["fit"]["identity"])
                if len(tuples) == 1:
                    owners |= tuples
            if activity_id not in choices or (hashes and activity_id not in owners):
                raise ResolutionError("Choose a listed activity that can own these FIT bytes.")
            if not owners.issubset(choices) or (exact is not None and exact != activity_id):
                raise ResolutionError("Another activity now owns this input. Retry the import to refresh the choices.")
            target = activity_id
            donors = owners - {target}
            for donor in donors:
                merge_activity(db, donor, target)
            if donors:
                previous = db.execute("SELECT * FROM activities WHERE id=?", (target,)).fetchone()
                fields = merge_fields(payload["fields"], payload["fit"]["fields"] if payload["fit"] else None)
                payload = {**payload, "fields": merge_fields(fields, previous)}
        elif action == "keep_separate":
            if hashes or exact is not None:
                raise ResolutionError("This input already identifies an activity. Link it or retry the import.")
            gid = payload["fields"]["garmin_id"]
            prior = garmin_owner(db, gid)
            target = prior["id"] if prior else None
        else:
            raise ResolutionError("Link the run or confirm it is separate.")
        target = save(db, import_id, payload, target, acquired_at)
        gid = payload["fields"]["garmin_id"]
        if gid and not db.execute("SELECT 1 FROM activities WHERE garmin_id=?", (gid,)).fetchone():
            db.execute("INSERT OR REPLACE INTO garmin_aliases VALUES (?, ?)", (gid, target))
    apply_review(db, row["key"], action, target, local_date)
    return target
