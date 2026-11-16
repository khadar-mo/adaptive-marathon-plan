"""Private source bytes and the local import, activity and report records."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time

import fit_store
import garmin_coverage


SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY, requested_from TEXT, requested_to TEXT,
    started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'garmin'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_import ON imports(state) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY, garmin_id TEXT UNIQUE, name TEXT, sport TEXT,
    start_local TEXT, local_date TEXT, start_utc TEXT,
    distance_m REAL, timer_seconds REAL, elapsed_seconds REAL, moving_seconds REAL,
    metric_sources TEXT NOT NULL, original_warning TEXT
);
CREATE TABLE IF NOT EXISTS activity_sources (
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    kind TEXT NOT NULL, sha256 TEXT NOT NULL, format TEXT NOT NULL, acquired_at TEXT NOT NULL,
    PRIMARY KEY (activity_id, kind, sha256)
);
CREATE TABLE IF NOT EXISTS import_activities (
    import_id INTEGER NOT NULL REFERENCES imports(id), activity_id INTEGER NOT NULL REFERENCES activities(id),
    outcome TEXT NOT NULL, PRIMARY KEY (import_id, activity_id)
);
CREATE TABLE IF NOT EXISTS import_issues (
    id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL REFERENCES imports(id),
    severity TEXT NOT NULL, item TEXT NOT NULL, step TEXT NOT NULL, message TEXT NOT NULL
);
""" + fit_store.SCHEMA


def prepare_schema(db):
    # Older Garmin-only databases required both dates. Rebuild those two tables
    # in one transaction, preserving local IDs, dependent rows and indexes.
    db.execute("PRAGMA foreign_keys=OFF")
    with db:
        db.execute("BEGIN IMMEDIATE")
        for table, dates in (("activities", ("start_local", "local_date")), ("imports", ("requested_from", "requested_to"))):
            if not any(c["name"] in dates and c["notnull"] for c in db.execute(f"PRAGMA table_info({table})")):
                continue
            definition = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            indexes = db.execute("""SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger')
                                    AND sql IS NOT NULL""", (table,)).fetchall()
            for column in dates:
                definition = definition.replace(column + " TEXT NOT NULL", column + " TEXT")
            temporary = table + "_fit_upgrade"
            db.execute(definition.replace(f"TABLE {table} ", f"TABLE {temporary} ", 1))
            db.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for row in indexes:
                db.execute(row[0])
        import_columns = {c["name"] for c in db.execute("PRAGMA table_info(imports)")}
        if import_columns and "source" not in import_columns:
            db.execute("ALTER TABLE imports ADD COLUMN source TEXT NOT NULL DEFAULT 'garmin'")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Cannot migrate inconsistent activity references")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_id(value):
    if re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
        if 0 < number < 2**63:
            return number
    return None


@contextmanager
def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("Private storage needs a regular file")
        if mode & 0o077:
            os.fchmod(fd, mode & 0o600)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


class ArchivePermissionsError(OSError):
    pass


class ArchiveChecksumError(OSError):
    pass


def protect_sqlite_file(path):
    # POSIX locks on this inode may belong to another SQLite connection in this
    # process. Opening and closing an independent fd can release those locks.
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise OSError("SQLite needs a regular file")
    if mode & 0o077:
        os.chmod(path, mode & 0o600, follow_symlinks=False)


class Archive:
    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self, digest):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise OSError("Invalid archive reference")
        try:
            with private_file(self.directory / digest) as saved:
                data = saved.read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ArchiveChecksumError("Archived source checksum mismatch")
            return data
        except FileNotFoundError:
            return None

    def available(self, digest):
        try:
            return self.read(digest) is not None
        except OSError:
            return False

    def put(self, data):
        return self.put_file(BytesIO(data))

    def put_file(self, stream):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ArchivePermissionsError("Use a private archive directory with mode 0700, then retry these dates.")
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as pending:
            try:
                checksum = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    checksum.update(chunk)
                    pending.write(chunk)
                digest = checksum.hexdigest()
                target = self.directory / digest
                repair = False
                try:
                    if self.read(digest) is not None:
                        return digest
                except (ArchiveChecksumError, PermissionError):
                    if not stat.S_ISREG(target.lstat().st_mode):
                        raise
                    repair = True
                pending.flush()
                os.fsync(pending.fileno())
                try:
                    if repair:
                        # The old bytes failed the checksum. Restore the exact original atomically.
                        os.replace(pending.name, target)
                    else:
                        os.link(pending.name, target)
                except FileExistsError:
                    if self.read(digest) is None:
                        raise OSError("Archived source disappeared during publication") from None
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                Path(pending.name).unlink(missing_ok=True)
        return digest


