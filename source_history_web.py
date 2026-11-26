"""Read-only local source-history inspection."""
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import canonical
import source_history
import views
import workload_inputs as inputs
from workload_web import call

router = APIRouter()


def inspect(request, activity_id, cutoff):
    with request.app.state.store.history() as db:
        return call(source_history.read_in_db, db, cutoff or inputs.now(), activity_id=activity_id)


@router.get("/api/activities/{activity_id}/source-history")
def activity_history(request: Request, activity_id: int, knowledge_cutoff: str | None = None):
    return inspect(request, activity_id, knowledge_cutoff)


@router.get("/api/source-history")
def source_history_json(request: Request, knowledge_cutoff: str | None = None):
    return inspect(request, None, knowledge_cutoff)


@router.get("/api/canonical/versions/{version_id}")
def immutable_version(request: Request, version_id: str):
    with request.app.state.store.history() as db:
        version = canonical.load_version(db, version_id)
    if version is None:
        raise HTTPException(404, "Canonical version not found.")
    return version


@router.get("/activities/{activity_id}/source-history", response_class=HTMLResponse)
def activity_history_page(request: Request, activity_id: int, knowledge_cutoff: str | None = None):
    data = inspect(request, activity_id, knowledge_cutoff)
    cutoff = data["knowledge_cutoff"]
    query = urlencode({"knowledge_cutoff": cutoff})
    content = (f'<p><a href="/activities/{activity_id}">Activity {activity_id}</a> · '
               f'<a href="/api/activities/{activity_id}/source-history?{escape(query)}">Source history JSON</a></p>'
               f'<form method="get"><label>Knowledge cutoff (ISO time with offset) '
               f'<input name="knowledge_cutoff" value="{escape(cutoff)}"></label> <button>Inspect cutoff</button></form>')
    if data["missing_reason"]:
        content += '<p>' + escape(data["missing_reason"]) + '</p>'
    for value in data["groups"]:
        members = ", ".join(map(str, value["member_ids"]))
        providers = ", ".join(p["provider"] + " " + str(p["id"]) for p in (value["provider_ids"] or []))
        content += f'<h2>Activity {value["owner_id"]}</h2><p>Local activity IDs {members}. Provider IDs {escape(providers or "unavailable")}.</p>'
        for s in value["sources"]:
            proof = s["first_source_evidence"]
            content += (f'<h3>{escape(s["kind"])} · {escape(s["format"])}</h3><details><summary>Checksum and evidence</summary>'
                        f'<p>{escape(s["sha256"])}</p><p>First observed by {escape(proof["known_at"])} using '
                        f'{escape(proof["basis"])}. First proof for this owner {escape(s["first_owner_evidence"]["known_at"])}.</p></details>')
    return views.page("Source and identity history", content)
