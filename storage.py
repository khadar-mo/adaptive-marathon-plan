"""Private source bytes and the local import, activity and report records."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
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

import garmin_coverage


SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY, requested_from TEXT NOT NULL, requested_to TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_import ON imports(state) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY, garmin_id TEXT UNIQUE, name TEXT, sport TEXT,
    start_local TEXT NOT NULL, local_date TEXT NOT NULL, start_utc TEXT,
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
"""


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

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ArchivePermissionsError("Use a private archive directory with mode 0700, then retry these dates.")
        target = self.directory / digest
        repair = False
        try:
            if self.read(digest) is not None:
                return digest
        except ArchiveChecksumError:
            repair = True
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as pending:
            try:
                pending.write(data)
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
                if pending:
                    with self.connect(write=True) as db:
                        db.execute("UPDATE imports SET state='interrupted', finished_at=? WHERE state='running'", (now(),))
                self.recovery_pending = False

    def start(self, first, last):
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
            scans = garmin_coverage.plan(db, first, last)
            _, first, last = scans[0]
            import_id = db.execute("""
                INSERT INTO imports(requested_from, requested_to, started_at, state)
                VALUES (?, ?, ?, 'running')""", (first, last, now())).lastrowid
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
                WHERE garmin_id=? AND status!='outside_range' ORDER BY scan_id DESC LIMIT 1""", (garmin_id,)).fetchone()
            db.execute("""INSERT INTO garmin_scan_items(scan_id, garmin_id, local_date, list_sha, record_sha, from_search)
                VALUES (?, ?, ?, ?, ?, ?)""", (scan_id, garmin_id, local_date, page_sha, record_sha, from_search))
            saved = db.execute("""SELECT detail_sha FROM garmin_scan_items WHERE garmin_id=?
                                  AND detail_sha IS NOT NULL
                                  ORDER BY scan_id DESC LIMIT 1""", (garmin_id,)).fetchone()
            if not saved:
                # A PR #9 database keeps its detail in activity_sources.
                saved = db.execute("""SELECT s.sha256 FROM activity_sources s JOIN activities a ON a.id=s.activity_id
                                      WHERE a.garmin_id=? AND s.kind='detail' ORDER BY s.rowid DESC LIMIT 1""",
                                   (garmin_id,)).fetchone()
            reusable = bool(previous and saved and previous["status"] == "saved"
                            and previous["record_sha"] == record_sha and previous["detail_sha"] == saved[0])
            return (saved[0] if saved else None), reusable

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
            self.write_report("UPDATE imports SET state=?, finished_at=? WHERE id=? AND state='running'",
                              (state, now(), import_id))
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

    def save(self, import_id, fields, sources, warning, *, scan_id, detail_sha, detail_changed, status):
        # Every key is supplied by the normalizer; no upstream field name enters SQL.
        columns = tuple(fields)
        bindings = ", ".join(":" + key for key in columns)
        updates = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "garmin_id")
        with self.connect(write=True) as db:
            previous = db.execute("SELECT * FROM activities WHERE garmin_id=?", (fields["garmin_id"],)).fetchone()
            if status != "saved":
                # An observed date/sport edit still applies to the stored activity.
                # Excluded IDs do not contribute to the range's saved IDs or counts.
                db.execute("""UPDATE garmin_scan_items SET status=?, detail_sha=? WHERE scan_id=? AND garmin_id=?""",
                           (status, detail_sha if previous else None, scan_id, fields["garmin_id"]))
                if previous is None:
                    return
                warning = previous["original_warning"]
            activity_id = db.execute(f"""
                INSERT INTO activities({', '.join(columns)}, original_warning) VALUES ({bindings}, :original_warning)
                ON CONFLICT(garmin_id) DO UPDATE SET {updates}, original_warning=excluded.original_warning
                RETURNING id""", {**fields, "original_warning": warning}).fetchone()["id"]
            changed_source = detail_changed
            for kind, digest, file_format in sources:
                if kind == "original_error":
                    db.execute("""DELETE FROM activity_sources WHERE activity_id=? AND kind='original' AND sha256=?""",
                               (activity_id, digest))
                added = db.execute("""
                    INSERT INTO activity_sources(activity_id, kind, sha256, format, acquired_at)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(activity_id, kind, sha256) DO NOTHING""",
                    (activity_id, kind, digest, file_format, now())).rowcount
                if added and kind not in ("list", "detail"):
                    changed_source = True
            if status != "saved":
                return
            outcome = "inserted"
            if previous:
                changed = (changed_source or previous["original_warning"] != warning
                           or any(previous[k] != fields[k] for k in columns))
                outcome = "updated" if changed else "unchanged"
            db.execute("INSERT INTO import_activities(import_id, activity_id, outcome) VALUES (?, ?, ?)",
                       (import_id, activity_id, outcome))
            db.execute("""UPDATE garmin_scan_items SET status='saved', detail_sha=?, original_available=?
                          WHERE scan_id=? AND garmin_id=?""",
                       (detail_sha, warning is None, scan_id, fields["garmin_id"]))
            if warning:
                db.execute("""INSERT INTO import_issues(import_id, severity, item, step, message)
                              VALUES (?, 'warning', ?, 'Original download', ?)""",
                           (import_id, fields["garmin_id"], warning))

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
            issues = db.execute("SELECT severity, item, step, message FROM import_issues WHERE import_id=? ORDER BY id",
                                (import_id,))
            activities = [dict(a) for a in activities]
            scans = [garmin_coverage.legacy_report(row["requested_from"], row["requested_to"])]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='garmin_scans'").fetchone():
                scans = [garmin_coverage.scan_report(db, scan) for scan in db.execute(
                    """SELECT s.*, i.started_at FROM garmin_scans s JOIN imports i ON i.id=s.import_id
                       WHERE s.import_id=? ORDER BY s.id""", (import_id,))]
            return {**dict(row), "activities": activities, "issues": [dict(i) for i in issues],
                    "counts": {outcome: sum(a["outcome"] == outcome for a in activities)
                               for outcome in ("inserted", "updated", "unchanged")}, "scans": scans}

    def activity(self, activity_id):
        with self.history() as db:
            row = db and db.execute("SELECT * FROM activities WHERE id=?", (local_id(activity_id),)).fetchone()
            if not row:
                return None
            activity = dict(row)
            warning = activity.pop("original_warning")
            sources = db.execute("""SELECT kind, sha256, format, acquired_at FROM activity_sources
                                    WHERE activity_id=? ORDER BY rowid""", (activity_id,))
            return {**activity, "garmin_url": "https://connect.garmin.com/modern/activity/" + row["garmin_id"],
                    "metric_sources": json.loads(row["metric_sources"]), "sources": [dict(s) for s in sources],
                    "warnings": [warning] if warning else []}
