"""Private source bytes and the local import, activity and report records."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile


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


class Archive:
    def __init__(self, directory):
        self.directory = Path(directory)

    def contains(self, digest):
        try:
            with (self.directory / digest).open("rb") as saved:
                return hashlib.file_digest(saved, "sha256").hexdigest() == digest
        except FileNotFoundError:
            return False

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.directory / digest
        if target.exists() and self.contains(digest):
            return digest
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as pending:
            try:
                pending.write(data)
                pending.flush()
                os.fsync(pending.fileno())
                try:
                    os.link(pending.name, target)  # Publish without replacing an immutable source.
                except FileExistsError:
                    if not self.contains(digest):
                        raise OSError("Archived source checksum mismatch") from None
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
        uri = self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if create:
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
        with self.history() as db:
            pending = db and db.execute("SELECT 1 FROM imports WHERE state='running'").fetchone()
        if pending:
            with self.connect(write=True) as db:
                db.execute("UPDATE imports SET state='interrupted', finished_at=? WHERE state='running'", (now(),))

    def start(self, first, last):
        with self.connect(write=True, create=True) as db:
            active = db.execute("SELECT id FROM imports WHERE state='running'").fetchone()
            if active:
                raise ActiveImport(active["id"])
            return db.execute("""
                INSERT INTO imports(requested_from, requested_to, started_at, state)
                VALUES (?, ?, ?, 'running')""", (first, last, now())).lastrowid

    def finish(self, import_id, state):
        with self.connect(write=True) as db:
            db.execute("UPDATE imports SET state=?, finished_at=? WHERE id=? AND state='running'",
                       (state, now(), import_id))

    def issue(self, import_id, item, step, message, severity="error"):
        with self.connect(write=True) as db:
            db.execute("INSERT INTO import_issues(import_id, severity, item, step, message) VALUES (?, ?, ?, ?, ?)",
                       (import_id, severity, item, step, message))

    def originals(self, garmin_id):
        with self.connect() as db:
            rows = db.execute("""SELECT s.sha256, s.format FROM activity_sources s
                                 JOIN activities a ON a.id=s.activity_id
                                 WHERE a.garmin_id=? AND s.kind='original' ORDER BY s.rowid DESC""", (garmin_id,))
            return [tuple(row) for row in rows]

    def save(self, import_id, fields, sources, warning):
        # Every key is supplied by the normalizer; no upstream field name enters SQL.
        columns = tuple(fields)
        bindings = ", ".join(":" + key for key in columns)
        updates = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "garmin_id")
        with self.connect(write=True) as db:
            previous = db.execute("SELECT * FROM activities WHERE garmin_id=?", (fields["garmin_id"],)).fetchone()
            activity_id = db.execute(f"""
                INSERT INTO activities({', '.join(columns)}, original_warning) VALUES ({bindings}, :original_warning)
                ON CONFLICT(garmin_id) DO UPDATE SET {updates}, original_warning=excluded.original_warning
                RETURNING id""", {**fields, "original_warning": warning}).fetchone()["id"]
            added = 0
            for kind, digest, file_format in sources:
                added += db.execute("""
                    INSERT INTO activity_sources(activity_id, kind, sha256, format, acquired_at)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(activity_id, kind, sha256) DO NOTHING""",
                    (activity_id, kind, digest, file_format, now())).rowcount
            outcome = "inserted"
            if previous:
                changed = added or previous["original_warning"] != warning or any(previous[k] != fields[k] for k in columns)
                outcome = "updated" if changed else "unchanged"
            db.execute("INSERT INTO import_activities(import_id, activity_id, outcome) VALUES (?, ?, ?)",
                       (import_id, activity_id, outcome))
            if warning:
                db.execute("""INSERT INTO import_issues(import_id, severity, item, step, message)
                              VALUES (?, 'warning', ?, 'Original download', ?)""",
                           (import_id, fields["garmin_id"], warning))

    def reports(self):
        with self.history() as db:
            return [] if db is None else [dict(r) for r in db.execute("SELECT * FROM imports ORDER BY id DESC")]

    def report(self, import_id):
        with self.history() as db:
            row = db and db.execute("SELECT * FROM imports WHERE id=?", (str(import_id),)).fetchone()
            if not row:
                return None
            activities = db.execute("""SELECT a.id, a.garmin_id, ia.outcome FROM import_activities ia
                                       JOIN activities a ON a.id=ia.activity_id WHERE ia.import_id=? ORDER BY a.id""",
                                    (import_id,))
            issues = db.execute("SELECT severity, item, step, message FROM import_issues WHERE import_id=? ORDER BY id",
                                (import_id,))
            return {**dict(row), "activities": [dict(a) for a in activities], "issues": [dict(i) for i in issues]}

    def activity(self, activity_id):
        with self.history() as db:
            row = db and db.execute("SELECT * FROM activities WHERE id=?", (str(activity_id),)).fetchone()
            if not row:
                return None
            activity = dict(row)
            warning = activity.pop("original_warning")
            sources = db.execute("""SELECT kind, sha256, format, acquired_at FROM activity_sources
                                    WHERE activity_id=? ORDER BY rowid""", (activity_id,))
            return {**activity, "garmin_url": "https://connect.garmin.com/modern/activity/" + row["garmin_id"],
                    "metric_sources": json.loads(row["metric_sources"]), "sources": [dict(s) for s in sources],
                    "warnings": [warning] if warning else []}
