"""Normal local API and inspectable workload/input pages, sharing the G3 core."""
from datetime import date, datetime, time
from html import escape
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import views
import workload
import workload_inputs as inputs


router = APIRouter()


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except inputs.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from None


def options(as_of=None, knowledge_cutoff=None, mode="EVENT_TIME_RECONSTRUCTION", proposed_start=None, proposed_distance_m=None):
    return {"as_of": as_of or None, "knowledge_cutoff": knowledge_cutoff or None, "mode": mode,
            "proposed_start": proposed_start or None, "proposed_distance_m": proposed_distance_m if proposed_distance_m not in (None, "") else None}


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str | None = None
    knowledge_cutoff: str | None = None
    mode: str = "EVENT_TIME_RECONSTRUCTION"
    proposed_start: str | None = None
    proposed_distance_m: str | None = None


@router.get("/api/workload")
def read(request: Request, as_of: str | None = None, knowledge_cutoff: str | None = None,
         mode: str = "EVENT_TIME_RECONSTRUCTION", proposed_start: str | None = None, proposed_distance_m: str | None = None):
    return call(workload.read, request.app.state.store, **options(as_of, knowledge_cutoff, mode, proposed_start, proposed_distance_m))


@router.post("/api/workload/capture")
def capture(request: Request, query: Query):
    value = call(workload.capture, request.app.state.store, **options(**query.model_dump()))
    return {**value, "html_url": "/workload/snapshots/" + value["snapshot_id"]}


@router.get("/api/workload/snapshots/{identity}")
def saved(request: Request, identity: str):
    value = workload.snapshot(request.app.state.store, identity)
    if value is None:
        raise HTTPException(404, "Workload snapshot not found.")
    return value


@router.get("/api/workload/snapshots/{identity}/replay")
def replay(request: Request, identity: str):
    value = workload.replay(request.app.state.store, identity)
    if value is None:
        raise HTTPException(404, "Workload snapshot not found.")
    return value


@router.post("/api/workload/inputs")
async def enter(request: Request):
    value = call(inputs.ingest, request.app.state.store, await request.body())
    return {**value, "html_url": "/workload"}


@router.post("/api/workload/import")
async def import_file(request: Request, file: UploadFile):
    value = call(inputs.ingest, request.app.state.store, await file.read(2_000_001))
    return {**value, "html_url": "/workload"}


@router.get("/api/workload/inputs/{record_id}")
def input_history(request: Request, record_id: str):
    values = inputs.history(request.app.state.store, record_id)
    if not values:
        raise HTTPException(404, "Workload input not found.")
    return {"versions": values}


class ScanCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_id: int = Field(ge=1, lt=2**63, strict=True)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4000)


@router.post("/api/workload/provider-coverage")
def provider_capture(request: Request, choice: ScanCapture):
    return {**call(inputs.provider_evidence, request.app.state.store, **choice.model_dump()), "html_url": "/workload"}


@router.post("/api/workload/coverage-dates")
async def coverage_dates(request: Request):
    """UI convenience: explicit London midnight boundaries, no browser timezone guess."""
    try:
        value = json.loads(await request.body())
        first, last = value.pop("first"), value.pop("to_exclusive")
        lower = datetime.combine(date.fromisoformat(first), time(), ZoneInfo("Europe/London"))
        upper = datetime.combine(date.fromisoformat(last), time(), ZoneInfo("Europe/London"))
        value.update(lower_utc=lower.isoformat(), upper_utc=upper.isoformat(), boundary_timezone="Europe/London",
                     original_local_bounds={"from": first, "to_exclusive": last, "lower_offset": str(lower.utcoffset()), "upper_offset": str(upper.utcoffset())})
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Supply valid London From and To exclusive dates.") from None
    if value.get("basis") == "BOTH":
        if value.get("supersedes"):
            raise HTTPException(422, "Correct START and END assertions separately.")
        value = [{**value, "basis": basis, "source_id": value.get("source_id", "") + ":" + basis} for basis in ("START", "END")]
    result = call(inputs.ingest, request.app.state.store, inputs.encoded(value).encode())
    return {**result, "html_url": "/workload"}


