"""Weekly availability entry and immutable revision inspection in the local app."""
from datetime import datetime, timedelta
from html import escape
import json
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

import availability
import views

router = APIRouter()


def e(value):
    return escape("" if value is None else str(value), quote=True)


def invalid(exc):
    if isinstance(exc, availability.Conflict):
        raise HTTPException(409, str(exc)) from None
    if isinstance(exc, ValidationError):
        raise HTTPException(422, "; ".join(".".join(str(x) for x in r["loc"]) + ": " + r["msg"] for r in exc.errors())) from None
    raise HTTPException(422, str(exc)) from None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are not accepted.")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("JSON numbers must be finite.")


@router.post("/api/availability/{week}")
async def submit(request: Request, week: str):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256 * 1024:
            raise HTTPException(413, "Availability submissions are limited to 256 KiB.")
    try:
        raw = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
        result = availability.save(request.app.state.store, week, raw)
        return {**result, "html_url": f"/availability?week={week}",
                "version_url": f"/api/availability/{week}/versions/{result['record']['version_id']}"}
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    except RecursionError:
        raise HTTPException(422, "Availability JSON is nested too deeply.") from None


@router.get("/api/availability/{week}")
def current(request: Request, week: str):
    try:
        return availability.read(request.app.state.store, week)
    except (ValueError, OverflowError) as exc:
        invalid(exc)


@router.get("/api/availability/{week}/versions/{version_id}")
def version(request: Request, week: str, version_id: str):
    try:
        result = availability.read(request.app.state.store, week, version_id)
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    if result is None:
        raise HTTPException(404, "Availability version not found in this week.")
    return result


def field(identity, label, value="", kind="text", extra=""):
    return f'<div><label for="{e(identity)}">{e(label)}</label><input id="{e(identity)}" type="{kind}" value="{e(value)}" {extra}></div>'


def choice(identity, label, values, selected, extra=""):
    return f'<div><label for="{e(identity)}">{e(label)}</label><select id="{e(identity)}" {extra}>' + ''.join(
        f'<option value="{e(value)}"{" selected" if value == selected else ""}>{e(title)}</option>' for value, title in values) + '</select></div>'


def window_fields(identity, row=None):
    row = row or {}
    result = '<fieldset class="time-window"><legend>Time window</legend>'
    for boundary, title in (("start", "Start"), ("end", "End")):
        values = row.get(boundary, {})
        result += field(f"{identity}-{boundary}", f"{title} London date and time", values.get("local", ""), "datetime-local", f'data-boundary="{boundary}" step="any"')
    result += '<details><summary>Clock-change choices and offsets</summary><p>Ordinary times resolve automatically. For a repeated autumn time, choose its first or second occurrence, or supply its UTC offset. Spring-gap times cannot be used.</p>'
    for boundary, title in (("start", "Start"), ("end", "End")):
        values = row.get(boundary, {})
        fold = values.get("fold")
        result += choice(f"{identity}-{boundary}-fold", f"{title} clock occurrence", [("", "Automatic when unambiguous"), ("0", "First occurrence"), ("1", "Second occurrence")], "" if fold is None else str(fold), f'data-fold="{boundary}"')
        result += field(f"{identity}-{boundary}-offset", f"{title} UTC offset (optional)", values.get("offset", ""), extra=f'data-offset="{boundary}" placeholder="+01:00 or +00:00"')
    return result + '</details><button type="button" class="remove-window">Remove window</button></fieldset>'


def editor(state):
    record = state["current"]
    previous = record["version_id"] if record else ""
    supplied = {row["date"]: row for row in record["submitted"]["days"]} if record else {}
    body = f'<form id="availability-editor" action="/api/availability/{state["week"]}" method="post" data-previous="{previous}" data-request="{uuid.uuid4()}">'
    body += '<p>Choose a state for every day. “Not supplied” keeps that day unknown; “No availability” explicitly records no windows. Overnight windows may finish on the following day but must stay within this week. Daily session limits apply to the session’s London start date.</p>'
    body += field("availability-actor", "Submitted by", record["actor"] if record else "", extra='name="actor" maxlength="100" required')
    body += field("availability-reason", "Change note (optional)", "", extra='name="reason" maxlength="2000"')
    for index, day in enumerate(state["dates"]):
        row = supplied.get(day, {"state": "UNKNOWN", "max_sessions": None, "doubles_allowed": False, "windows": []})
        label = availability.calendar_day(day).strftime("%A %d %B")
        identity = f"day-{index}"
        body += f'<section class="availability-day" data-date="{day}"><h2>{e(label)}</h2>'
        body += choice(identity + "-state", "Availability for " + day, [("UNKNOWN", "Not supplied"), ("UNAVAILABLE", "No availability"), ("AVAILABLE", "Windows supplied")], row["state"], 'data-state')
        body += field(identity + "-cap", "Maximum sessions on " + day, row["max_sessions"] or 1, "number", 'data-cap min="1" max="10" step="1"')
        body += f'<label for="{identity}-doubles"><input id="{identity}-doubles" type="checkbox" data-doubles{" checked" if row["doubles_allowed"] else ""}> I permit doubles or more than one session on {day}</label>'
        body += '<div class="day-windows">' + ''.join(window_fields(f"{identity}-window-{i}", window) for i, window in enumerate(row["windows"])) + '</div>'
        body += '<button type="button" class="add-window">Add time window</button></section>'
    return body + '<p><button type="submit">Save weekly availability</button></p><p class="save-result" role="status" aria-live="polite"></p></form>'


