"""Archive uploaded files before replying and import their recorded sessions offline."""
import sqlite3

import source_receipts

from fit_data import FitError, decode_fit, fit_member
from fit_store import store_laps


def unpack_original(archive, data):
    file_format, member = fit_member(data)
    digest = archive.put(member)
    return file_format, {**decode_fit(member), "sha256": digest}


def unindexed_originals(db):
    return db.execute("""SELECT DISTINCT s.sha256 FROM activity_sources s
                 LEFT JOIN original_members m ON m.original_sha=s.sha256
                 WHERE s.kind IN ('original', 'original_unavailable') AND m.original_sha IS NULL ORDER BY s.rowid""").fetchall()


def index_originals_in_db(db, archive, originals=None):
    """Index local original members/laps inside the caller's transaction."""
    # PR #9 archived original bytes by the outer ZIP hash. Include recordings
    # whose Garmin summary lacked UTC; never open a nested SQLite handle.
    originals = unindexed_originals(db) if originals is None else originals
    for (outer,) in originals:
        try:
            data = archive.read(outer)
            if data is None:
                continue
            _, fit = unpack_original(archive, data)
        except OSError:
            # A missing or inaccessible legacy original can be repaired by Garmin.
            continue
        except FitError:
            fit = None
        db.execute("INSERT OR IGNORE INTO original_members VALUES (?, ?, ?)",
                   (outer, fit["sha256"] if fit else None, fit["identity"] if fit else None))
        if fit:
            owners = db.execute("""SELECT DISTINCT activity_id FROM activity_sources
                                   WHERE kind IN ('original', 'original_unavailable') AND sha256=?""",
                                (outer,)).fetchall()
            for (owner,) in owners:
                if not db.execute("SELECT 1 FROM fit_sources WHERE activity_id=?", (owner,)).fetchone():
                    store_laps(db, owner, fit["laps"])


def index_originals(store, archive):
    with store.history() as db:
        if db is None:
            return
        originals = unindexed_originals(db)
    for original in originals:
        with store.connect(write=True) as db:
            index_originals_in_db(db, archive, [original])


def stage_uploads(store, archive, import_id, files):
    names = [((f.filename or "").replace("\\", "/").rsplit("/", 1)[-1] or "Unnamed FIT") for f in files]
    with store.connect(write=True) as db:
        db.executemany("INSERT INTO fit_uploads(import_id, ordinal, filename) VALUES (?, ?, ?)",
                       ((import_id, n, name) for n, name in enumerate(names)))
    for ordinal, (name, file) in enumerate(zip(names, files)):
        try:
            digest = archive.put_file(file.file)
            with store.connect(write=True) as db:
                db.execute("UPDATE fit_uploads SET sha256=? WHERE import_id=? AND ordinal=?", (digest, import_id, ordinal))
                source_receipts.record_sources(db, [("fit", digest, "fit")], "FIT_UPLOAD_ARCHIVED",
                                               import_id=import_id, ordinal=ordinal)
        except (OSError, sqlite3.Error):
            store.issue(import_id, name, "Archive upload", "Check private archive and database permissions and upload again.")


def run_fit_import(store, archive, import_id):
    state = "failed"
    try:
        index_originals(store, archive)
        with store.connect() as db:
            files = db.execute("SELECT filename, sha256 FROM fit_uploads WHERE import_id=? ORDER BY ordinal", (import_id,)).fetchall()
        for filename, digest in files:
            if digest is None:
                continue
            try:
                data = archive.read(digest)
                if data is None:
                    raise OSError("Missing archive")
                fit = {**decode_fit(data), "sha256": digest}
                store.save(import_id, fit["fields"], [("fit", digest, "fit")], None, fit, filename)
            except FitError as err:
                store.issue(import_id, filename, "Decode FIT", str(err))
            except (OSError, sqlite3.Error):
                store.issue(import_id, filename, "Save FIT", "Check the private archive and database, then upload again.")
        report = store.report(str(import_id))
        unresolved = any(i["severity"] == "review" and not i["resolution"] for i in report["issues"])
        failures = any(i["severity"] == "error" for i in report["issues"])
        state = "partial" if unresolved or (failures and report["activities"]) else "failed" if failures else "complete"
    except (OSError, sqlite3.Error):
        store.issue(import_id, "FIT upload", "Read local evidence", "Check the database and archive together, then upload again.")
    except Exception:
        store.issue(import_id, "FIT upload", "Import FIT", "Check the saved report and retry the files.")
    finally:
        store.finish(import_id, state)
