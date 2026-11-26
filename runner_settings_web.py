"""Local APIs and runner-facing race goal and training preferences."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from availability_web import e, invalid, reject_constant, unique_object
import runner_settings as settings
import views


router = APIRouter()


@router.get("/api/runner-settings/{kind}")
def current(request: Request, kind: settings.Kind, as_of: str | None = None):
    try:
        return settings.read(request.app.state.store, kind, as_of=as_of)
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/runner-settings/{kind}/versions/{version_id}")
def version(request: Request, kind: settings.Kind, version_id: str, as_of: str | None = None):
    try:
        result = settings.read(request.app.state.store, kind, version_id=version_id, as_of=as_of)
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    if result is None:
        raise HTTPException(404, "Settings revision unavailable at this cutoff.")
    return result


@router.post("/api/runner-settings/{kind}")
async def submit(request: Request, kind: settings.Kind):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 256 * 1024:
            raise HTTPException(413, "Settings submissions are limited to 256 KiB.")
        body.extend(chunk)
    try:
        raw = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
        result = settings.save(request.app.state.store, kind, raw)
    except settings.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    except RecursionError:
        raise HTTPException(422, "Settings JSON is nested too deeply.") from None
    return {**result, "html_url": "/runner-settings",
            "version_url": f"/api/runner-settings/{kind}/versions/{result['record']['version_id']}"}


def duration(seconds):
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


@router.get("/runner-settings", response_class=HTMLResponse)
def page(request: Request, as_of: str | None = None):
    goal = current(request, "goal", as_of)["current"]
    body = (f'<h2>Marathon goal</h2><p><strong>{duration(goal["target_duration_s"])}</strong> on '
            f'{e(goal["race_local_date"])}. Optional review {e(goal["review_date"])}.</p>'
            f'<p>{e(goal["source"])}</p><p><a href="/availability">Weekly availability</a></p>')
    return views.page("Runner settings", body)
