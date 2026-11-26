"""Local API and accessible entry/inspection forms for optional context inputs."""
from html import escape
import json
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

import context_data as context
import views

router = APIRouter()


def invalid(exc):
    if isinstance(exc, context.Conflict):
        raise HTTPException(409, str(exc)) from None
    if isinstance(exc, ValidationError):
        messages = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
        raise HTTPException(422, "; ".join(messages)) from None
    raise HTTPException(422, str(exc)) from None


def store(request):
    return request.app.state.store


async def bounded_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, "Context inputs are limited to 2 MiB.")
    return bytes(body)


@router.post("/api/context/records")
async def enter(request: Request):
    try:
        result = context.ingest(store(request), await bounded_body(request))
        return {**result, "html_url": "/context"}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.post("/api/context/import")
async def upload(request: Request, file: UploadFile, format: str = "json"):
    raw = await file.read(2 * 1024 * 1024 + 1)
    try:
        result = context.ingest(store(request), raw, format)
        return {**result, "html_url": "/context"}
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/context")
def data(request: Request, as_of: str | None = None, first: str | None = None, last: str | None = None,
         policy_version: str = context.DEFAULT_POLICY):
    try:
        return context.read(store(request), as_of=as_of or None, first=first or None, last=last or None, policy_version=policy_version)
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/context/events")
def event_log(request: Request, after: int = Query(0, ge=0, le=context.MAX_SQLITE_INTEGER),
              limit: int = Query(200, ge=1, le=1000), as_of: str | None = None):
    try:
        return context.event_log(store(request), after=after, limit=limit, as_of=as_of)
    except ValueError as exc:
        invalid(exc)


@router.get("/api/context/records/{record_id}")
def history(request: Request, record_id: str, as_of: str | None = None):
    try:
        value = context.history(store(request), record_id, as_of=as_of)
    except ValueError as exc:
        invalid(exc)
    if value is None:
        raise HTTPException(404, "Context record not found.")
    return value


@router.get("/api/context/feedback")
def feedback(request: Request, activity_id: int | None = Query(None, ge=1, le=context.MAX_SQLITE_INTEGER),
             week: str | None = None, as_of: str | None = None):
    try:
        return context.target_feedback(store(request), activity_id=activity_id, week=week, as_of=as_of)
    except ValueError as exc:
        invalid(exc)


@router.get("/api/context/definitions")
def definitions(request: Request):
    return context.definitions(store(request))


@router.post("/api/context/definitions/{kind}/{version}")
async def definition(request: Request, kind: str, version: str):
    try:
        rows = context.parse(await bounded_body(request), "json")
        if len(rows) != 1:
            raise ValueError("Supply one definition.")
        return {**context.register_definition(store(request), kind, version, rows[0]), "html_url": "/context"}
    except ValueError as exc:
        invalid(exc)


def e(value):
    return escape("" if value is None else str(value), quote=True)


def input_field(prefix, name, label, value="", *, required=False, kind="text", hint=None):
    identity = f"{prefix}-{name}"
    described = f' aria-describedby="{identity}-hint"' if hint else ''
    element = f'<input id="{identity}" name="{name}" type="{kind}" value="{e(value)}"{described}{" required" if required else ""}>'
    return f'<div><label for="{identity}">{e(label)}</label>{element}' + (f'<small id="{identity}-hint">{e(hint)}</small>' if hint else "") + '</div>'


def select(prefix, name, label, options, value):
    return f'<div><label for="{prefix}-{name}">{e(label)}</label><select id="{prefix}-{name}" name="{name}">' + ''.join(
        f'<option value="{e(k)}"{" selected" if k == value else ""}>{e(title)}</option>' for k, title in options) + '</select></div>'


def textarea(prefix, name, label, value="", hint=None):
    identity = f"{prefix}-{name}"
    described = f' aria-describedby="{identity}-hint"' if hint else ''
    return f'<div><label for="{identity}">{e(label)}</label><textarea id="{identity}" name="{name}" rows="3"{described}>{e(value)}</textarea>' + (f'<small id="{identity}-hint">{e(hint)}</small>' if hint else "") + '</div>'