SCRIPT = """
<script>
const form=document.getElementById('availability-editor');
if(form) {
 let sequence=1000;
 const renew=()=>{form.dataset.request=crypto.randomUUID();};
 const sync=day=>{
  const available=day.querySelector('[data-state]').value==='AVAILABLE';
  day.querySelector('[data-cap]').disabled=!available;
  day.querySelector('[data-doubles]').disabled=!available;
  day.querySelectorAll('.day-windows input,.day-windows select,.remove-window').forEach(input=>{input.disabled=!available;});
  day.querySelectorAll('[data-boundary]').forEach(input=>{input.required=available;});
 };
 form.querySelectorAll('.availability-day').forEach(day=>{
  sync(day);
  day.addEventListener('change',()=>sync(day));
  day.querySelector('.add-window').addEventListener('click',()=>{
   const template=document.getElementById('availability-window').innerHTML.replaceAll('WINDOW_ID','window-'+sequence++);
   day.querySelector('.day-windows').insertAdjacentHTML('beforeend',template);
   day.querySelector('[data-state]').value='AVAILABLE'; sync(day); renew();
   day.querySelector('.time-window:last-child [data-boundary=start]').focus();
  });
  day.addEventListener('click',event=>{if(event.target.classList.contains('remove-window')) {event.target.closest('.time-window').remove();renew();}});
 });
 form.addEventListener('input',renew); form.addEventListener('change',renew);
 form.addEventListener('submit',async event=>{
  event.preventDefault();
  const message=form.querySelector('.save-result'), button=event.submitter;
  const days=[...form.querySelectorAll('.availability-day')].map(day=>{
   const state=day.querySelector('[data-state]').value;
   const windows=state!=='AVAILABLE'?[]:[...day.querySelectorAll('.time-window')].map(row=>{
    const item={};
    for(const boundary of ['start','end']) {
     const fold=row.querySelector('[data-fold='+boundary+']').value;
     item[boundary]={local:row.querySelector('[data-boundary='+boundary+']').value,
      fold:fold===''?null:Number(fold),offset:row.querySelector('[data-offset='+boundary+']').value||null};
    }
    return item;
   });
   return {date:day.dataset.date,state,max_sessions:state==='AVAILABLE'?Number(day.querySelector('[data-cap]').value):state==='UNAVAILABLE'?0:null,
    doubles_allowed:state==='AVAILABLE'&&day.querySelector('[data-doubles]').checked,windows};
  });
  const payload={request_id:form.dataset.request,expected_revision:form.dataset.previous||null,
   actor:form.elements.actor.value,reason:form.elements.reason.value,days};
  button.disabled=true;message.textContent='Saving…';
  try {
   const response=await fetch(form.getAttribute('action'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
   const result=await response.json();
   if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Check the availability fields.');
   location.assign(result.html_url);
  } catch(error) {message.textContent=error.message||'Could not reach the app. Retry unchanged to check whether this submission was saved.';}
  finally {button.disabled=false;}
 });
}
</script>
"""


@router.get("/availability", response_class=HTMLResponse)
def page(request: Request, week: str | None = None, version: str | None = None):
    if week is None:
        today = datetime.now(ZoneInfo(availability.ZONE)).date()
        week = (today - timedelta(days=today.weekday())).isoformat()
    try:
        state = availability.read(request.app.state.store, week)
        record = availability.read(request.app.state.store, week, version) if version else state["current"]
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    if version and record is None:
        raise HTTPException(404, "Availability version not found in this week.")
    body = '<p><a href="/context">Context and weekly readiness</a></p>'
    body += '<form method="get" action="/availability"><fieldset><legend>Choose a week</legend>' + field("availability-week", "Week beginning Monday", week, "date", 'name="week" required') + '<button>Open week</button></fieldset></form>'
    body += '<p>All windows use Europe/London. Saving availability records your calendar; it does not approve a target or activate a training plan.</p>'
    shown = record["state"] if record else "UNKNOWN — not entered"
    body += f'<p class="availability-status">Availability: <strong>{e(shown)}</strong></p>'
    body += f'<p>Week bounds: {e(state["week_start_utc"])} to {e(state["week_end_utc"])} (end excluded).</p>'
    if record:
        body += f'<h2>Saved revision {record["revision"]}</h2><p>Submitted by {e(record["actor"])} at {e(record["received_at"])}. {e(record["reason"])}</p>'
        body += f'<p>Total distinct available time: <strong>{record["union_elapsed_seconds"]:g} seconds</strong>. Overlaps and touching windows are counted once.</p>'
        if record["state"] == "EMPTY":
            body += '<p>This is an explicitly empty week, not a missing submission.</p>'
        if record["union_windows"]:
            body += '<ul>' + ''.join(f'<li>{e(w["start_utc"])} → {e(w["end_utc"])} · {w["elapsed_seconds"]:g} seconds</li>' for w in record["union_windows"]) + '</ul>'
        body += '<details><summary>Original local choices, UTC bounds and daily limits</summary><pre style="white-space:pre-wrap">' + e(json.dumps(record, indent=2, ensure_ascii=False)) + '</pre></details>'
        body += f'<p><a href="/api/availability/{week}/versions/{record["version_id"]}">This immutable revision as JSON</a></p>'
    if version:
        body += f'<p>This revision is read-only. <a href="/availability?week={week}">Open current availability to edit</a>.</p>'
    else:
        body += editor(state) + '<template id="availability-window">' + window_fields("WINDOW_ID") + '</template>' + SCRIPT
    body += f'<h2>Revision history</h2><p><a href="/api/availability/{week}">Current availability JSON</a></p>'
    body += '<ul>' + ''.join(f'<li><a href="/availability?week={week}&amp;version={v["id"]}">Revision {v["revision"]}</a> · {e(v["received_at"])} · {e(v["actor"])}</li>' for v in state["versions"]) + '</ul>'
    return views.page("Weekly availability", body)
