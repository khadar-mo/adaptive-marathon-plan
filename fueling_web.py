"""Activity-linked entry and exact revision inspection for optional fueling history."""
import json
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

import context_data as context
from context_web import bounded_body, e, input_field, invalid, select, store, SCRIPT
import fueling
from garmin_activity import RUN_TYPES
import views

router = APIRouter()


@router.get("/api/activities/{activity_id}/fueling")
def read(request: Request, activity_id: int = Path(ge=1, le=context.MAX_SQLITE_INTEGER), as_of: str | None = None):
    try:
        return fueling.read(store(request), activity_id, as_of=as_of or None)
    except ValueError as exc:
        invalid(exc)


@router.post("/api/activities/{activity_id}/fueling")
async def enter(request: Request, activity_id: int = Path(ge=1, le=context.MAX_SQLITE_INTEGER)):
    try:
        raw = await bounded_body(request)
        rows = context.parse(raw, "json")
        with store(request).history() as db:
            if db is None:
                raise ValueError("Activity not found.")
            for row in rows:
                # Validate before binding values to SQLite; retain the original raw bytes.
                record = context.Fueling.model_validate(row)
                if context.owner(db, record.activity_id) != context.owner(db, activity_id):
                    raise ValueError("The entry must reference this activity or its recorded identity redirect.")
        result = context.ingest(store(request), raw)
        return {**result, "html_url": f"/activities/{activity_id}/fueling"}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/fueling/versions/{version_id}")
def revision(request: Request, version_id: str = Path(pattern=r"^[a-f0-9]{64}$"), as_of: str | None = None):
    try:
        row = fueling.revision(store(request), version_id, as_of=as_of or None)
    except ValueError as exc:
        invalid(exc)
    if row is None:
        raise HTTPException(404, "Fueling revision not found at this cutoff.")
    return row


STYLE = """<style>
.context-entry {display:grid;grid-template-columns:minmax(0,1fr);gap:.8rem;border:1px solid #bccbc2;padding:1rem}
.context-entry > * {min-width:0;max-width:100%} textarea {box-sizing:border-box;width:100%;font:inherit}
pre,.runner-text {white-space:pre-wrap;overflow-wrap:anywhere} .result {color:#753a13}
</style>"""


def form(activity_id, row=None):
    row = row or {}
    def field(name, label, default="", **kw):
        return input_field("fueling", name, label, row.get(name, default), **kw)
    original = {k: row[k] for k in ("user_strategy", "tolerance", "notes") if isinstance(row.get(k), str)}
    body = f'<form class="context-entry" data-kind="fueling" data-original-text="{e(json.dumps(original, ensure_ascii=False))}" action="/api/activities/{activity_id}/fueling">'
    body += f'<input type="hidden" name="activity_id" value="{e(row.get("activity_id", activity_id))}">'
    if row:
        # Source identity and target are immutable; a correction only changes its report.
        body += f'<input type="hidden" name="source" value="{e(row["source"])}"><input type="hidden" name="source_id" value="{e(row["source_id"])}">'
        body += f'<input type="hidden" name="policy_version" value="{e(row["policy_version"])}"><input type="hidden" name="supersedes" value="{row["version_id"]}">'
        body += f'<p>Correcting {e(row["source"])} / {e(row["source_id"])} · revision {row["revision"]}. The previous revision remains available.</p>'
        body += field("correction_reason", "Correction reason", required=True)
    else:
        body += field("source", "Source", "self-report", required=True)
        body += field("source_id", "Source record ID", "fueling-" + uuid4().hex, required=True,
                      hint="Keep this ID when replaying an import or correcting this report.")
    body += field("actor", "Reported by", "runner", required=True, hint="The supplied author label; this local app does not authenticate this identity.")
    body += field("reported_at", "Report and notes timestamp", required=True, hint="ISO time with offset, for example 2026-10-25T13:00:00+00:00. Server receipt time is recorded separately.")
    body += field("observed_at", "Practice observation time", hint="Optional ISO time with offset. Leave an unknown observation time blank.")
    body += select("fueling", "practice_status", "Practice status", [(v, v) for v in ("UNKNOWN", "NOT_PLANNED", "OFFERED", "PLANNED", "COMPLETED")], row.get("practice_status", "UNKNOWN"))
    body += textarea("fueling", "user_strategy", "Your food / drink strategy", row.get("user_strategy"), "Optional. Your own wording, quantities and timing are saved verbatim.")
    body += textarea("fueling", "tolerance", "GI tolerance / symptoms", row.get("tolerance"), "Optional self-report. Blank remains UNKNOWN; it does not mean good or poor tolerance.")
    body += field("tolerance_reported_at", "Tolerance report timestamp", hint="Required with GI tolerance / symptoms. ISO time with offset, no later than the report timestamp above.")
    body += textarea("fueling", "notes", "Notes", row.get("notes"), "Saved verbatim with the report timestamp above.")
    body += field("source_updated_at", "Original source update time", hint="Optional source timestamp with offset. It cannot backdate receipt or correction availability.")
    body += f'<button>{"Save fueling correction" if row else "Save fueling practice"}</button><p role="status" class="result"></p></form>'
    return body