def form(kind, title, row, definitions):
    prefix = kind
    def field(name, label, default="", **kwargs):
        return input_field(prefix, name, label, row.get(name, default), **kwargs)
    policies = [(d["version"], d["version"]) for d in definitions if d["kind"] == "policy"]
    scales = [(d["version"], d["definition"]["name"] + " · " + d["version"]) for d in definitions if d["kind"] == "scale"]
    content = field("source", "Source", "self-report", required=True) + field("source_id", "Source record ID", required=True,
                hint="Keep this ID for corrections. Use one check-in per activity/week per source.")
    content += select(prefix, "policy_version", "Input policy", policies, row.get("policy_version", context.DEFAULT_POLICY))
    if row:
        content += f'<input type="hidden" name="supersedes" value="{e(row["version_id"])}">' + field("correction_reason", "Correction reason", required=True)
    content += field("source_updated_at", "Source correction timestamp", hint="Optional original source time with offset; server availability is recorded separately.")
    if kind in ("wellness", "strength"):
        content += field("local_date", "Local date", required=True, kind="date") + field("timezone", "Timezone", "Europe/London", required=True)
        content += field("observed_at", "Measurement / session time", hint="Optional ISO time with offset, e.g. 2026-10-25T01:30:00+01:00. Leave unknown times blank.")
    if kind == "wellness":
        content += field("metric", "Metric", required=True) + field("value", "Value") + field("unit", "Unit", required=True)
        content += select(prefix, "value_type", "Value type", [(v, v) for v in ("decimal", "category")], row.get("value_type", "decimal"))
        content += select(prefix, "validity", "Observation validity", [(v, v) for v in ("VALID", "SUSPECT", "INVALID", "MISSING")], row.get("validity", "VALID"))
        content += field("missing_reason", "Missing reason") + field("method", "Measurement method", "UNKNOWN", required=True)
        content += field("calibration", "Calibration or device setting", hint="Optional calibration identifier or method. A changed setting starts a separate comparison stream.")
        content += select(prefix, "origin", "Origin", [(v, v) for v in ("unknown", "device", "self_report", "laboratory")], row.get("origin", "unknown"))
    if kind in ("post_run", "preweek"):
        content += field("reported_at", "Report timestamp", required=True, hint="ISO time with explicit UTC offset.")
        content += field("activity_id", "Canonical activity ID", required=True, kind="number") if kind == "post_run" else field("week", "Week beginning Monday", required=True, kind="date")
        content += field("missing_reason", "Missing report reason")
        content += textarea(prefix, "pain_discomfort", "Pain / discomfort", row.get("pain_discomfort", ""))
        content += field("pain_location", "Pain location") + textarea(prefix, "context", "Symptom context", row.get("context", ""))
        content += '<fieldset><legend>Reported attention signals</legend>'
        for flag, label in (("distinct_pain", "Distinct pain"), ("worsening_symptoms", "Worsening symptoms"), ("illness", "Illness")):
            content += f'<label><input type="checkbox" name="attention" value="{flag}"{" checked" if flag in row.get("attention", []) else ""}>{label}</label>'
        content += '</fieldset>'
    if kind == "strength":
        content += field("duration_s", "Duration in seconds")
        content += select(prefix, "status", "Completion status", [(v, v) for v in ("unknown", "completed", "partial", "not_completed")], row.get("status", "unknown"))
        content += select(prefix, "lower_body_emphasis", "Lower-body emphasis", [(v, v) for v in ("unknown", "yes", "no")], row.get("lower_body_emphasis", "unknown"))
        content += textarea(prefix, "exercises", "Exercises (JSON)", json.dumps(row.get("exercises", [])),
                             'Optional list, e.g. [{"name":"Squat","sets":3,"reps":"5","load":"20.5","load_unit":"kg"}]. Exercise choice remains yours.')
        content += field("soreness_reported_at", "Later soreness report timestamp")
    if kind in ("post_run", "preweek", "strength"):
        names = ("rpe", "soreness") if kind == "strength" else ("rpe", "fatigue", "soreness", "pain", "readiness")
        content += '<fieldset><legend>Optional ratings</legend><p>Select the defined scale for each supplied rating. Register a different scale below when needed.</p>'
        for name in names:
            rating = row.get("ratings", {}).get(name, {})
            content += input_field(prefix, name, name.upper() if name == "rpe" else name.capitalize(), rating.get("value", ""))
            content += select(prefix, name + "_scale", (name.upper() if name == "rpe" else name.capitalize()) + " scale", [("", "Choose a scale")]+scales, rating.get("scale_version", ""))
        content += '</fieldset>' + textarea(prefix, "notes", "Notes", row.get("notes", ""))
    if kind == "coverage":
        content += select(prefix, "domain", "Coverage stream", [(v, v) for v in context.KINDS if v != "coverage"], row.get("domain", "wellness"))
        content += field("first", "First covered date", required=True, kind="date") + field("last", "Last covered date", required=True, kind="date")
        content += field("timezone", "Timezone", "Europe/London", required=True) + field("metric", "Metric (optional)")
        content += select(prefix, "status", "Coverage status", [(v, v) for v in ("PARTIAL", "COMPLETE", "UNAVAILABLE")], row.get("status", "PARTIAL"))
        content += select(prefix, "basis", "Coverage evidence", [(v, v) for v in ("athlete_attestation", "import_manifest")], row.get("basis", "athlete_attestation"))
        content += field("reported_at", "Attestation / manifest timestamp", required=True) + field("reason", "Coverage evidence / missing reason", required=True)
    return f'<section><h2>{e(title)}</h2><form class="context-entry" data-kind="{kind}" action="/api/context/records">{content}<button type="submit">{"Save correction" if row else "Save " + title.lower()}</button><p role="status" class="result"></p></form></section>'


