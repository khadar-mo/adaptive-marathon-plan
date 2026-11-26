"""One loopback FastAPI process for local running history."""
from contextlib import asynccontextmanager
from datetime import date
import os
from pathlib import Path
import sqlite3
import stat
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fit_import import index_originals, run_fit_import, stage_uploads
from fit_store import ResolutionError
from garmin_import import run_import
import garmin_coverage
import history
import canonical
import effort_api
import estimator_api
import recording
import provider_modes
import provider_views
from storage import ActiveImport, Archive, Store
import views
from context_web import router as context_router
from fueling_web import router as fueling_router
from workout_web import router as workout_router


@asynccontextmanager
async def lifespan(app):
    path = Path(os.environ.get("RUNS_DB_PATH", "runs.sqlite3"))
    app.state.archive = Archive(os.environ.get("RUNS_ARCHIVE_PATH", path.with_name("runs-archive")))
    app.state.store = Store(path, app.state.archive)
    try:
        app.state.store.interrupt_unfinished()
        index_originals(app.state.store, app.state.archive)
    except (OSError, sqlite3.Error):
        # Keep /health reachable when storage is unreadable.
        pass
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(context_router)
app.include_router(fueling_router)
app.include_router(workout_router)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
effort_api.register(app)
estimator_api.register(app)


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
        with app.state.store.connect(timeout=1) as db:
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


class ProviderConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["GARMIN_ONLY", "STRAVA_ONLY", "BOTH", "GARMIN_PRIMARY_FALLBACK"]
    expected_revision: int = Field(ge=0, le=2**63 - 1, strict=True)


@app.get("/api/providers")
def provider_status():
    return provider_modes.status(app.state.store)


@app.post("/api/providers/config")
def provider_configuration(choice: ProviderConfiguration):
    try:
        config = provider_modes.configure(app.state.store, choice.mode, choice.expected_revision)
    except provider_modes.StaleConfiguration as exc:
        raise HTTPException(409, str(exc)) from None
    return {"config": config, "html_url": "/providers"}


@app.post("/api/providers/import", status_code=202)
def provider_import(dates: DateRange, background: BackgroundTasks):
    try:
        attempt_id, import_id = provider_modes.start(app.state.store, dates.first, dates.last)
    except ActiveImport as active:
        return JSONResponse({"detail": "An import is already running.", **report_links(active.import_id)}, status_code=409)
    if import_id is not None:
        background.add_task(provider_modes.run, app.state.store, app.state.archive, attempt_id, import_id)
    return {"id": attempt_id, "attempt_url": f"/api/provider-attempts/{attempt_id}",
            "html_url": f"/providers?attempt={attempt_id}"}


@app.post("/api/imports/strava")
def strava_import(dates: DateRange):
    return JSONResponse({"detail": provider_modes.POLICY["reason"], **provider_modes.strava_analytics()}, status_code=403)


@app.get("/api/provider-attempts/{attempt_id}")
def provider_attempt(attempt_id: str):
    result = provider_modes.read_attempt(app.state.store, attempt_id)
    if result is None:
        raise HTTPException(404, "Source import attempt not found.")
    return result


@app.get("/providers", response_class=HTMLResponse)
def providers_page(attempt: str | None = None):
    return provider_views.page(provider_status(), provider_attempt(attempt) if attempt is not None else None)


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
    return {**row, **history.measurements(row),
            "laps": [{**lap, **history.measurements(lap)} for lap in row["laps"]]}


def read_recording(activity_id, source, offset, limit):
    try:
        row = recording.read(app.state.store, activity_id, source, offset=offset, limit=limit)
    except recording.UnknownRecording as exc:
        raise HTTPException(404, str(exc)) from None
    if row is None:
        raise HTTPException(404, "Activity not found.")
    return row


@app.get("/api/activities/{activity_id}/recording")
def recording_json(activity_id: str, source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   offset: int = Query(0, ge=0, le=2**31 - 1), limit: int = Query(200, ge=1, le=500)):
    return read_recording(activity_id, source, offset, limit)


@app.get("/activities/{activity_id}/recording", response_class=HTMLResponse)
def recording_page(activity_id: str, source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   offset: int = Query(0, ge=0, le=2**31 - 1), limit: int = Query(200, ge=1, le=500)):
    return views.recording_page(read_recording(activity_id, source, offset, limit))


class Recompute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,80}$")
    config: dict | None = None


@app.post("/api/canonical/recompute")
def recompute_all(choice: Recompute):
    try:
        return {"activities": canonical.recompute(app.state.store, config_version=choice.config_version, config=choice.config)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.post("/api/activities/{activity_id}/canonical/recompute")
def recompute_activity(activity_id: str, choice: Recompute):
    from storage import local_id

    if local_id(activity_id) is None:
        raise HTTPException(404, "Activity not found.")
    try:
        return {"activities": canonical.recompute(app.state.store, [int(activity_id)],
                                                  config_version=choice.config_version, config=choice.config)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "Activity not found.") from None


def read_canonical(activity_id, version, source, offset, limit):
    try:
        row = canonical.read(app.state.store, activity_id, version, source, offset=offset, limit=limit)
    except canonical.UnknownVersion as exc:
        raise HTTPException(404, str(exc)) from None
    if row is None:
        raise HTTPException(404, "Activity not found.")
    return row


@app.get("/api/activities/{activity_id}/canonical")
def canonical_json(activity_id: str, version: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   offset: int = Query(0, ge=0, le=2**31 - 1), limit: int = Query(200, ge=1, le=500)):
    return read_canonical(activity_id, version, source, offset, limit)


@app.get("/activities/{activity_id}/canonical", response_class=HTMLResponse)
def canonical_page(activity_id: str, version: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                   offset: int = Query(0, ge=0, le=2**31 - 1), limit: int = Query(200, ge=1, le=500)):
    return views.canonical_page(read_canonical(activity_id, version, source, offset, limit))


def history_dates(request):
    query = request.query_params
    if len(query.multi_items()) != len(query):
        raise ValueError("Supply each history parameter once.")
    if "from" in query or "to" in query:
        try:
            dates = DateRange.model_validate(dict(query))
        except ValueError:
            raise ValueError("Enter From and To as YYYY-MM-DD, with From on or before To.") from None
        return dates.first, dates.last
    unit = query.get("period", "month")
    if set(query) - {"period", "date"} or unit not in ("week", "month"):
        raise ValueError("Choose a week or month and a calendar date.")
    anchor = garmin_coverage.today()
    if "date" in query:
        try:
            anchor = date.fromisoformat(query["date"])
            if anchor.isoformat() != query["date"]:
                raise ValueError
        except ValueError:
            raise ValueError("Enter a calendar date as YYYY-MM-DD.") from None
    first, last = history.calendar_period(anchor, unit)
    return first.isoformat(), last.isoformat()


@app.get("/api/history")
def history_json(request: Request):
    try:
        first, last = history_dates(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return history.read(app.state.store, first, last)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    try:
        first, last = history_dates(request)
    except ValueError as exc:
        return HTMLResponse(views.history_error(str(exc)), status_code=422)
    return views.history_page(history.read(app.state.store, first, last))


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/history")


@app.get("/favicon.ico", include_in_schema=False, status_code=204)
def favicon():
    # Chrome requests this when the runner opens a JSON document directly.
    return Response(status_code=204)


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
