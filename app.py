"""Local SQLite health endpoint for the running history app."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException

app = FastAPI()


@app.get("/health")
def health():
    path = Path(os.environ.get("RUNS_DB_PATH", "runs.sqlite3"))
    try:
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except (OSError, sqlite3.Error, ValueError):
        raise HTTPException(status_code=503, detail="SQLite is unavailable.") from None
    return {"status": "ok"}
