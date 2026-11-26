"""Read-only local source-history inspection."""
from html import escape
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

import canonical
import source_history
import views
import workload_inputs as inputs
from workload_web import call


router = APIRouter()
LocalID = Annotated[int, Path(gt=0, lt=2**63)]


def inspect(request, activity_id, cutoff):
    now = inputs.utc(inputs.now())
    known = now if cutoff is None else call(inputs.utc, cutoff)
    if known > now:
        raise HTTPException(422, "Knowledge cutoff cannot be in the future.")
    with request.app.state.store.history() as db:
        return source_history.read_in_db(db, known, activity_id=activity_id)


@router.get("/api/activities/{activity_id}/source-history")
def activity_history(request: Request, activity_id: LocalID, knowledge_cutoff: str | None = None):
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


def receipt_text(receipt):
    reason = escape(receipt["basis"].replace("_", " ").lower())
    text = f'{escape(receipt["known_at"])} · {reason}'
    if receipt.get("import_id") is not None:
        iid = int(receipt["import_id"])
        text += f' · <a href="/imports/{iid}">Import {iid}</a>'
    if receipt["basis"] in ("CANONICAL_VERSION", "CANONICAL_SELECTION"):
        version = receipt.get("version_id", receipt["id"])
        text += f' · <a href="/api/canonical/versions/{escape(version)}">Stored canonical version</a>'
    return text


@router.get("/activities/{activity_id}/source-history", response_class=HTMLResponse)
def activity_history_page(request: Request, activity_id: LocalID, knowledge_cutoff: str | None = None):
    data = inspect(request, activity_id, knowledge_cutoff)
    cutoff = data["knowledge_cutoff"]
    query = urlencode({"knowledge_cutoff": cutoff})
    content = (f'<p><a href="/activities/{activity_id}">Current activity {activity_id}</a> · '
               f'<a href="/api/activities/{activity_id}/source-history?{escape(query)}">Source history JSON</a></p>'
               f'<form method="get"><label>Knowledge cutoff (ISO time with offset) '
               f'<input name="knowledge_cutoff" value="{escape(cutoff)}"></label> <button>Inspect cutoff</button></form>')
    if data["missing_reason"]:
        content += '<p>' + escape(data["missing_reason"]) + '</p>'
    for value in data["groups"]:
        members = ", ".join(map(str, value["member_ids"]))
        providers = ", ".join(p["provider"] + " " + str(p["id"]) for p in (value["provider_ids"] or []))
        provider_text = providers or ("Unknown at this cutoff" if value["provider_ids"] is None else "None recorded")
        content += (f'<h2>Activity {value["owner_id"]}</h2><dl><dt>Local activity IDs</dt><dd>{members}</dd>'
                    f'<dt>Provider IDs</dt><dd>{escape(provider_text)}</dd><dt>Reported activity start</dt>'
                    f'<dd>{escape(value["claimed_event_start_utc"] or "Unavailable")}</dd></dl>')
        for s in value["sources"]:
            content += (f'<h3>{escape(s["kind"])} · {escape(s["format"])}</h3><details><summary>Checksum and evidence</summary>'
                        f'<dl><dt>SHA-256</dt><dd>{escape(s["sha256"])}</dd><dt>Claimed acquisition time</dt>'
                        f'<dd>{escape(s["claimed_acquired_at"] or "Unavailable")}</dd><dt>First evidence of this source</dt>'
                        f'<dd>{receipt_text(s["first_source_evidence"])}</dd><dt>First evidence for this owner</dt>'
                        f'<dd>{receipt_text(s["first_owner_evidence"])}</dd></dl></details>')
    if data["group_history"]:
        content += '<h2>Ownership observations</h2><ol>'
        for item in data["group_history"]:
            group = item["group"]
            members = ", ".join(map(str, group["member_ids"]))
            content += (f'<li>Owner {group["owner_id"]} · local IDs {members}<br>{receipt_text(item["evidence"])}</li>')
        content += '</ol>'
    if data["review_events"]:
        content += '<h2>Identity review history</h2><ol>'
        for item in data["review_events"]:
            action = escape(item["action"].replace("_", " ").lower())
            content += f'<li>{action} · {receipt_text(item)}</li>'
        content += '</ol>'
    return views.page("Source and identity history", content)