class ActiveImport(Exception):
    def __init__(self, import_id):
        self.import_id = import_id


class Store:
    def __init__(self, path, archive=None):
        self.path = Path(path)
        self.archive = archive or Archive(self.path.with_name("runs-archive"))
        self.recovery_lock = threading.Lock()
        self.recovery_pending = True

    @contextmanager
    def connect(self, *, write=False, create=False):
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                pass
        if not stat.S_ISREG(self.path.stat().st_mode):
            raise OSError("SQLite needs a regular file")
        if write:
            # Earlier versions documented creating an empty DB with the shell's umask.
            resolved = self.path.resolve(strict=True)
            protect_sqlite_file(resolved)
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    protect_sqlite_file(Path(str(resolved) + suffix))
                except FileNotFoundError:
                    pass
        uri = self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if create:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fit_uploads'").fetchone():
                    prepare_schema(db)
                else:
                    db.executescript(SCHEMA)
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if create and not db.execute("SELECT 1 FROM sqlite_master WHERE name='garmin_scans'").fetchone():
                    for statement in garmin_coverage.SCHEMA.strip().split(";"):
                        if statement.strip():
                            db.execute(statement)
                    garmin_coverage.migrate(db)
                yield db

    @contextmanager
    def history(self):
        # A fresh install can open Imports without creating a file or changing /health.
        if not self.path.exists():
            yield None
            return
        with self.connect() as db:
            initialized = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='imports'").fetchone()
            yield db if initialized else None

    def interrupt_unfinished(self):
        with self.recovery_lock:
            if self.recovery_pending:
                with self.history() as db:
                    pending = db and db.execute("SELECT 1 FROM imports WHERE state='running'").fetchone()
                    upgrade = db and db.execute("""SELECT COUNT(*) FROM sqlite_master
                                                   WHERE name IN ('garmin_aliases', 'garmin_scans')""").fetchone()[0] < 2
                if upgrade:
                    with self.connect(write=True, create=True):
                        pass
                if pending:
                    with self.connect(write=True) as db:
                        db.execute("UPDATE imports SET state='interrupted', finished_at=? WHERE state='running'", (now(),))
                self.recovery_pending = False

    def start(self, first, last, *, source='garmin'):
        self.interrupt_unfinished()  # Retry startup recovery if storage was unavailable.
        active_query = "SELECT id FROM imports WHERE state='running'"
        with self.history() as db:
            active = db and db.execute(active_query).fetchone()
            if active:
                raise ActiveImport(active["id"])
        with self.connect(write=True, create=True) as db:
            active = db.execute(active_query).fetchone()
            if active:
                raise ActiveImport(active["id"])
            scans = garmin_coverage.plan(db, first, last) if source == "garmin" else []
            if scans:
                _, first, last = scans[0]
            import_id = db.execute("""
                INSERT INTO imports(requested_from, requested_to, started_at, state, source)
                VALUES (?, ?, ?, 'running', ?)""", (first, last, now(), source)).lastrowid
            db.executemany("INSERT INTO garmin_scans(import_id, mode, first, last) VALUES (?, ?, ?, ?)",
                           [(import_id, mode, start, end) for mode, start, end in scans])
            return import_id

    def scans(self, import_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM garmin_scans WHERE import_id=? ORDER BY id", (import_id,)).fetchall()

    def retry_items(self, first, last):
        start, end = garmin_coverage.bounds(first, last)
        with self.connect() as db:
            return [i for i in garmin_coverage.outstanding_items(db)
                    if (span := garmin_coverage.item_range(i))[0] <= end and start <= span[1]]

    def observe(self, scan_id, garmin_id, local_date, page_sha, record_sha, from_search):
        with self.connect(write=True) as db:
            previous = db.execute("""SELECT status, record_sha, detail_sha FROM garmin_scan_items
                WHERE garmin_id=? ORDER BY scan_id DESC LIMIT 1""", (garmin_id,)).fetchone()
            db.execute("""INSERT INTO garmin_scan_items(scan_id, garmin_id, local_date, list_sha, record_sha, from_search)
                VALUES (?, ?, ?, ?, ?, ?)""", (scan_id, garmin_id, local_date, page_sha, record_sha, from_search))
            saved = db.execute("""SELECT detail_sha FROM garmin_scan_items WHERE garmin_id=?
                                  AND detail_sha IS NOT NULL
                                  ORDER BY scan_id DESC LIMIT 1""", (garmin_id,)).fetchone()
            reusable = bool(previous and saved and previous["status"] not in ("missing_activity", "pending_identity")
                            and previous["record_sha"] == record_sha and previous["detail_sha"] == saved[0])
            if saved:
                return [saved[0]], reusable
            # A new detail acquired exclusively during the last PR #9 save identifies
            # its selected version. Overlapping timestamps or a reused blob do not.
            selected = db.execute("""WITH attempts AS (
                    SELECT i.id, i.started_at, i.finished_at, a.id AS activity_id FROM imports i
                    JOIN import_activities ia ON ia.import_id=i.id JOIN activities a ON a.id=ia.activity_id
                    JOIN garmin_scans g ON g.import_id=i.id AND g.mode='legacy' WHERE a.garmin_id=?
                ), last_save AS (SELECT * FROM attempts ORDER BY id DESC LIMIT 1)
                SELECT s.sha256 FROM last_save l JOIN activity_sources s ON s.activity_id=l.activity_id
                WHERE s.kind='detail' AND s.acquired_at BETWEEN l.started_at AND l.finished_at
                AND NOT EXISTS (SELECT 1 FROM attempts p WHERE p.id!=l.id AND p.started_at<=s.acquired_at
                                AND (p.finished_at IS NULL OR s.acquired_at<=p.finished_at))""", (garmin_id,)).fetchall()
            if len(selected) == 1:
                return [selected[0][0]], False
            versions = db.execute("""SELECT s.sha256 FROM activity_sources s JOIN activities a ON a.id=s.activity_id
                                     WHERE a.garmin_id=? AND s.kind='detail' ORDER BY s.rowid DESC""", (garmin_id,))
            return [row[0] for row in versions], False

    def write_report(self, statement, parameters, *, review_import=None):
        # The background worker still owns the running row while another SQLite
        # connection holds a lock. Persist its issues and outcome once the lock clears.
        while True:
            try:
                with self.connect(write=True) as db:
                    db.execute(statement, parameters)
                    if review_import is not None:
                        fit_store.complete_reviewed_import(db, review_import)
                return
            except sqlite3.OperationalError as exc:
                if (getattr(exc, "sqlite_errorcode", 0) & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                time.sleep(0.05)

    def finish(self, import_id, state):
        try:
            self.write_report("UPDATE imports SET state=?, finished_at=? WHERE id=? AND state='running'",
                              (state, now(), import_id), review_import=import_id if state == "partial" else None)
        except (sqlite3.Error, OSError):
            # The worker has exited without a durable outcome. Recover the
            # interrupted report on the next submission if storage comes back.
            with self.recovery_lock:
                self.recovery_pending = True

    def issue(self, import_id, item, step, message, severity="error"):
        self.write_report("INSERT INTO import_issues(import_id, severity, item, step, message) VALUES (?, ?, ?, ?, ?)",
                          (import_id, severity, item, step, message))

    def originals(self, garmin_id):
        with self.connect() as db:
            # Separately resolved versions of one Garmin ID can belong to different
            # local runs. Fetch the current bytes when the cached owner is ambiguous.
            owners = db.execute("""SELECT DISTINCT activity_id FROM fit_reviews WHERE key GLOB ?
                                   AND resolution IS NOT NULL LIMIT 2""", (f"garmin:{garmin_id}:*",)).fetchall()
            prefix = f"garmin:{garmin_id}"
            pending = db.execute("""SELECT 1 FROM fit_reviews WHERE (key=? OR key GLOB ?)
                                    AND resolution IS NULL LIMIT 1""", (prefix, prefix + ":*")).fetchone()
            if len(owners) > 1 or pending:
                return []
            owner = fit_store.garmin_owner(db, garmin_id)
            if not owner:
                return []
            # Recheck only the most recently observed version for this upstream ID.
            # An older usable ZIP may contain different recorded laps.
            version = db.execute("""SELECT original_sha FROM garmin_original_versions WHERE garmin_id=?
                                    ORDER BY last_import_id DESC, rowid DESC LIMIT 1""", (garmin_id,)).fetchone()
            selected = version["original_sha"] if version else None
            rows = db.execute("""SELECT sha256, format FROM activity_sources WHERE activity_id=?
                                 AND kind IN ('original', 'original_unavailable') AND (? IS NULL OR sha256=?)
                                 ORDER BY (kind='original') DESC, rowid DESC""", (owner["id"], selected, selected))
            return [tuple(row) for row in rows]

    def save(self, import_id, fields, sources, warning, fit=None, item=None, *,
             scan_id=None, detail_sha=None, detail_changed=False, status="saved"):
        gid = fields["garmin_id"]
        with self.connect(write=True) as db:
            if scan_id is not None:
                fit_store.reconcile_scan_edits(db, fields, sources, status, now())
            if status != "saved":
                db.execute("""UPDATE garmin_scan_items SET status=?, detail_sha=? WHERE scan_id=? AND garmin_id=?""",
                           (status, detail_sha, scan_id, gid))
                # A recorded date or sport edit can exclude a previously saved activity.
                previous = db.execute("SELECT * FROM activities WHERE garmin_id=?", (gid,)).fetchone()
                if previous:
                    if fields["local_date"] is None:
                        fields = {**{key: previous[key] for key in fields}, "sport": fields["sport"]}
                    elif fields["sport"] is None:
                        fields = {**fields, "sport": previous["sport"]}
                    fields = fit_store.retain_fit_fields(fields, previous)
                    db.execute(f"UPDATE activities SET {', '.join(k+'=:'+k for k in fields)} WHERE id=:id",
                               {**fields, "id": previous["id"]})
                    fit_store.save_sources(db, previous["id"], sources, now())
                return None
            payload = {"fields": fields, "sources": sources, "warning": warning, "fit": fit,
                       "item": item or gid, "detail_changed": detail_changed}
            target = fit_store.ingest(db, import_id, payload, now(), self.archive.available)
            if scan_id is not None:
                db.execute("""UPDATE garmin_scan_items SET status=?, detail_sha=?, original_available=?
                              WHERE scan_id=? AND garmin_id=?""",
                           ("saved" if target is not None else "pending_identity", detail_sha, warning is None, scan_id, gid))
            return target

    def resolve(self, import_id, issue_id, action, activity_id, date):
        with self.history() as db:
            if db is None:
                raise KeyError
        with self.connect(write=True) as db:
            return fit_store.resolve(db, local_id(import_id), local_id(issue_id), action, activity_id, date,
                                     now(), self.archive.available)

    @staticmethod
    def review_rows(db, import_id=None):
        rows = db.execute("""SELECT i.id, i.import_id, i.item, f.kind, f.payload, f.candidates, f.resolution, f.activity_id, f.local_date
                             FROM fit_review_issues r JOIN import_issues i ON i.id=r.issue_id
                             JOIN fit_reviews f ON f.key=r.review_key
                             WHERE i.import_id=? OR (? IS NULL AND f.resolution IS NULL AND i.id=(
                                 SELECT MAX(x.issue_id) FROM fit_review_issues x WHERE x.review_key=r.review_key))
                             ORDER BY i.id DESC""", (import_id, import_id))
        reviews = []
        for row in rows:
            choices = fit_store.review_choices(db, row)
            candidates = [dict(c) for c in db.execute("""SELECT id, name, garmin_id, local_date, start_utc, distance_m
                          FROM activities WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id""", (json.dumps(choices),))]
            public = dict(row)
            del public["payload"]
            reviews.append({**public, "candidates": candidates})
        return reviews

    def pending(self):
        with self.history() as db:
            return self.review_rows(db) if db is not None else []

    def reports(self):
        with self.history() as db:
            return [] if db is None else [dict(r) for r in db.execute("SELECT * FROM imports ORDER BY id DESC")]

    def report(self, import_id):
        with self.history() as db:
            row = db and db.execute("SELECT * FROM imports WHERE id=?", (local_id(import_id),)).fetchone()
            if not row:
                return None
            activities = db.execute("""SELECT a.id, a.garmin_id, ia.outcome FROM import_activities ia
                                       JOIN activities a ON a.id=ia.activity_id WHERE ia.import_id=? ORDER BY a.id""",
                                    (import_id,))
            issues = db.execute("SELECT id, severity, item, step, message FROM import_issues WHERE import_id=? ORDER BY id",
                                (import_id,))
            reviews = {r["id"]: r for r in self.review_rows(db, import_id)}
            files = db.execute("SELECT filename, sha256, activity_id FROM fit_uploads WHERE import_id=? ORDER BY ordinal",
                               (import_id,))
            activities = [dict(a) for a in activities]
            scans = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='garmin_scans'").fetchone():
                scans = [garmin_coverage.scan_report(db, scan) for scan in db.execute(
                    """SELECT s.*, i.started_at FROM garmin_scans s JOIN imports i ON i.id=s.import_id
                       WHERE s.import_id=? ORDER BY s.id""", (import_id,))]
            elif row["requested_from"] and row["requested_to"] and ("source" not in row.keys() or row["source"] == "garmin"):
                scans = [garmin_coverage.legacy_report(row["requested_from"], row["requested_to"])]
            return {**dict(row), "activities": activities, "files": [dict(f) for f in files],
                    "issues": [{**dict(i), **reviews.get(i["id"], {})} for i in issues],
                    "counts": {outcome: sum(a["outcome"] == outcome for a in activities)
                               for outcome in ("inserted", "updated", "unchanged")}, "scans": scans}

    def activity(self, activity_id):
        with self.history() as db:
            requested = local_id(activity_id)
            row = db and db.execute("""SELECT * FROM activities WHERE id=COALESCE(
                                     (SELECT activity_id FROM activity_redirects WHERE old_id=?), ?)""",
                                    (requested, requested)).fetchone()
            if not row:
                return None
            activity_id = row["id"]
            activity = dict(row)
            warning = activity.pop("original_warning")
            sources = db.execute("""SELECT kind, sha256, format, acquired_at FROM activity_sources
                                    WHERE activity_id=? ORDER BY rowid""", (activity_id,))
            laps = db.execute("""SELECT sequence, 'fit' AS source, distance_m, timer_seconds, elapsed_seconds, moving_seconds
                                 FROM laps WHERE activity_id=? ORDER BY sequence""", (activity_id,))
            laps = [dict(lap) for lap in laps]
            warnings = [warning] if warning else []
            if row["garmin_id"] and row["distance_m"] is not None and laps and all(lap["distance_m"] is not None for lap in laps):
                if abs(sum(lap["distance_m"] for lap in laps) - row["distance_m"]) > 1:
                    warnings.append("Recorded lap distances differ from the Garmin activity distance.")
            fits = db.execute("SELECT sha256 FROM fit_sources WHERE activity_id=? ORDER BY sha256", (activity_id,))
            return {**activity, "laps": laps, "fit_sha256": [f[0] for f in fits],
                    "garmin_url": "https://connect.garmin.com/modern/activity/" + row["garmin_id"] if row["garmin_id"] else None,
                    "metric_sources": json.loads(row["metric_sources"]), "sources": [dict(s) for s in sources],
                    "warnings": warnings}
