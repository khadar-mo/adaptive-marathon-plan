"""Read-only readiness inspection with explicit immutable policy/record saves."""
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import HTMLResponse

import context_data
from context_web import bounded_body
from fitness_web import call
from fitness_views import details, e, field, link, select, table
import fm_snapshots
import readiness
import views

router = APIRouter()


def choice(identity, policy, week, activity):
    return {"snapshot_id": identity, "policy_version": policy, "scope": {"week": week or None, "activity_id": activity}}


@router.get("/api/snapshots/{identity}/readiness")
def read(request: Request, identity: str = Path(pattern=r"^[a-f0-9]{64}$"), policy_version: str = readiness.DEFAULT_POLICY,
         week: str | None = None, activity_id: int | None = Query(None, ge=1, le=context_data.MAX_SQLITE_INTEGER)):
    return call(readiness.evaluate, request.app.state.store, choice(identity, policy_version, week, activity_id))


@router.get("/api/readiness/policies")
def policies(request: Request):
    return readiness.policies(request.app.state.store)


@router.post("/api/readiness/policies")
async def policy(request: Request):
    return call(readiness.register, request.app.state.store, await bounded_body(request))


@router.post("/api/readiness/records")
async def save(request: Request):
    return call(readiness.save, request.app.state.store, call(readiness.fi.parse, await bounded_body(request)))


@router.get("/api/readiness/records/{identity}")
def saved(request: Request, identity: str = Path(pattern=r"^[a-f0-9]{64}$")):
    return call(readiness.saved, request.app.state.store, identity)


@router.get("/api/readiness/records/{identity}/replay")
def replay(request: Request, identity: str = Path(pattern=r"^[a-f0-9]{64}$")):
    return call(readiness.replay, request.app.state.store, identity)


def evidence(result):
    text = '<h2>Readiness: ' + e(result["state"]) + '</h2><p>' + e(result["reason"]) + '</p>'
    text += '<p>Policy ' + e(result["policy"]["version"]) + ' · ' + e(result["policy_time_basis"]) + '</p>'
    text += table(["Snapshot", "Event cutoff T", "Knowledge cutoff K", "Target"], [[e(result["snapshot_id"]), e(result["event_cutoff"]), e(result["knowledge_cutoff"]), e(result["scope"])]] )
    if result["state"] == "PAIN_OR_ILLNESS_REVIEW":
        text += '<p>Review the reported pain or illness. This result does not clear demanding exercise; seek appropriate professional advice for concerning symptoms.</p>'
    text += '<h3>Runner reports</h3>' + table(["Source / ID", "Target", "Report time", "Known at", "Applicable", "Interpretation / reasons"], [
        [e(r["record"]["source"] + ' / ' + r["record"]["source_id"]), e(r["record"].get("week") or r["record"].get("activity_id")),
         e(r["record"]["reported_at"]), e(r["record"]["known_at"]), e(r["applicable"]), e(r["state"] + ' · ' + ', '.join(r["reasons"]))]
        for r in result["runner_reports"]])
    text += '<h3>Compatible wellness evidence</h3>' + table(["Metric / source / unit", "State", "Days", "Baseline / MAD / z", "Rule"], [
        [e(r["rule"]["stream"]["metric"] + ' / ' + r["rule"]["stream"]["source"] + ' / ' + r["rule"]["stream"]["unit"]), e(r["state"]),
         e(', '.join(d["local_date"] for d in r["days"])),
         e([{ "baseline":d["g5_descriptor"]["baseline"], "observed_days":d["g5_descriptor"]["observed_days"], **d["g5_descriptor"]["robust_deviation"]} for d in r["days"]]), e(r["rule"])] for r in result["wellness_evidence"]])
    text += '<h3>Missingness and limitations</h3><ul>' + ''.join('<li>' + e(note) + '</li>' for note in result["limitations"]) + '</ul>'
    for title, data in (("Missing evidence", result["missingness"]), ("Persistence proof", result["persistence"]),
                        ("Runner values, scales and source versions", result["runner_reports"]),
                        ("Wellness dates, baseline and threshold provenance", result["wellness_evidence"]),
                        ("Unconfigured compatible streams", result["unconfigured_wellness_streams"]),
                        ("Unlinked or time-uncertain evidence", {k:result[k] for k in ("unlinked_feedback", "uncertain_context")}),
                        ("Immutable policy and actor", result["policy"])):
        text += details(title, data)
    return text


STYLE = '<style>textarea{box-sizing:border-box;width:100%;font:inherit}form{margin:1rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}td{vertical-align:top}form>label{margin-top:.7rem}</style>'