def e(value):
    return escape(str(value) if value is not None else "Unknown", quote=True)


def details(title, value):
    return f'<details><summary>{e(title)}</summary><pre style="white-space:pre-wrap">{e(json.dumps(value, indent=2))}</pre></details>'


def text_field(name, label, value="", required=False, kind="text"):
    return f'<label>{e(label)}<input type="{kind}" name="{name}" value="{e(value)}" {"required" if required else ""}></label>'


def area(name, label, value="", required=True):
    return f'<label>{e(label)}<textarea name="{name}" rows="3" style="width:100%;box-sizing:border-box" {"required" if required else ""}>{e(value)}</textarea></label>'


def select(name, label, values, chosen=None):
    return f'<label>{e(label)}<select name="{name}">' + "".join(f'<option value="{e(v)}" {"selected" if v == chosen else ""}>{e(label)}</option>' for v, label in values) + '</select></label>'


def entry_form(kind, data, old=None):
    old = old or {}
    fields = f'<input type="hidden" name="kind" value="{kind}">'
    if old:
        fields += f'<input type="hidden" name="supersedes" value="{old["version_id"]}">'
        fields += text_field("correction_reason", "Correction reason", required=True)
    for name, label, default in (("source", "Source", "athlete"), ("source_id", "Source record ID", ""),
                                  ("actor", "Actor", "athlete"), ("reason", "Evidence / reason", "")):
        fields += text_field(name, label, old.get(name, default), True)
    fields += area("source_refs", "Source references (JSON)", json.dumps(old.get("source_refs", [{"type": "athlete-attestation"}])))
    if kind == "coverage":
        fields += select("basis", "Endpoint basis", [(v, v) for v in ("END", "START", "BOTH", "RECORDED_LOCAL_DATE")], old.get("basis"))
        fields += select("evidence_kind", "Evidence kind", [(v, v) for v in ("ATHLETE_ATTESTATION", "IMPORT_MANIFEST", "PROVIDER_CENSUS")], old.get("evidence_kind"))
        fields += select("status", "Census status", [(v, v) for v in ("COMPLETE", "INCOMPLETE", "UNKNOWN")], old.get("status"))
        fields += '<p>Use absolute bounds below, or London dates (To exclusive). An open week can end at the inclusive observation cutoff. Complete coverage cannot include unobserved future time.</p>'
        for name, label in (("lower_utc", "Lower UTC bound"), ("upper_utc", "Upper UTC bound")):
            fields += text_field(name, label, old.get(name, ""))
        fields += text_field("first", "London From date", kind="date") + text_field("to_exclusive", "London To exclusive date", kind="date")
        fields += select("lower_inclusive", "Lower endpoint included", [("true", "Yes"), ("false", "No")], str(old.get("lower_inclusive", True)).lower())
        fields += select("upper_inclusive", "Upper endpoint included", [("false", "No"), ("true", "Yes")], str(old.get("upper_inclusive", False)).lower())
        fields += text_field("observed_through", "Observed through timestamp", old.get("observed_through", data["knowledge_cutoff"]), True)
        fields += text_field("reported_at", "Attestation / manifest timestamp", old.get("reported_at", data["knowledge_cutoff"]), True)
        fields += area("exclusions", "Unresolved exclusions (JSON)", json.dumps(old.get("exclusions", [])))
    elif kind == "zone_profile":
        for name, label in (("method", "Zone method"), ("metric", "Measured metric"), ("unit", "Metric unit"),
                             ("calibration", "Calibration evidence"), ("effective_from", "Effective from timestamp"), ("effective_to", "Effective to timestamp (optional)")):
            fields += text_field(name, label, old.get(name) or "", name != "effective_to")
        fields += select("system", "Zone system", [(v, v) for v in ("RESEARCH_THREE", "DEVICE_NATIVE_FIVE", "OTHER")], old.get("system"))
        fields += select("reliability", "Method reliability", [(v, v) for v in ("UNCALIBRATED", "CALIBRATED", "DEVICE_REPORTED")], old.get("reliability"))
        fields += area("bands", "Band definitions (JSON)", json.dumps(old.get("bands", [{"label": "low"}, {"label": "middle"}, {"label": "high"}])))
        fields += '<p>No physiological thresholds are supplied. Record your source method, effective dates and calibration. “Calibrated” describes supplied evidence, not scientific approval by this software.</p>'
    else:
        activities = [(r["canonical_version_id"], f'Activity {r["activity_id"]} · {r["canonical_version_id"]}') for r in data["activities"]]
        profiles = [(r["version_id"], r["method"] + ' · ' + r["version_id"]) for r in data["input_records"] if r["kind"] == "zone_profile"]
        for name, label, values in (("canonical_version_id", "Exact canonical version", activities), ("profile_version_id", "Exact zone profile", profiles)):
            if old.get(name) and old[name] not in {v for v, _ in values}:
                values.append((old[name], old[name]))
            fields += select(name, label, values, old.get(name))
        fields += select("duration_basis", "Measured duration basis", [(v, v) for v in workload.MEASURES if v != "distance_m"], old.get("duration_basis"))
        fields += area("seconds", "Measured band seconds (JSON)", json.dumps(old.get("seconds", {"low": "0", "middle": "0", "high": "0"})))
        fields += text_field("unclassified_seconds", "Explicit unclassified seconds", old.get("unclassified_seconds", "0"), True)
        fields += text_field("measured_at", "Measurement timestamp", old.get("measured_at", data["knowledge_cutoff"]), True)
        fields += text_field("measurement_method", "Measurement method / source semantics", old.get("measurement_method", ""), True)
    title = {"coverage": "running coverage", "zone_profile": "zone profile", "band_ledger": "measured band ledger"}[kind]
    return f'<form class="workload-entry" data-kind="{kind}" action="/api/workload/inputs"><fieldset><legend>{"Correct" if old else "Enter"} {title}</legend>{fields}<button>{"Save correction" if old else "Save " + title}</button></fieldset></form>'


