"""Local SQLite health endpoint for the running history app."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat

from fastapi import FastAPI, HTTPException

app = FastAPI()


@app.get("/health")
def health():
    path = Path(os.environ.get("RUNS_DB_PATH", "runs.sqlite3"))
    try:
        info = path.stat()
        # A new SQLite file may be empty. An initialized file has at least one 512-byte page.
        if not stat.S_ISREG(info.st_mode) or 0 < info.st_size < 512:
            raise OSError("Invalid SQLite file")
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except (OSError, sqlite3.Error, ValueError):
        raise HTTPException(status_code=503, detail="SQLite is unavailable.") from None
    return {"status": "ok"}
