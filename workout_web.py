"""Editable workout labels, source-linked inference and immutable history."""
from decimal import Decimal
from html import escape
import json
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import context_data
import effort_views
import views
import workout_labels as labels


router = APIRouter()


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except labels.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from None


def parse(raw):
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=context_data.reject_constant, object_pairs_hook=context_data.unique_object)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Use a UTF-8 JSON object with unique fields and finite values.") from None


class Infer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_version_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_version: str = Field(default=labels.DEFAULT_CONFIG_VERSION, pattern=r"^[A-Za-z0-9._-]{1,80}$")


@router.get("/api/activities/{activity_id}/workout")
def data(request: Request, activity_id: str, as_of: str | None = None, knowledge_as_of: str | None = None):
    return call(labels.read, request.app.state.store, activity_id, as_of=as_of, knowledge_as_of=knowledge_as_of)


@router.post("/api/activities/{activity_id}/workout")
async def save(request: Request, activity_id: str):
    raw = await request.body()
    if len(raw) > 100_000:
        raise HTTPException(413, "Label document is too large.")
    result = call(labels.save, request.app.state.store, activity_id, parse(raw))
    return {**result, "html_url": f"/activities/{result['workout']['activity_id']}/workout"}


@router.post("/api/activities/{activity_id}/workout/infer")
def infer(request: Request, activity_id: str, choice: Infer):
    result = call(labels.infer, request.app.state.store, activity_id, **choice.model_dump())
    return {"workout": result, "html_url": f"/activities/{result['activity_id']}/workout"}


@router.post("/api/workouts/import")
async def import_label(request: Request, document: UploadFile):
    raw = await document.read(100_001)
    await document.close()
    if len(raw) > 100_000:
        raise HTTPException(413, "Import one label document of at most 100 KB.")
    value = parse(raw)
    if not isinstance(value, dict) or set(value) != {"activity_id", "label"}:
        raise HTTPException(422, "Supply one activity_id and label object.")
    # Store exact supplied bytes; a rejected document can leave an unreferenced
    # content-addressed archive object, never a partially committed label.
    raw_sha = request.app.state.store.archive.put(raw)
    result = call(labels.save, request.app.state.store, value["activity_id"], value["label"], import_sha256=raw_sha)
    return {**result, "html_url": f"/activities/{result['workout']['activity_id']}/workout"}


@router.get("/api/workouts/configs")
def configs(request: Request):
    return labels.configs(request.app.state.store)


@router.post("/api/workouts/configs/{version}")
async def config(request: Request, version: str):
    raw = await request.body()
    if len(raw) > 50_000:
        raise HTTPException(413, "Configuration is too large.")
    return call(labels.register_config, request.app.state.store, version, parse(raw))


@router.get("/api/workouts/changes")
def changes(request: Request, after: str | None = None):
    return {"events": call(labels.changes, request.app.state.store, after)}


def field(name, title, value="", required=False):
    return f'<label>{escape(title)}<input name="{name}" value="{escape(str(value or ""))}"' + (' required' if required else '') + '></label>'


def select(name, title, options, current=None):
    return f'<label>{escape(title)}<select name="{name}">' + ''.join(f'<option value="{escape(v)}"' + (' selected' if current == v else '') + f'>{escape(v)}</option>' for v in options) + '</select></label>'


SCRIPT = """<script>
document.querySelectorAll('form[data-label]').forEach(form => form.addEventListener('submit', async event => {
 event.preventDefault(); const button=event.submitter;button.disabled=true; const status=document.getElementById('workout-status');status.textContent='Saving…';
 try {
 const fields=Object.fromEntries(new FormData(form));let body,headers={'Content-Type':'application/json'},url=form.action;
 if(form.dataset.label==='upload'){body=new FormData(form);headers={};}
 else if(form.dataset.label==='config'){url='/api/workouts/configs/'+encodeURIComponent(fields.version);body=fields.config;}
 else {if('tags' in fields){fields.tags=JSON.parse(fields.tags||'[]');
 if(!Array.isArray(fields.tags)||!fields.tags.every(tag=>typeof tag==='string'))throw new Error('Tags must be a JSON array of strings.');}
 if('supersedes' in fields&&!fields.supersedes)fields.supersedes=null;
 if('event_utc' in fields&&!fields.event_utc)fields.event_utc=null;
 if('planned_session_ref' in fields&&!fields.planned_session_ref)fields.planned_session_ref=null;
 if('supporting_efforts' in fields)fields.supporting_efforts=JSON.parse(fields.supporting_efforts||'[]');body=JSON.stringify(fields);}
 const response=await fetch(url,{method:'POST',headers,body});const result=await response.json();
 if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Check the document fields.');
 if(result.html_url)location.assign(result.html_url);else location.reload();
 }catch(error){status.textContent=error.message;button.disabled=false;}
}));
document.querySelectorAll('[data-clear-references]').forEach(button=>button.addEventListener('click',()=>{document.getElementById(button.dataset.clearReferences).value='[]';}));
</script>"""


