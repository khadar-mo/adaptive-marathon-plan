"""One loopback FastAPI process for local running history."""
from contextlib import asynccontextmanager, closing
from datetime import date
import os
from pathlib import Path
import sqlite3
import stat
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fit_import import index_originals, run_fit_import, stage_uploads
from fit_store import ResolutionError
from garmin_import import run_import
from storage import ActiveImport, Archive, Store
import views


@asynccontextmanager
async def lifespan(app):
    path = Path(os.environ.get("RUNS_DB_PATH", "runs.sqlite3"))
    app.state.store = Store(path)
    app.state.archive = Archive(os.environ.get("RUNS_ARCHIVE_PATH", path.with_name("runs-archive")))
    try:
        app.state.store.interrupt_unfinished()
        index_originals(app.state.store, app.state.archive)
    except (OSError, sqlite3.Error):
        # Keep /health reachable when storage is unreadable.
        pass
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def local_browser(request: Request, call_next):
    if request.method == "POST":
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        try:
            if origin is None and referer is not None:
                url = urlsplit(referer)
                origin = f"{url.scheme}://{url.netloc}"
        except ValueError:
            origin = "invalid"
        if origin not in (None, expected) or request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
            return JSONResponse({"detail": "Submit imports from the local Imports page."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(OSError)
@app.exception_handler(sqlite3.Error)
async def storage_error(request, exc):
    return JSONResponse({"detail": "Local storage is unavailable. Check SQLite and archive permissions."}, status_code=503)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    if request.method == "POST" and request.url.path == "/api/imports/garmin":
        return JSONResponse({"detail": "Enter From and To as YYYY-MM-DD, with From on or before To."}, status_code=422)
    if request.url.path.endswith("/resolve"):
        return JSONResponse({"detail": "Choose a resolution and enter a valid local activity ID or calendar date."}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
def health():
    path = app.state.store.path
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


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first: str | None = Field(None, alias="from", pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    last: str | None = Field(None, alias="to", pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @model_validator(mode="after")
    def ordered_calendar_dates(self):
        if not self.model_fields_set:
            return self
        if self.first is None or self.last is None:
            raise ValueError("Supply both From and To.")
        if date.fromisoformat(self.first) > date.fromisoformat(self.last):
            raise ValueError("From must be on or before To.")
        return self


def report_links(import_id):
    return {"id": import_id, "report_url": f"/api/imports/{import_id}", "html_url": f"/imports/{import_id}"}


@app.post("/api/imports/garmin", status_code=202)
def submit(dates: DateRange, background: BackgroundTasks):
    try:
        import_id = app.state.store.start(dates.first, dates.last)
    except ActiveImport as active:
        return JSONResponse({"detail": "An import is already running.", **report_links(active.import_id)}, status_code=409)
    links = report_links(import_id)
    background.add_task(run_import, app.state.store, app.state.archive, import_id)
    return JSONResponse(links, status_code=202, headers={"Location": links["report_url"]})


@app.post("/api/imports/fit", status_code=202)
def upload_fit(files: list[UploadFile], background: BackgroundTasks):
    try:
        import_id = app.state.store.start(None, None, source="fit")
    except ActiveImport as active:
        return JSONResponse({"detail": "An import is already running.", **report_links(active.import_id)}, status_code=409)
    try:
        stage_uploads(app.state.store, app.state.archive, import_id, files)
    except (sqlite3.Error, OSError):
        app.state.store.finish(import_id, "failed")
        raise
    links = report_links(import_id)
    background.add_task(run_fit_import, app.state.store, app.state.archive, import_id)
    return JSONResponse(links, status_code=202, headers={"Location": links["report_url"]})


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["link", "keep_separate", "set_local_date"]
    activity_id: int | None = Field(default=None, gt=0, lt=2**63, strict=True)
    local_date: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @model_validator(mode="after")
    def check_choice(self):
        if (self.action == "link") != (self.activity_id is not None):
            raise ValueError("Link requires a local activity ID.")
        if (self.action == "set_local_date") != (self.local_date is not None):
            raise ValueError("Set local date requires a calendar date.")
        if self.local_date:
            date.fromisoformat(self.local_date)
        return self


@app.post("/api/imports/{import_id}/issues/{issue_id}/resolve")
def resolve(import_id: str, issue_id: str, choice: Resolution):
    try:
        target = app.state.store.resolve(import_id, issue_id, choice.action, choice.activity_id, choice.local_date)
    except KeyError:
        raise HTTPException(404, "Import issue not found.") from None
    except ResolutionError as err:
        raise HTTPException(409, str(err)) from None
    return {"activity_id": target, **report_links(int(import_id))}


@app.get("/api/imports")
def reports():
    return [{**row, **report_links(row["id"])} for row in app.state.store.reports()]


@app.get("/api/imports/{import_id}")
def report(import_id: str):
    row = app.state.store.report(import_id)
    if row is None:
        raise HTTPException(404, "Import not found.")
    return {**row, **report_links(row["id"])}


@app.get("/api/activities/{activity_id}")
def activity(activity_id: str):
    row = app.state.store.activity(activity_id)
    if row is None:
        raise HTTPException(404, "Activity not found.")
    return row


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/imports")


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request):
    return views.imports(reports(), request.query_params.get("from", ""), request.query_params.get("to", ""),
                         app.state.store.pending())


@app.get("/imports/{import_id}", response_class=HTMLResponse)
def report_page(import_id: str):
    return views.report(report(import_id))


@app.get("/activities/{activity_id}", response_class=HTMLResponse)
def detail_page(activity_id: str):
    return views.activity(activity(activity_id))