SCRIPT = """
<script>
document.querySelectorAll('form.context-entry, form.context-definition, form.context-import').forEach(form => {
 form.addEventListener('submit', async event => {
  event.preventDefault(); const button=event.submitter; const message=form.querySelector('.result');
  const data=new FormData(form); let url=form.action, body, headers={};
  try {
   if(form.classList.contains('context-import')) { url += '?format='+encodeURIComponent(data.get('format')); data.delete('format'); body=data; }
   else {
    const fields=Object.fromEntries([...data].filter(([k,v]) => v !== ''));
    const rawJSON={};
    if(form.classList.contains('context-definition')) {
     url='/api/context/definitions/'+encodeURIComponent(fields.kind)+'/'+encodeURIComponent(fields.version);
     const kind=fields.kind; delete fields.kind; delete fields.version;
     if(kind==='policy') fields.expected_wellness_metrics=(fields.expected_wellness_metrics||'').split(',').map(s=>s.trim()).filter(Boolean);
     else fields.anchors=JSON.parse(fields.anchors||'{}');
    } else {
     fields.kind=form.dataset.kind;
     if(fields.activity_id) {
      if(!/^[1-9][0-9]*$/.test(fields.activity_id)) throw new Error('Use a positive whole activity ID.');
      rawJSON.activity_id=fields.activity_id;
     }
     if(fields.exercises) {
      // Parse only to validate syntax; serialize the original numeric lexemes.
      // The server validates structure and parses decimals without float rounding.
      JSON.parse(fields.exercises); rawJSON.exercises=fields.exercises;
     }
     const ratings={};
     for(const name of ['rpe','fatigue','soreness','pain','readiness']) {
      if(fields[name] !== undefined) ratings[name]={value:fields[name],scale_version:fields[name+'_scale']||''};
      delete fields[name]; delete fields[name+'_scale'];
     }
     if(['post_run','preweek','strength'].includes(fields.kind)) fields.ratings=ratings;
     if(['post_run','preweek'].includes(fields.kind)) fields.attention=data.getAll('attention');
    }
    body='{'+Object.entries(fields).map(([key,value])=>JSON.stringify(key)+':'+
     (Object.hasOwn(rawJSON,key)?rawJSON[key]:JSON.stringify(value))).join(',')+'}';
    headers={'Content-Type':'application/json'};
   }
   button.disabled=true; message.textContent='Saving…';
   const response=await fetch(url,{method:'POST',body,headers}); const result=await response.json();
   if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Check the supplied fields.');
   location.assign(result.html_url);
  } catch(error) { message.textContent=error.message || 'Could not save. Reload the page to inspect stored records.'; }
  finally {button.disabled=false;}
 });
});
</script>
"""


