"""Local APIs and runner-facing race goal and training preferences."""
import json
from urllib.parse import urlencode
from uuid import uuid4

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


@router.get("/api/runner-settings/availability/{week}")
def calendar(request: Request, week: str, preferences_version: str | None = None,
             availability_version: str | None = None, as_of: str | None = None):
    try:
        with request.app.state.store.history() as db:
            return settings.calendar_limits(db, week, preferences_version=preferences_version,
                                            availability_version=availability_version, as_of=as_of)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, OverflowError) as exc:
        invalid(exc)


def duration(seconds):
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def textarea(label, name, value=None, required=False):
    original = json.dumps(value, ensure_ascii=True)
    return (f'<label>{e(label)}<textarea name="{name}" data-original="{e(original)}" '
            f'{"required" if required else ""} rows="2" style="width:100%;box-sizing:border-box">\n'
            f'{e(value)}</textarea></label>')


def constraint_row(item):
    return ('<fieldset class="constraint"><legend>Special constraint</legend>'
            + textarea("Applies to", "scope", item.get("scope"), required=True)
            + textarea("Constraint in your words", "text", item.get("text"), required=True)
            + f'<label>From local date<input type="date" name="from_date" value="{e(item.get("from_date"))}"></label>'
            + f'<label>Through local date<input type="date" name="through_date" value="{e(item.get("through_date"))}"></label>'
            + '<button type="button" class="remove-constraint">Remove constraint</button></fieldset>')


def form_open(record):
    return (f'<form class="settings-form" action="/api/runner-settings/{record["kind"]}" method="post" '
            f'data-revision="{record["version_id"]}" data-request="{uuid4()}">')


def signature():
    return ('<label>Decided by<input name="actor" autocomplete="name" maxlength="100" required></label>'
            + textarea("Reason for this decision", "reason", required=True))


def goal_editor(record):
    return (form_open(record) + '<fieldset><legend>Revise the race goal</legend>'
            f'<label>New finish time (hours:minutes:seconds)<input name="finish" value="{duration(record["target_duration_s"])}" '
            'pattern="[0-9]+:[0-5][0-9]:[0-5][0-9]" required></label>'
            + signature() + '<label><input type="checkbox" name="confirm" required> I choose to change my race goal</label>'
            '<button type="submit">Record goal decision</button><p role="status"></p></fieldset></form>')


def preference_editor(record):
    limits = record["special_constraints"]
    options = [("unknown", "Not specified", limits is None), ("none", "No special constraints", limits == []),
               ("listed", "Enter constraints", bool(limits))]
    return (form_open(record) + '<fieldset><legend>Update training preferences</legend>'
            f'<label>Maximum running sessions per day (optional)<input type="number" min="0" max="10" step="1" '
            f'name="cap" value="{e(record["max_sessions_per_day"])}"></label>'
            + textarea("Your strength exercise and loading choices (optional)", "strength", record["strength_exercise_choices"])
            + '<div><label for="constraint-state">Special constraints</label><select id="constraint-state" name="constraint_state">'
            + ''.join(f'<option value="{k}" {"selected" if selected else ""}>{label}</option>' for k, label, selected in options)
            + '</select></div><div class="constraint-controls"><div class="constraint-list">'
            + ''.join(constraint_row(item) for item in limits or [])
            + '</div><button type="button" class="add-constraint">Add constraint</button></div>'
            + signature() + '<button type="submit">Save training preferences</button><p role="status"></p></fieldset></form>'
            + '<template id="new-constraint">' + constraint_row({}) + '</template>')


def presentation(record):
    if record["kind"] == "goal":
        body = (f'<p><strong>{duration(record["target_duration_s"])}</strong> on <time>{record["race_local_date"]}</time>. '
                f'Optional review <time>{record["review_date"]}</time>.</p>')
        if record["revision"] == 0:
            body += f'<p>Original goal from the {e(record["source"])}.</p>'
    else:
        cap, strength, constraints = (record[k] for k in ("max_sessions_per_day", "strength_exercise_choices", "special_constraints"))
        body = '<dl><dt>Maximum running sessions per day</dt><dd>' + ("Not specified" if cap is None else str(cap)) + '</dd>'
        body += '<dt>Strength exercise choice and loading</dt><dd>Runner controlled.</dd>'
        if strength is not None:
            body += '<dd style="white-space:pre-wrap">' + e(strength) + '</dd>'
        body += '<dt>Special constraints</dt><dd>'
        if constraints:
            body += '<ul>' + ''.join(
                f'<li><strong style="white-space:pre-wrap">{e(c["scope"])}</strong><div style="white-space:pre-wrap">{e(c["text"])}</div>'
                f'{e(c["from_date"] or "Open start")} to {e(c["through_date"] or "open end")} (London dates)</li>' for c in constraints) + '</ul>'
        else:
            body += "Not specified" if constraints is None else "Runner entered no special constraints."
        body += '</dd></dl><p>Each week’s available windows and daily permission for doubles are recorded in <a href="/availability">weekly availability</a>.</p>'
    if record["revision"]:
        body += (f'<p>Revision {record["revision"]} · {e(record["actor"])} · <time>{record["decided_at"]}</time></p>'
                 f'<p style="white-space:pre-wrap">{e(record["reason"])}</p>')
    return body


