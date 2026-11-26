"""Workout template library, revision inspection and authenticated-local-origin form handling."""
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from availability_web import e, invalid, reject_constant, unique_object
import views
import workout_templates as templates


router = APIRouter()


async def document(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2 * 1024 * 1024:
            raise HTTPException(413, "A template submission can contain up to 2 MiB.")
        body.extend(chunk)
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except RecursionError:
        raise HTTPException(422, "Template JSON is nested too deeply.") from None


@router.get("/api/workout-templates")
def library(request: Request, as_of: str | None = None):
    try:
        with request.app.state.store.history() as db:
            return {"templates": templates.index(db, as_of=as_of), "as_of": as_of}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


def lookup(db, key, version_id, as_of):
    record = templates.read_version(db, key, version_id=version_id, as_of=as_of)
    if record is None:
        raise HTTPException(404, "Workout template revision unavailable at this cutoff.")
    return record


@router.get("/api/workout-templates/{key}")
def current(request: Request, key: templates.Key, as_of: str | None = None):
    try:
        with request.app.state.store.history() as db:
            return {"record": lookup(db, key, None, as_of), "versions": templates.index(db, template_id=key, as_of=as_of)}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/workout-templates/{key}/versions/{version_id}")
def version(request: Request, key: templates.Key, version_id: templates.Hash, as_of: str | None = None):
    try:
        with request.app.state.store.history() as db:
            return lookup(db, key, version_id, as_of)
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.post("/api/workout-templates/{key}")
async def save(request: Request, key: templates.Key):
    try:
        result = templates.save(request.app.state.store, key, await document(request))
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    return {**result, "html_url": "/workout-templates/" + key,
            "version_url": f"/api/workout-templates/{key}/versions/{result['record']['version_id']}"}


@router.post("/api/workout-templates/{key}/versions/{version_id}/preview")
async def preview(request: Request, key: templates.Key, version_id: templates.Hash, as_of: str | None = None):
    try:
        choice = templates.Adjustments.model_validate(await document(request))
        record = version(request, key, version_id, as_of)
        return {"template_id": key, "version_id": version_id,
                "accounting": templates.account(record["blocks"], choice.distances_m)}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


def label(value):
    return value.replace("_", " ").title()


def options(values):
    return "".join(f'<option value="{e(v)}">{e(label(v))}</option>' for v in values)


def field(name, title, *, required=False, whole=False):
    attr = ' required' if required else ''
    if whole:
        attr += ' type="number" min="0" max="2147483647" step="1"'
    return f'<label>{title}<input name="{name}"{attr}></label>'


def text_field(name, title, *, required=False):
    return f'<label>{title}<textarea name="{name}" rows="2"{" required" if required else ""}></textarea></label>'


def block_editor():
    return ('<fieldset class="dose-block"><legend>Block</legend>'
            '<label>Block type<select name="kind">' + options(templates.KINDS) + '</select></label>'
            + field('distance_m', 'Distance (metres)', required=True, whole=True)
            + text_field('intensity', 'Intended intensity', required=True)
            + field('intensity_method', 'Intensity method', required=True)
            + '<label><input type="checkbox" name="protected"> Protect this block</label>'
            + field('rest_after_s', 'Stationary rest after this block (seconds)')
            + '<details><summary>Easy-volume adjustments</summary>'
            + field('min_m', 'Minimum metres', whole=True) + field('max_m', 'Maximum metres', whole=True)
            + '<label><input type="checkbox" name="allow_split"> Allow this easy block to split</label></details>'
            + '<details><summary>Pace evidence</summary><label><input type="checkbox" name="use_pace"> Include supplied pace bounds</label>'
            + '<div class="pace-evidence">' + field('pace_lower', 'Fastest pace', required=True)
            + field('pace_upper', 'Slowest pace', required=True)
            + '<label>Pace unit<select name="pace_unit"><option>s/km</option><option>min/km</option><option>s/m</option></select></label>'
            + field('pace_method', 'Pace method') + text_field('pace_source_ref','Pace source reference')
            + text_field('pace_calibration', 'Pace calibration reference')
            + field('pace_evidence_known_at','Evidence known at (ISO timestamp with UTC offset)')
            + text_field('pace_uncertainty','Pace uncertainty')
            + '<label>Evidence status<select name="pace_status"><option>UNVERIFIED</option><option>SUPPORTED</option></select></label></div></details>'
            + '<details><summary>Intensity zone</summary><label><input type="checkbox" name="use_zone"> Record a zone in its own method</label>'
            + '<div class="zone-evidence">' + field('zone_method','Zone method', required=True)
            + field('zone_band','Band in that method', required=True) + text_field('zone_calibration','Zone calibration reference')
            + '</div></details><p><button type="button" class="up">Move up</button> <button type="button" class="down">Move down</button> '
            + '<button type="button" class="remove">Remove block</button></p></fieldset>')


def editor(key, record=None):
    initial = {k: record[k] for k in ("name", "primary", "role", "tags", "blocks", "version_id")} if record else {}
    initial_json = json.dumps(initial, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    return ('<p id="template-loading" role="status">Loading the workout template editor…</p>'
            f'<form id="template-editor" action="/api/workout-templates/{key}" method="post" hidden>'
            + field("name","Template name",required=True)
            + '<label>Primary purpose<select name="primary">' + options(templates.CATEGORIES) + '</select></label>'
            + '<label>Intended role<select name="role">' + options(templates.ROLES) + '</select></label>'
            + '<label>Secondary tags (JSON list)<textarea name="tags" rows="2">[]</textarea></label>'
            + '<p>Enter each work repetition and active recovery in execution order. Stationary rest uses seconds.</p>'
            + '<div id="ordered-blocks"></div><button type="button" id="add-block">Add block</button>'
            + field("actor","Recorded by",required=True) + text_field("reason","Reason for this revision",required=True)
            + '<button type="submit">Save workout template</button><p role="status"></p></form>'
            + '<template id="block-editor">' + block_editor() + '</template>'
            + f'<script type="application/json" id="template-values">{initial_json}</script>'
            + '<script src="/assets/workout-template-editor.js" defer></script>')


@router.get("/assets/workout-template-editor.js", include_in_schema=False)
def script():
    return FileResponse(Path(__file__).with_name("workout_template_editor.js"), media_type="application/javascript")


@router.get("/workout-templates", response_class=HTMLResponse)
def page(request: Request, as_of: str | None = None):
    suffix = "?" + urlencode({"as_of": as_of}) if as_of else ""
    items = library(request, as_of)["templates"]
    links = "".join(f'<li><a href="/workout-templates/{r["template_id"]}{e(suffix)}">{e(r["name"])}</a> · revision {r["revision"]}</li>' for r in items)
    content = "<ul>" + links + "</ul>" if links else "<p>No saved workout templates at this time.</p>"
    if as_of is None:
        content += "<h2>Create a template</h2>" + editor(uuid4().hex)
    return views.page("Workout templates", content)


def block_summary(block, calculated):
    dose = [f'{calculated["min_m"]:,}–{calculated["max_m"]:,} m permitted',
            f'{e(block["rest_after_s"])} s stationary rest']
    if block["protected"]:
        dose.append("Protected")
    if block["allow_split"]:
        dose.append("Easy-volume split allowed")
    body = (f'<strong>{e(label(block["kind"]))} · {block["distance_m"]:,} m</strong><br>'
            f'{e(block["intensity"])} · {e(block["intensity_method"])}<br>' + " · ".join(dose))
    pace = block["pace"]
    if pace:
        body += (f'<details><summary>Pace {e(pace["lower"])}–{e(pace["upper"])} {e(pace["unit"])}</summary>'
                 f'<dl><dt>Runner-declared evidence</dt><dd>{e(pace["status"])}</dd>')
        for key, title in (("method", "Method"), ("source_ref", "Source"), ("calibration", "Calibration"),
                           ("evidence_known_at", "Known at"), ("uncertainty", "Uncertainty")):
            body += f'<dt>{title}</dt><dd>{e(pace[key] or "Unspecified")}</dd>'
        body += '</dl></details>'
    zone = block["zone"]
    if zone:
        body += (f'<p>{e(zone["method"])} · {e(zone["band"])} · {e(calculated["zone_status"])}'
                 f'<br>Calibration · {e(zone["calibration"] or "Unspecified")}</p>')
    else:
        body += '<p>ZONE_UNVERIFIED · No zone method supplied.</p>'
    return '<li>' + body + '</li>'


@router.get("/workout-templates/{key}", response_class=HTMLResponse)
def detail(request: Request, key: templates.Key, version_id: templates.Hash | None = None, as_of: str | None = None):
    state = current(request, key, as_of)
    r = version(request, key, version_id, as_of) if version_id else state["record"]
    data = r["accounting"]
    suffix = "?" + urlencode({"as_of": as_of}) if as_of else ""
    content = (f'<p><a href="/workout-templates{e(suffix)}">Workout templates</a></p>'
               f'<p>{e(label(r["primary"]))} · {e(label(r["role"]))} · revision {r["revision"]}</p>'
               f'<p><strong>{data["total_m"]:,} m total</strong> · {data["work_m"]:,} m work · {data["easy_m"]:,} m easy</p>'
               f'<p>Stationary rest {e(data["rest_s"])} s · {e(data["duration_status"])}</p>')
    if r["tags"]:
        content += '<p>Secondary tags</p><ul>' + "".join(f'<li>{e(tag)}</li>' for tag in r["tags"]) + '</ul>'
    if data["duration_upper_s"] is not None:
        content += f'<p>Supplied duration range {e(data["duration_lower_s"])}–{e(data["duration_upper_s"])} seconds, including rest.</p>'
    else:
        content += f'<p>{len(data["missing_pace_block_ids"])} running blocks lack supported pace evidence.</p>'
    content += '<ol>' + "".join(block_summary(b, a) for b, a in zip(r["blocks"], data["blocks"])) + '</ol>'
    content += f'<p>Recorded by {e(r["actor"])} at {e(r["known_at"])} · {e(r["reason"])}</p>'
    content += f'<p><a href="/api/workout-templates/{key}/versions/{r["version_id"]}{e(suffix)}">Exact revision JSON</a></p><h2>Revision history</h2><ul>'
    for older in state["versions"]:
        query = {"version_id": older["version_id"]}
        if as_of:
            query["as_of"] = as_of
        content += f'<li><a href="/workout-templates/{key}?{e(urlencode(query))}">Revision {older["revision"]} · {e(older["known_at"])}</a></li>'
    content += '</ul>'
    if version_id is None and as_of is None:
        content += "<h2>Edit this template</h2>" + editor(key, r)
    return views.page(r["name"], content)