def definition_forms():
    policy = '<h2>Define an input policy</h2><form class="context-definition"><input type="hidden" name="kind" value="policy">'
    policy += input_field("policy", "version", "Policy version", required=True)
    policy += select("policy", "daily_aggregation", "Daily aggregation", [("retain_all", "Retain all; ambiguous when multiple"), ("median", "Median within compatible numeric groups")], "retain_all")
    policy += input_field("policy", "expected_wellness_metrics", "Expected metrics (comma separated)", "resting_hr,hrv,sleep_duration")
    policy += '<button>Save input policy</button><p role="status" class="result"></p></form>'
    scale = '<h2>Define a rating scale</h2><p>Definitions are immutable. Use a new version to change meaning; incompatible scales are never combined.</p><form class="context-definition"><input type="hidden" name="kind" value="scale">'
    for name, label in (("version", "Scale version"), ("name", "Scale name"), ("minimum", "Scale minimum"), ("maximum", "Scale maximum"), ("unit", "Scale unit"), ("description", "Scale description")):
        scale += input_field("scale", name, label, required=True)
    scale += textarea("scale", "anchors", "Scale anchors (JSON)", "{}") + '<button>Save rating scale</button><p role="status" class="result"></p></form>'
    return policy + scale


@router.get("/context", response_class=HTMLResponse)
def page(request: Request, as_of: str | None = None, first: str | None = None, last: str | None = None,
         policy_version: str = context.DEFAULT_POLICY, edit: str | None = None):
    current = data(request, as_of, first, last, policy_version)
    defs = context.definitions(store(request))
    selected = history(request, edit)["versions"][-1] if edit else None
    query = urlencode({k: v for k, v in {"as_of": as_of, "first": first, "last": last, "policy_version": policy_version}.items() if v})
    content = '<p>Wellness, runner reports and strength sessions are optional context. Missing data does not mean recovery or illness. This page makes no medical or mileage decisions.</p>'
    content += '<style>.context-entry, .context-definition, .context-import {display:grid;grid-template-columns:minmax(0,1fr);gap:.8rem;border:1px solid #bccbc2;padding:1rem;margin:1rem 0} fieldset > div, .context-entry > *, .context-definition > *, .context-import > * {min-width:0;max-width:100%} textarea {box-sizing:border-box;max-width:100%;width:100%;font:inherit} .context-entry label input[type=checkbox] {display:inline} pre {white-space:pre-wrap;overflow-wrap:anywhere} .result {color:#753a13}</style>'
    content += '<form method="get"><fieldset><legend>Inspect a cutoff / date range</legend>'
    content += input_field("filter", "as_of", "As-of timestamp", as_of, hint="Explicit offset; uses server-known history, never backdated import time.")
    content += input_field("filter", "first", "From", first, kind="date") + input_field("filter", "last", "To", last, kind="date")
    content += select("filter", "policy_version", "Read policy", [(d["version"], d["version"]) for d in defs if d["kind"] == "policy"], policy_version)
    content += '</fieldset><button>Inspect context</button></form>'
    content += f'<p><a href="/api/context?{e(query)}">Context JSON</a> · <a href="/api/context/definitions">Scale and policy definitions</a> · <a href="/api/context/events">Append-only event log</a></p>'
    content += '<h2>Wellness missingness</h2><ul>' + ''.join(f'<li>{e(metric)}: {e(state["state"])} · {state["observed_days"]} observed days · units {e(", ".join(state["units"]) or "unknown")}<small>{e(state["missing_reason"] or "Observed days do not establish complete source coverage.")}</small></li>' for metric, state in current["wellness"].items()) + '</ul>'
    content += '<p>HRV/sleep values are never assumed. Date-only observations keep an unknown measurement instant; a current partial day is not complete coverage.</p>'
    content += '<h2>Reports and attention</h2><p>' + e(', '.join(f'{k}: {v}' for k, v in current['report_counts'].items())) + '</p>'
    content += '<p>Post-run / pre-week reports absent from a target are MISSING. Attention signals go to planner review only.</p>'
    if current["attention_signals"]:
        content += '<ul>' + ''.join(f'<li>{e(r["kind"])} · {e(r["reported_at"])} · PLANNER_REVIEW_ONLY</li>' for r in current["attention_signals"]) + '</ul>'
    content += '<form action="/context/feedback" method="get"><fieldset><legend>Inspect a post-run check-in</legend>' + input_field("target", "activity_id", "Activity ID to inspect", required=True, kind="number") + '<button>Inspect check-in</button></fieldset></form>'
    content += '<form action="/context/feedback" method="get"><fieldset><legend>Inspect weekly readiness</legend>' + input_field("target-week", "week", "Monday to inspect", required=True, kind="date") + '<button>Inspect readiness</button></fieldset></form>'
    content += '<h2>Saved contextual records</h2><p>S&amp;C contributes no running kilometres. Completed strength sessions appear only in this separate stream.</p>'
    for row in current["records"]:
        summary = f'{row["kind"]} · {row["source"]} / {row["source_id"]} · revision {row["revision"]}'
        content += f'<article><h3>{e(summary)}</h3><p><a href="/context/records/{row["record_id"]}">History and source details</a> · <a href="/context?edit={row["record_id"]}">Correct this record</a></p><dl>'
        for label, value in (("Local date / target week", row.get("local_date", row.get("week"))), ("Measurement / report UTC", row.get("event_utc") or "UNKNOWN measurement time"), ("First known", row["first_known_at"]), ("Version available", row["known_at"]), ("Value and unit", f'{row.get("value") if row.get("value") is not None else "MISSING"} {row.get("unit", "")}' if row["kind"] == "wellness" else None), ("Ratings and scale definitions", ", ".join(f'{name} {rating["value"]} ({rating["scale_version"]})' for name, rating in row.get("ratings", {}).items())), ("Strength duration / status", f'{row.get("duration_s") or "unknown"} s / {row.get("status")}' if row["kind"] == "strength" else None), ("Pain / discomfort", row.get("pain_discomfort")), ("Notes", row.get("notes")), ("Validity / missing reason", f'{row.get("validity", "")} {row.get("missing_reason") or ""}'), ("Method / interpretation", f'{row.get("method", "")} {row.get("interpretation", "")}')):
            if value and str(value).strip():
                content += f'<dt>{e(label)}</dt><dd>{e(value)}</dd>'
        content += '</dl></article>'
    if not current["records"]:
        content += '<p>No contextual records supplied for this selection.</p>'
    if current["withheld"]:
        content += f'<p>{len(current["withheld"])} known records withheld because their event/local date is after the cutoff.</p>'
    content += '<details><summary>Daily observation groups and retained conflicts</summary><pre>' + e(json.dumps(current['daily_wellness'], indent=2)) + '</pre></details>'
    if selected:
        content += '<h2>Correct a saved record</h2><p>The previous version and source bytes remain inspectable.</p>' + form(selected["kind"], "Correction", selected, defs)
        content += '<p><a href="/context">Return to new entries</a></p>'
    else:
        content += '<h2>Import a supported export</h2><p>Use the documented context-export-v1 JSON or CSV schema from an owned export or self-reported source. Proprietary vendor layouts are not guessed.</p>'
        content += '<form class="context-import" action="/api/context/import" enctype="multipart/form-data"><label>Context file<input type="file" name="file" accept=".json,.csv" required></label><label for="import-format">Export format</label><select id="import-format" name="format"><option value="json">JSON</option><option value="csv">CSV</option></select><button>Import context</button><p role="status" class="result"></p></form>'
        for kind, title in (("wellness", "Wellness observation"), ("post_run", "Post-run check-in"), ("preweek", "Pre-week readiness"), ("strength", "Strength session"), ("coverage", "Source coverage")):
            content += f'<details><summary>{title}</summary>' + form(kind, title, {}, defs) + '</details>'
        content += '<details><summary>Input policies and rating scales</summary>' + definition_forms() + '</details>'
    return views.page("Context inputs and history", content + SCRIPT)