def textarea(prefix, name, label, value, hint):
    identity = f"{prefix}-{name}"
    # HTML removes one leading newline after the opening textarea tag. Supply
    # that newline separately so a runner's own leading newline survives editing.
    return (f'<div><label for="{identity}">{e(label)}</label><textarea id="{identity}" name="{name}" rows="3" '
            f'aria-describedby="{identity}-hint">\n{e(value)}</textarea><small id="{identity}-hint">{e(hint)}</small></div>')


def report_card(row):
    body = f'<article><h2>{e(row["source"])} / {e(row["source_id"])} · revision {row["revision"]}</h2><dl>'
    for label, value in (("Practice status", row["practice_status"]), ("GI tolerance status", row["tolerance_status"]),
                         ("Your food / drink strategy", row["user_strategy"]), ("GI tolerance / symptoms", row["tolerance"]),
                         ("Tolerance reported at", row["tolerance_reported_at"]), ("Notes", row["notes"]),
                         ("Reported by", row["actor"]), ("Report and notes timestamp", row["reported_at"]),
                         ("Practice observation time", row["observed_at"] or "UNKNOWN"), ("First received", row["first_known_at"]),
                         ("Revision received", row["known_at"]), ("Research status", row["research_status"])):
        if value is not None:
            body += f'<dt>{e(label)}</dt><dd class="runner-text">{e(value)}</dd>'
    body += f'</dl><p><a href="/fueling/versions/{row["version_id"]}">Inspect exact revision</a> · <a href="/context/records/{row["record_id"]}">All revisions and sources</a></p></article>'
    return body


@router.get("/activities/{activity_id}/fueling", response_class=HTMLResponse)
def page(request: Request, activity_id: int = Path(ge=1, le=context.MAX_SQLITE_INTEGER),
         as_of: str | None = None, edit: str | None = None):
    data = read(request, activity_id, as_of)
    selected = None
    if edit:
        current = read(request, activity_id)
        selected = next((r for r in current["reports"] + current["withheld"] if r["record_id"] == edit), None)
        if selected is None:
            raise HTTPException(404, "Fueling record not found for this activity.")
    json_url = f'/api/activities/{activity_id}/fueling?' + urlencode({'as_of': data['as_of']})
    body = STYLE + f'<p><a href="/activities/{activity_id}">Back to activity {activity_id}</a> · <a href="{e(json_url)}">Fueling JSON</a></p>'
    body += '<p>OPTIONAL · RESEARCH_PENDING. Record your own practice and tolerance. No intake quantities, timing, readiness or fitness decisions are generated here.</p>'
    body += f'<p>Report state: {e(data["state"])}. Practice: {e(data["practice_status"])}. Tolerance: {e(data["tolerance_status"])}.</p>'
    if data["missing_reason"]:
        body += '<p>' + e(data["missing_reason"]) + '</p>'
    body += '<form method="get">' + input_field("cutoff", "as_of", "As-of timestamp", as_of, hint="Optional ISO timestamp with offset. Reads only revisions known and reported at that time.") + '<button>Inspect fueling cutoff</button></form>'
    for row in data["reports"]:
        body += report_card(row) + f'<p><a href="/activities/{activity_id}/fueling?edit={row["record_id"]}">Correct fueling record</a></p>'
    if data["withheld"]:
        body += f'<p>{len(data["withheld"])} known reports withheld because their report time is after this cutoff.</p>'
    if selected or data["target_now"]["sport"] in RUN_TYPES:
        body += '<h2>' + ('Correct fueling report' if selected else 'Record optional practice') + '</h2>' + form(activity_id, selected)
    else:
        body += '<p>This activity is currently classified as non-running. Existing fueling history remains inspectable; new entries require a recorded run.</p>'
    return views.page("Fueling practice and tolerance", body + SCRIPT)


@router.get("/fueling/versions/{version_id}", response_class=HTMLResponse)
def revision_page(request: Request, version_id: str = Path(pattern=r"^[a-f0-9]{64}$")):
    row = revision(request, version_id)
    body = STYLE + f'<p><a href="/activities/{row["activity_id"]}/fueling">Back to activity fueling history</a></p>'
    body += '<p>This saved revision retains its original target. Current target metadata is labelled separately and is not historical evidence.</p>'
    body += report_card(row) + '<h2>Revision and source provenance</h2><pre>' + e(json.dumps(row, indent=2, ensure_ascii=False)) + '</pre>'
    return views.page("Fueling exact revision", body)