@router.get("/readiness", response_class=HTMLResponse)
def page(request: Request, snapshot_id: str | None = None, policy_version: str = readiness.DEFAULT_POLICY,
         week: str | None = None, activity_id: str | None = None):
    # Native GET forms submit the unselected run as an empty string.
    activity_id = call(int, activity_id) if activity_id else None
    if activity_id is not None:
        call(readiness.Scope, activity_id=activity_id)
    store = request.app.state.store
    records = fm_snapshots.history(store)
    definitions = readiness.policies(store)
    selected = next((p for p in definitions if p["version"] == policy_version), None)
    if selected is None:
        return call(readiness.policy, None, policy_version)
    text = STYLE + '<p>Interpret a saved FitnessSnapshot using an explicit provisional policy. Nothing here changes a plan, distance target or athlete decision.</p>'
    text += '<p>' + link('/validation', 'Choose or create a fitness snapshot') + ' · ' + link('/context', 'Enter runner or wellness reports') + '</p>'
    if records:
        snapshot_id = snapshot_id or records[0]["id"]
        snap = call(fm_snapshots.load_snapshot, store, snapshot_id)
        text += '<form action="/readiness"><fieldset><legend>Interpret frozen evidence</legend>'
        text += select('snapshot_id', 'Saved snapshot', [(r['id'],r['event_cutoff'] + ' · ' + r['id'][:10]) for r in records], snapshot_id)
        text += select('policy_version', 'Readiness policy', [(p['version'],p['version']) for p in definitions], policy_version)
        text += field('week', 'Target week (Monday)', week, typ='date')
        text += select('activity_id', 'Completed run (leave blank for week)', [('', 'No run selected'), *[(str(r['activity_id']),f"Run {r['activity_id']} · {r['start_utc']}") for r in snap['planner']['actuals']]], str(activity_id) if activity_id else '')
        text += '<p>Choose a week or a completed run, not both. Only this snapshot’s frozen evidence is used.</p></fieldset><button>Inspect readiness</button></form>'
        if week or activity_id:
            current = choice(snapshot_id, policy_version, week, activity_id)
            result = call(readiness.evaluate, store, current)
            query = urlencode({k:v for k,v in {"policy_version":policy_version,"week":week,"activity_id":activity_id}.items() if v is not None and v != ''})
            text += '<p>' + link('/api/snapshots/' + snapshot_id + '/readiness?' + query, 'Same interpretation JSON') + '</p>' + evidence(result)
            text += '<form data-save="' + e(json.dumps(current)) + '"><button>Save immutable interpretation</button><p role="status"></p></form>'
    else:
        text += '<p>No saved FitnessSnapshot. Capture one before interpreting readiness.</p>'
    text += '<details><summary>Define a new immutable policy</summary><p>Copy the selected version, give it a new name, and record who chose the rules and why. No personal thresholds are supplied by default. Rating rules name the exact source, field, scale and ordinary/concern ranges. Wellness rules name the entire compatible stream and reuse its G5 direction/MAD/epsilon.</p>'
    text += '<form id="readiness-policy">' + field('version','New policy version',required=True) + field('actor','Policy author',required=True)
    text += '<label for="readiness-reason">Policy reason</label><textarea id="readiness-reason" name="reason" required></textarea>'
    text += '<label for="readiness-config">Policy configuration (JSON)</label><textarea id="readiness-config" name="config" rows="15" required>\n' + e(json.dumps(selected['config'],ensure_ascii=False,indent=2)) + '</textarea>'
    text += '<button>Save policy version</button><p role="status"></p></form></details>'
    return views.page('Readiness from a frozen snapshot', text + SCRIPT)


@router.get('/readiness/records/{identity}', response_class=HTMLResponse)
def record_page(request: Request, identity: str = Path(pattern=r'^[a-f0-9]{64}$')):
    value = saved(request, identity)
    return views.page('Saved readiness interpretation', STYLE + '<p>' + link('/api/readiness/records/'+identity,'Frozen interpretation JSON') + ' · ' + link('/api/readiness/records/'+identity+'/replay','Replay frozen interpretation') + ' · ' + link('/readiness','Inspect another scope or policy') + '</p>' + evidence(value['result']))


SCRIPT = r"""<script>
document.querySelectorAll('#readiness-policy,form[data-save]').forEach(form=>form.addEventListener('submit',async event=>{
 event.preventDefault();const button=event.submitter,status=form.querySelector('[role=status]');button.disabled=true;
 try{
  let body=form.dataset.save,url='/api/readiness/records';
  if(form.id==='readiness-policy'){
   const data=Object.fromEntries(new FormData(form));JSON.parse(data.config);
   body='{"version":'+JSON.stringify(data.version)+',"actor":'+JSON.stringify(data.actor)+',"reason":'+JSON.stringify(data.reason)+',"config":'+data.config+'}';
   url='/api/readiness/policies';
  }
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body});const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
  if(form.id==='readiness-policy'){const next=new URL(location.href);next.searchParams.set('policy_version',result.version);location.assign(next.pathname+next.search);}
  else location.assign('/readiness/records/'+encodeURIComponent(result.id));
 }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
}));
</script>"""