@router.get("/context/records/{record_id}", response_class=HTMLResponse)
def record_page(request: Request, record_id: str, as_of: str | None = None):
    record = history(request, record_id, as_of)
    body = '<p><a href="/context">Context inputs</a> · ' + f'<a href="/context?edit={e(record_id)}">Correct this record</a></p>'
    body += '<p>Each revision preserves the source checksum, original time, first-known time and correction availability. Current activity redirects are labelled separately from historical evidence.</p>'
    for row in record["versions"]:
        body += f'<h2>Revision {row["revision"]}</h2><pre>' + e(json.dumps(row, indent=2, ensure_ascii=False)) + '</pre>'
    return views.page("Context correction history", body)


@router.get("/context/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, activity_id: int | None = Query(None, ge=1, le=context.MAX_SQLITE_INTEGER),
                  week: str | None = None, as_of: str | None = None):
    result = feedback(request, activity_id, week, as_of)
    body = '<p><a href="/context">Enter or correct a check-in</a></p>'
    body += '<p>Report state: ' + e(result["state"]) + '</p><p>' + e(result["missing_reason"] or 'Supplied reports retain individual scale and source definitions.') + '</p>'
    body += '<pre>' + e(json.dumps(result, indent=2, ensure_ascii=False)) + '</pre>'
    return views.page("Target check-in history", body)