SCRIPT = """
<script>
for (const form of document.querySelectorAll('.settings-form')) {
  const active = form.elements;
  const kind = form.action.split('/').pop();
  const status = form.querySelector('[role=status]');
  const box = form.querySelector('.constraint-controls');
  const list = form.querySelector('.constraint-list');
  const addConstraint = () => {
    list.append(document.querySelector('#new-constraint').content.cloneNode(true));
    list.lastElementChild.querySelector('textarea').focus();
  };
  const updateConstraints = () => {
    box.hidden = active.constraint_state.value !== 'listed';
    for (const input of box.querySelectorAll('input,textarea')) input.disabled = box.hidden;
  };
  const renew = () => { form.dataset.request = crypto.randomUUID(); status.textContent = ''; };
  if (box) {
    updateConstraints();
    active.constraint_state.addEventListener('change', () => {
      updateConstraints();
      if (!box.hidden && !list.children.length) addConstraint();
    });
    form.querySelector('.add-constraint').addEventListener('click', () => { addConstraint(); renew(); });
    list.addEventListener('click', event => {
      if (!event.target.matches('.remove-constraint')) return;
      event.target.closest('.constraint').remove();
      if (!list.children.length) active.constraint_state.value = 'none';
      updateConstraints();
      active.constraint_state.focus();
      renew();
    });
  }
  form.addEventListener('input', renew);
  form.addEventListener('change', renew);
  const exact = field => {
    const original = JSON.parse(field.dataset.original);
    if (original !== null && original.replace(/\\r\\n?/g, '\\n') === field.value) return original;
    return field.value;
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = {request_id: form.dataset.request, expected_revision: form.dataset.revision,
      actor: active.actor.value, reason: exact(active.reason)};
    if (kind === 'goal') {
      const [hours, minutes, seconds] = active.finish.value.split(':').map(Number);
      body.action = 'REVISE_GOAL';
      body.target_duration_s = hours * 3600 + minutes * 60 + seconds;
      if (!active.confirm.checked || !Number.isSafeInteger(body.target_duration_s) || body.target_duration_s <= 0) {
        status.textContent = 'Enter a positive finish time and confirm your decision.'; return;
      }
    } else {
      body.action = 'SET_PREFERENCES';
      body.max_sessions_per_day = active.cap.value === '' ? null : Number(active.cap.value);
      body.strength_exercise_choices = exact(active.strength) || null;
      body.special_constraints = null;
      if (active.constraint_state.value === 'none') body.special_constraints = [];
      if (active.constraint_state.value === 'listed') body.special_constraints = [...list.children].map(row => {
        const field = name => row.querySelector('[name=' + name + ']');
        return {scope: exact(field('scope')), text: exact(field('text')), from_date: field('from_date').value || null,
          through_date: field('through_date').value || null};
      });
    }
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      const response = await fetch(form.action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Check the settings fields.');
      location.assign(result.html_url);
    } catch (error) { status.textContent = error.message || 'Could not reach the app. Retry to check this decision.'; }
    finally { button.disabled = false; }
  });
}
</script>
"""


@router.get("/runner-settings", response_class=HTMLResponse)
def page(request: Request, as_of: str | None = None):
    suffix = "?" + urlencode({"as_of": as_of}) if as_of is not None else ""
    body = (f'<p>Decisions known by {e(as_of)}. <a href="/runner-settings">Open current settings</a></p>' if suffix else "")
    for kind, title in (("goal", "Marathon goal"), ("preferences", "Training preferences")):
        state = current(request, kind, as_of)
        record = state["current"]
        body += f'<section><h2>{title}</h2>' + presentation(record)
        if not suffix:
            body += goal_editor(record) if kind == "goal" else preference_editor(record)
        body += f'<details><summary>{title} history</summary><ul>'
        for rev in state["versions"]:
            href = f'/runner-settings/{kind}/versions/{rev["version_id"]}{suffix}'
            label = "Original goal" if kind == "goal" else "Preferences before entry"
            if rev["revision"]:
                label = f'Revision {rev["revision"]} · {rev["decided_at"]}'
            body += f'<li><a href="{e(href)}">{e(label)}</a></li>'
        body += f'</ul></details><p><a href="/api/runner-settings/{kind}{e(suffix)}">{title} JSON</a></p></section>'
    return views.page("Runner settings", body + ("" if suffix else SCRIPT))


@router.get("/runner-settings/{kind}/versions/{version_id}", response_class=HTMLResponse)
def historical(request: Request, kind: settings.Kind, version_id: str, as_of: str | None = None):
    record = version(request, kind, version_id, as_of)
    suffix = "?" + urlencode({"as_of": as_of}) if as_of is not None else ""
    title = "Marathon goal" if kind == "goal" else "Training preferences"
    endpoint = f"/api/runner-settings/{kind}/versions/{version_id}{suffix}"
    body = (presentation(record) + f'<p><a href="{e(endpoint)}">This revision as JSON</a> · '
            f'<a href="/runner-settings{e(suffix)}">Runner settings</a></p>')
    return views.page(f"{title} · revision {record['revision']}", body)