SCRIPT = """
<script>
document.addEventListener('submit', async event => {
 const form=event.target;
 if (!form.matches('.workload-entry,.workload-capture,.workload-import,.workload-scan')) return;
 event.preventDefault(); const button=event.submitter, message=document.getElementById('workload-message');
 button.disabled=true; message.textContent='Saving…';
 try {
  let data=Object.fromEntries(new FormData(form)), url=form.getAttribute('action'), headers={}, body;
  if(form.matches('.workload-import')) body=new FormData(form);
  else {
   if(form.matches('.workload-entry')) {
    for(const key of ['source_refs','exclusions','bands','seconds']) if(key in data) data[key]=JSON.parse(data[key]);
    for(const key of ['lower_inclusive','upper_inclusive']) if(key in data) data[key]=data[key]==='true';
    if(data.effective_to==='') data.effective_to=null;
    if(data.kind==='coverage') {
     if(!data.lower_utc && !data.upper_utc) {url='/api/workload/coverage-dates'; delete data.lower_utc; delete data.upper_utc;}
     else {delete data.first; delete data.to_exclusive;}
    }
   }
   if(form.matches('.workload-scan')) data.scan_id=Number(data.scan_id);
   if(form.matches('.workload-capture')) for(const key in data) if(data[key]==='') data[key]=null;
   headers={'Content-Type':'application/json'};
   if(data.basis==='BOTH' && !url.endsWith('coverage-dates')) {
    if(data.supersedes) throw Error('Correct START and END assertions separately using their history links.');
    data=['START','END'].map(basis=>({...data,basis,source_id:data.source_id+':'+basis}));
   }
   body=JSON.stringify(data);
  }
  const response=await fetch(url,{method:'POST',headers,body}), result=await response.json();
  if(!response.ok) throw Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
  location.assign(result.html_url);
 } catch(error) {message.textContent=error.message; button.disabled=false;}
});
</script>
"""


