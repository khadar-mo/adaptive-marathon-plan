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
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("Private storage needs a regular file")
        if mode & 0o077:
            os.fchmod(fd, mode & 0o600)
        yield stream


class ArchivePermissionsError(OSError):
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
                raise OSError("Archived source checksum mismatch")
            return data
        except FileNotFoundError:
            return None

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
                if target.exists() and self.read(digest) is not None:
                    return digest
                pending.flush()
                os.fsync(pending.fileno())
                try:
                    os.link(pending.name, target)  # Publish without replacing an immutable source.
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
    def __init__(self, path):
        self.path = Path(path)
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
                    upgrade = db and not db.execute("SELECT 1 FROM sqlite_master WHERE name='garmin_aliases'").fetchone()
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
            return db.execute("""
                INSERT INTO imports(requested_from, requested_to, started_at, state, source)
                VALUES (?, ?, ?, 'running', ?)""", (first, last, now(), source)).lastrowid

    def write_report(self, statement, parameters):
        # The background worker still owns the running row while another SQLite
        # connection holds a lock. Persist its issues and outcome once the lock clears.
        while True:
            try:
                with self.connect(write=True) as db:
                    db.execute(statement, parameters)
                return
            except sqlite3.OperationalError as exc:
                if (getattr(exc, "sqlite_errorcode", 0) & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                time.sleep(0.05)

    def finish(self, import_id, state):
        try:
            self.write_report(f"""UPDATE imports SET state=CASE
                              WHEN ?='partial' AND EXISTS (SELECT 1 FROM import_issues i WHERE i.import_id=imports.id
                                   AND i.severity='review') AND NOT EXISTS ({fit_store.OPEN_ISSUES}) THEN 'complete'
                              ELSE ? END, finished_at=? WHERE id=? AND state='running'""", (state, state, now(), import_id))
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
            rows = db.execute("""SELECT s.sha256, s.format FROM activity_sources s
                                 JOIN activities a ON a.id=s.activity_id
                                 WHERE a.garmin_id=? AND s.kind='original' ORDER BY s.rowid DESC""", (garmin_id,))
            return [tuple(row) for row in rows]

    def save(self, import_id, fields, sources, warning, fit=None, item=None):
        payload = {"fields": fields, "sources": sources, "warning": warning, "fit": fit,
                   "item": item or fields["garmin_id"]}
        with self.connect(write=True) as db:
            return fit_store.ingest(db, import_id, payload, now())

    def resolve(self, import_id, issue_id, action, activity_id, date):
        with self.history() as db:
            if db is None:
                raise KeyError
        with self.connect(write=True) as db:
            return fit_store.resolve(db, local_id(import_id), local_id(issue_id), action, activity_id, date, now())

    @staticmethod
    def review_rows(db, import_id=None):
        rows = db.execute("""SELECT i.id, i.import_id, i.item, f.kind, f.candidates, f.resolution, f.activity_id, f.local_date
                             FROM fit_review_issues r JOIN import_issues i ON i.id=r.issue_id
                             JOIN fit_reviews f ON f.key=r.review_key
                             WHERE i.import_id=? OR (? IS NULL AND f.resolution IS NULL AND i.id=(
                                 SELECT MAX(x.issue_id) FROM fit_review_issues x WHERE x.review_key=r.review_key))
                             ORDER BY i.id DESC""", (import_id, import_id))
        reviews = []
        for row in rows:
            candidates = [dict(c) for c in db.execute("""SELECT id, name, garmin_id, local_date, start_utc, distance_m
                          FROM activities WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id""", (row["candidates"],))]
            reviews.append({**dict(row), "candidates": candidates})
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
            return {**dict(row), "activities": [dict(a) for a in activities],
                    "files": [dict(f) for f in files],
                    "issues": [{**dict(i), **reviews.get(i["id"], {})} for i in issues]}

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