@router.get("/workouts", response_class=HTMLResponse)
def index(request: Request):
    with request.app.state.store.history() as db:
        rows = list(db.execute("SELECT id,name FROM activities ORDER BY id DESC")) if db else []
    content = '<p>Planned intent, inferred completed structure and runner corrections remain separate. Strength stays in <a href="/context">Context</a> as STRENGTH_CONDITIONING and never adds running kilometres.</p><ul>'
    content += ''.join(f'<li><a href="/activities/{r["id"]}/workout">Activity {r["id"]}: {escape(r["name"] or "Recorded run")}</a></li>' for r in rows) + '</ul>'
    content += '<h2>Import a label</h2><p>One JSON document containing activity_id and label. Existing labels need their current supersedes ID; source versions must match.</p><form data-label="upload" method="post" action="/api/workouts/import" enctype="multipart/form-data"><label>Label JSON file<input type="file" name="document" accept=".json,application/json" required></label><button>Import label</button></form>'
    content += '<h2>Classifier configuration</h2><p>Exact completed step-name mappings are editable software conventions, not physiological thresholds. A title or unexecuted workout step alone is never enough.</p>'
    content += effort_views.details(labels.configs(request.app.state.store), 'Saved classifier configurations')
    content += '<form data-label="config" method="post" action="/api/workouts/configs">' + field('version', 'New configuration version', required=True)
    content += '<label>Mapping JSON<textarea name="config" rows="8" required>' + escape(json.dumps(labels.DEFAULT_CONFIG, indent=2)) + '</textarea></label><button>Save classifier configuration</button></form>'
    content += '<p><a href="/api/workouts/changes">Versioned change events</a></p><p id="workout-status" role="status"></p>'
    return views.page('Workout labels', '<p><a href="/workout-templates">Workout templates</a></p>' + content + SCRIPT)


@router.get("/activities/{activity_id}/workout", response_class=HTMLResponse)
def page(request: Request, activity_id: str, as_of: str | None = None, knowledge_as_of: str | None = None):
    row = data(request, activity_id, as_of, knowledge_as_of)
    aid = row['activity_id']
    content = f'<p><a href="/activities/{aid}">Original activity {aid}</a> · <a href="/activities/{aid}/efforts">Continuous efforts and test evidence</a> · <a href="/workouts">All workout labels</a></p>'
    content += '<p>Labels describe intent and recorded context. They do not change distance, timing, trace QC or maximality. Source titles and steps remain inspectable; corrections never erase them.</p>'
    content += f'<p id="effective-label">Current actual classification: <strong>{escape(row["effective"])}</strong></p>'
    if row['source_changed']:
        content += '<p><strong>Source changed:</strong> prior labels refer to older evidence. Recompute the inference and review corrections explicitly.</p>'
    content += '<form method="get">' + field('as_of', 'Event cutoff UTC', as_of) + field('knowledge_as_of', 'Knowledge cutoff UTC', knowledge_as_of) + '<button>Inspect historical labels</button></form>'
    for layer, title in (('planned', 'Planned intent'), ('inferred', 'Inferred completed workout'), ('corrected', 'Runner correction')):
        entry = row['layers'][layer]
        content += f'<h2>{title}</h2><p id="{layer}-label">' + escape(entry['payload']['primary'] if entry else 'UNKNOWN — no record') + '</p>'
        if entry:
            content += effort_views.details(entry, title + ' evidence and version')
        if layer == 'inferred':
            content += f'<form data-label="infer" action="/api/activities/{aid}/workout/infer" method="post"><input type="hidden" name="canonical_version_id" value="{row["canonical_version_id_now"]}">'
            content += select('config_version', 'Classifier version', [c['version'] for c in labels.configs(request.app.state.store)]) + '<button>Infer from recorded structure</button></form>'
        else:
            value = entry['payload'] if entry else {}
            content += f'<form data-label="edit" action="/api/activities/{aid}/workout" method="post"><fieldset><legend>Edit {title.lower()}</legend>'
            for key, val in {'layer': layer, 'canonical_version_id': row['canonical_version_id_now'], 'supersedes': entry['id'] if entry else ''}.items():
                content += f'<input type="hidden" name="{key}" value="{val}">'
            content += select('primary', title + ' type', [v for v in labels.TYPES if v != 'STRENGTH_CONDITIONING'], value.get('primary'))
            content += field('tags', title + ' tags', json.dumps(value.get('tags', [])))
            content += '<p>Enter tags as a JSON array, such as ["BENCHMARK_TAG"]. Commas inside quotes stay in one tag. Use [] to remove all tags.</p>'
            content += field('actor', title + ' recorded by', value.get('actor',''), required=True)
            content += field('source', title + ' source', value.get('source', 'athlete'), True) + field('source_reference', title + ' source reference', value.get('source_reference', ''), True)
            content += field('event_utc', title + ' observation time UTC', entry['event_utc'] if entry else '')
            content += field('planned_session_ref', title + ' session reference', value.get('planned_session_ref',''))
            content += '<p>Keep the session reference unchanged, or clear it to remove the link.</p>'
            content += f'<label>{title} explanation<textarea name="explanation" required maxlength="4000">{escape(value.get("explanation", ""))}</textarea></label>'
            references = [{k:r[k] for k in ('version_id','candidate_id')} for r in value.get('supporting_efforts', [])]
            content += f'<label>{title} supporting effort references (JSON)<textarea id="{layer}-supporting-efforts" name="supporting_efforts">' + escape(json.dumps(references,indent=2)) + '</textarea></label>'
            content += f'<p>Edit these owned version/candidate references to change the links.</p><button type="button" data-clear-references="{layer}-supporting-efforts">Remove {title.lower()} supporting efforts</button>'
            content += f'<button>Save {title.lower()}</button></fieldset></form>'
    content += effort_views.details(row['history'], 'All label revisions') + effort_views.details(row['withheld'], 'Records excluded by cutoffs')
    query = escape(urlencode({k:v for k,v in {'as_of':as_of,'knowledge_as_of':knowledge_as_of}.items() if v}))
    content += f'<p><a href="/api/activities/{aid}/workout?{query}">Workout JSON</a></p><p id="workout-status" role="status"></p>'
    return views.page('Activity workout labels', content + SCRIPT)