def page(data, frozen=None, correction=None):
    query = {"as_of": data["event_cutoff"], "knowledge_cutoff": data["knowledge_cutoff"], "mode": data["time_basis"]}
    if data["l30"]:
        query.update(proposed_start=data["l30"]["proposed_start_utc"], proposed_distance_m=data["l30"]["proposed_distance_m"])
    body = '<p>Whole completed runs; fractional metres. FM weeks use the END date in London; planner weeks use START. The History calendar keeps its recorded local date.</p>'
    body += f'<p><strong>{e(data["time_basis"])}</strong> · Event cutoff {e(data["event_cutoff"])} · Knowledge cutoff {e(data["knowledge_cutoff"])}. Manual verification: UNVERIFIED.</p>'
    if data.get("current_diagnostics"):
        body += '<p><strong>INCOMPLETE_PROCESSING: canonical backfill required before using present totals as complete.</strong></p>' + details('Current operational diagnostic, outside historical inputs', data["current_diagnostics"])
    if frozen:
        body += f'<p>Frozen calculation {e(frozen["snapshot_id"])} · captured {e(frozen["created_at"])}. This workload artifact is not a prospective fitness prediction.</p><p><a href="/api/workload/snapshots/{frozen["snapshot_id"]}">Frozen evidence JSON</a> · <a href="/api/workload/snapshots/{frozen["snapshot_id"]}/replay">Replay frozen inputs</a></p>'
    else:
        fields = text_field("as_of", "Event cutoff", query["as_of"]) + text_field("knowledge_cutoff", "Knowledge cutoff", query["knowledge_cutoff"])
        fields += select("mode", "Time view", [(v, v) for v in ("EVENT_TIME_RECONSTRUCTION", "AS_KNOWN")], query["mode"])
        fields += text_field("proposed_start", "Proposed START timestamp", query.get("proposed_start", "")) + text_field("proposed_distance_m", "Proposed distance in metres", query.get("proposed_distance_m", ""))
        body += f'<form method="get" action="/workload"><fieldset><legend>Inspect workload and L30</legend>{fields}<button>Inspect workload</button></fieldset></form>'
        body += '<form class="workload-capture" action="/api/workload/capture">' + ''.join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k,v in query.items()) + '<button>Freeze this calculation</button></form>'
    body += '<p id="workload-message" role="status"></p><h2>Rolling completed exposure</h2>'
    body += '<div class="table-wrap"><table><thead><tr><th>Window</th><th>Known metres</th><th>Runs</th><th>Census</th><th>Missing distances</th></tr></thead><tbody>'
    for row in data["rolling"]:
        body += f'<tr><td>{row["days"]} days</td><td>{e(row["measures"]["distance_m"]["known_subtotal"])}</td><td>{row["run_count"]}</td><td>{e(row["coverage"]["status"])}</td><td>{row["measures"]["distance_m"]["missing_count"]}</td></tr>'
    body += '</tbody></table></div><p>Known subtotals are not complete totals when census or measurements are missing. Missing history is not verified rest.</p>'
    for row in data["rolling"]:
        body += details(f'{row["days"]}-day bounds, durations, intensity and coverage evidence', row)
    body += '<h2>Longest runs and L30</h2>'
    for row in data["longest"]:
        body += f'<p>{row["days"]} days: observed maximum {e(row["longest"]["observed_distance_m"])} m · {e(row["longest"]["status"])}.</p>'
    if data["l30"]:
        row = data["l30"]
        body += f'<p id="l30-result"><strong>{e(row["status"])}</strong> · ratio {e(row["ratio"])} · percentage change {e(row["percentage_change"])}%. Exposure comparison only; no injury probability or automatic mileage change.</p>'
        body += details('L30 exact bounds, denominator and sources', row)
    body += '<h2>ISO weeks and accounting</h2><div class="table-wrap"><table><thead><tr><th>Week</th><th>State</th><th>FM END metres</th><th>Planner START metres</th><th>END / START census</th></tr></thead><tbody>'
    for row in data["weeks"]:
        body += f'<tr><td>{e(row["iso_week"])}</td><td>{e(row["week_state"])}</td><td>{e(row["fm_end"]["measures"]["distance_m"]["known_subtotal"])}</td><td>{e(row["planner_start"]["measures"]["distance_m"]["known_subtotal"])}</td><td>{e(row["fm_end"]["coverage"]["status"])} / {e(row["planner_start"]["coverage"]["status"])}</td></tr>'
    body += '</tbody></table></div>' + details('Week boundaries, open-week observation and contributions', data["weeks"])
    body += '<h2>Spacing and missing history</h2>' + details('Observed gaps and overlaps', {"spacing": data["spacing"], "since_last_run": data["since_last_run"], "unassigned": data["unassigned"], "unassigned_known_subtotals": data["unassigned_known_subtotals"]})
    body += details('Dated zone profiles at cutoff', data['zone_profiles_at_cutoff']) + details('Provider modes and saved capability lineage', data['provider_lineage'])
    body += '<h2>Source contributions</h2>'
    for row in data["activities"]:
        body += f'<p><a href="/activities/{row["activity_id"]}/canonical?version={row["canonical_version_id"]}">Activity {row["activity_id"]} canonical evidence</a> · {e(row["measures"]["distance_m"])} m · {e(row["timing_basis"])}</p>'
        body += details(f'Activity {row["activity_id"]} workload input', row)
    body += details('Excluded inputs and reasons', data["excluded"])
    if not frozen:
        body += '<h2>Running coverage and intensity inputs</h2><p>Running census is distinct from wellness or strength coverage. Supply explicit evidence for each endpoint basis. Strength never adds running kilometres.</p>'
        if correction:
            body += entry_form(correction["kind"], data, correction)
        else:
            for kind, title in (("coverage", "Enter running coverage"), ("zone_profile", "Enter a sourced zone profile"), ("band_ledger", "Enter measured intensity seconds")):
                body += f'<details><summary>{title}</summary>{entry_form(kind, data)}</details>'
        body += '<form class="workload-import" action="/api/workload/import"><fieldset><legend>Import immutable input JSON</legend><label>Workload JSON file<input name="file" type="file" accept="application/json,.json" required></label><button>Import workload inputs</button></fieldset></form>'
        body += '<details><summary>Capture legacy provider census evidence</summary><form class="workload-scan" action="/api/workload/provider-coverage"><fieldset><legend>Recorded-local-date scan only</legend>' + text_field('scan_id','Garmin scan ID',required=True,kind='number') + text_field('actor','Actor','athlete',True) + text_field('reason','Capture reason',required=True) + '<button>Capture provider evidence</button></fieldset></form><p>This preserves current scan facts with today’s knowledge time. It does not establish canonical START/END completeness.</p></details>'
        for record in data["input_records"]:
            body += f'<h3>{e(record["kind"])} · {e(record["source_id"])} · revision {record["revision"]}</h3><p><a href="/workload/inputs/{record["record_id"]}">Input revision history</a> · <a href="/workload?correct={record["record_id"]}">Correct this input</a></p>' + details('Input and source evidence', record)
        body += f'<p><a href="/api/workload?{e(urlencode(query))}">Workload JSON</a></p>'
    return views.page("Training workload", body + SCRIPT)


@router.get("/workload", response_class=HTMLResponse)
def workload_page(request: Request, as_of: str | None = None, knowledge_cutoff: str | None = None,
                  mode: str = "EVENT_TIME_RECONSTRUCTION", proposed_start: str | None = None,
                  proposed_distance_m: str | None = None, correct: str | None = None):
    data = read(request, as_of, knowledge_cutoff, mode, proposed_start, proposed_distance_m)
    old = input_history(request, correct)["versions"][-1] if correct else None
    return page(data, correction=old)


@router.get("/workload/snapshots/{identity}", response_class=HTMLResponse)
def saved_page(request: Request, identity: str):
    frozen = saved(request, identity)
    return page(frozen["result"], frozen=frozen)


@router.get("/workload/inputs/{record_id}", response_class=HTMLResponse)
def input_page(request: Request, record_id: str):
    records = input_history(request, record_id)["versions"]
    return views.page("Workload input history", '<p><a href="/workload">Training workload</a></p>' + ''.join(f'<h2>Revision {r["revision"]}</h2>' + details('Immutable input and source', r) for r in records))
