"""Read-only fixed-session calendar diagnostic."""
from datetime import datetime, timedelta
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import availability
from availability_web import e, invalid, reject_constant, unique_object
from calendar_fit import Check, LONDON, check_calendar
import views
import workout_templates as templates


router = APIRouter()


@router.post("/api/calendar-fit")
def check(query: Check, request: Request):
    try:
        with request.app.state.store.history() as db:
            return check_calendar(db, query)
    except (ValueError, OverflowError) as exc:
        invalid(exc)


def session_row(options):
    return ('<fieldset class="fixed-session"><legend>Workout 1</legend>'
            '<label>Workout revision<select name="workout" required><option value="">Choose a revision</option>'
            + options + '</select></label><label>Fixed block distances in metres (optional JSON object)'
            '<textarea name="distances" rows="2" placeholder=\'{"block-id": 1500}\'></textarea></label></fieldset>')


EDITOR_SCRIPT = """
<script>
const list = document.getElementById("sessions");
const addButton = document.getElementById("add-session");
addButton.addEventListener("click", () => {
  const row = document.getElementById("new-session").content.firstElementChild.cloneNode(true);
  row.querySelector("legend").textContent = "Workout " + (list.children.length + 1);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove workout";
  remove.addEventListener("click", () => {
    row.remove();
    Array.from(list.children).forEach((item, i) => { item.querySelector("legend").textContent = "Workout " + (i + 1); });
    addButton.focus();
  });
  row.append(remove);
  list.append(row);
  row.querySelector("select").focus();
});
document.getElementById("fit-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = form.querySelector('[role="status"]');
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  status.textContent = "Checking the fixed workouts…";
  try {
    const response = await fetch(form.action, {method: "POST", body: new URLSearchParams(new FormData(form))});
    const report = new DOMParser().parseFromString(await response.text(), "text/html");
    if (!response.ok) throw new Error(report.querySelector('[role="alert"]')?.textContent || "The check could not be completed.");
    document.title = report.title;
    document.querySelector("main").replaceWith(report.querySelector("main"));
    const heading = document.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus();
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
});
</script>"""


@router.get("/calendar-fit", response_class=HTMLResponse)
def diagnostic(request: Request, week: str | None = None, availability_version_id: templates.Hash | None = None):
    if week is None:
        today = datetime.now(LONDON).date()
        week = (today - timedelta(days=today.weekday())).isoformat()
    store = request.app.state.store
    try:
        state = availability.read(store, week)
        if availability_version_id and all(v["id"] != availability_version_id for v in state["versions"]):
            raise ValueError("That availability revision does not belong to this week.")
        with store.history() as db:
            revisions = [v for template in templates.index(db)
                         for v in templates.index(db, template_id=template["template_id"])]
    except (ValueError, OverflowError) as exc:
        invalid(exc)
    body = (f'<form method="get"><label>Week beginning Monday<input type="date" name="week" value="{e(week)}" required></label>'
            '<button>Open week</button></form>'
            f'<p><a href="/availability?week={e(week)}">Weekly availability</a> · '
            '<a href="/workout-templates">Workout templates</a></p>')
    if not state["versions"]:
        body += '<p>Save availability for this week to check a placement.</p>'
    if not revisions:
        body += '<p>Save a workout template with block distances and pace bounds.</p>'
    if not state["versions"] or not revisions:
        return views.page("Check workout time", body)
    selected = availability_version_id or state["current"]["version_id"]
    versions = ''.join(f'<option value="{e(v["id"])}" {"selected" if v["id"] == selected else ""}>'
                       f'Revision {v["revision"]} · {e(v["received_at"])}</option>' for v in state["versions"])
    options = ''.join(f'<option value="{e(v["template_id"])}/{e(v["version_id"])}">'
                      f'{e(v["name"])} · revision {v["revision"]} · {e(v["template_id"])}</option>' for v in revisions)
    body += (f'<form id="fit-form" method="post" action="/calendar-fit"><input type="hidden" name="week" value="{e(week)}">'
             f'<label>Availability revision<select name="availability_version_id">{versions}</select></label>'
             '<div id="sessions">' + session_row(options) + '</div><button type="button" id="add-session">Add workout</button>'
             '<label>Earliest allowed start (optional, with UTC offset)<input name="earliest" placeholder="2026-01-12T08:00:00+00:00"></label>'
             '<label>Occupied intervals (optional JSON list)<textarea name="occupied" rows="3" placeholder="[]"></textarea></label>'
             '<details><summary>Occupied interval format</summary><p>Each interval needs an ID, a kind of RUNNING or NON_RUNNING, '
             'and start_utc and end_utc timestamps with offsets. Running starts use a daily session allowance.</p>'
             '<code>{"id":"appointment","kind":"NON_RUNNING","start_utc":"2026-01-12T09:00Z","end_utc":"2026-01-12T10:00Z"}</code></details>'
             '<button type="submit">Check placement</button><p role="status"></p></form><template id="new-session">'
             + session_row(options) + '</template>' + EDITOR_SCRIPT)
    return views.page("Check workout time", body)


REASONS = {
    "CONCRETE_INTERVALS": "Every fixed workout fits its conservative duration in the declared calendar.",
    "TOTAL_FREE_TIME": "The available time is shorter than the time required by the fixed workouts.",
    "START_DAY_LIMITS": "Too few running starts remain on days with available time.",
    "NO_CONTINUOUS_SLOT": "At least one workout has no continuous permitted interval long enough for it.",
    "EXHAUSTIVE_ORDER_SEARCH": "Every distinct workout-duration order failed the time or start-day limits.",
    "SEARCH_BUDGET": "The search reached its budget before it could establish a result.",
    "UNKNOWN_AVAILABILITY": "A missing calendar declaration could change this result.",
    "PACE_RANGE_STRADDLES_CALENDAR": "The faster supplied pace could fit. The conservative duration cannot fit.",
    "MISSING_INPUT": "A required saved revision or supported block pace is missing.",
}


@router.post("/calendar-fit", response_class=HTMLResponse)
async def form_result(request: Request):
    form = await request.form()
    try:
        def unpack(value, fallback):
            return json.loads(value or fallback, object_pairs_hook=unique_object, parse_constant=reject_constant)

        workouts = form.getlist("workout")
        distances = form.getlist("distances")
        if not workouts or len(workouts) != len(distances):
            raise ValueError("Select a saved revision for each workout.")
        sessions = []
        for i, (ref, fixed) in enumerate(zip(workouts, distances), 1):
            key, version = ref.split("/", 1)
            sessions.append({"session_id": f"session-{i}", "template_id": key, "template_version_id": version,
                             "distances_m": unpack(fixed, "{}")})
        query = Check.model_validate({"week": form.get("week"), "availability_version_id": form.get("availability_version_id"),
                                      "sessions": sessions, "occupied": unpack(form.get("occupied"), "[]"),
                                      "earliest_start_utc": form.get("earliest") or None})
        with request.app.state.store.history() as db:
            answer = check_calendar(db, query)
    except (ValueError, OverflowError, RecursionError) as exc:
        return HTMLResponse(views.page("Check workout time", f'<p role="alert">{e(exc)}</p>'
                                       '<p>Return to the form to correct the input.</p>'), status_code=422)
    body = (f'<p><strong>{e(answer["status"])}</strong></p><p>{e(REASONS[answer["reason"]])}</p>'
            f'<p>Conservative upper duration · {e(answer["conservative_fit"]["status"])}</p>')
    if answer["fixed_total_m"] is not None:
        body += f'<p>Fixed running distance · {answer["fixed_total_m"]:,} m</p>'
    sessions = {s["session_id"]: s for s in answer["sessions"]}
    if answer["placements"]:
        body += '<ol>'
        for slot in answer["placements"]:
            s = sessions[slot["session_id"]]
            a, b = [datetime.fromisoformat(slot[key]).astimezone(LONDON) for key in ("start_utc", "end_utc")]
            body += (f'<li><strong>{e(s["name"])} · {s["total_m"]:,} m</strong><br>'
                     f'{e(a.isoformat())} to {e(b.isoformat())} · Europe/London · {s["duration_upper_seconds"]:,} s</li>')
        body += '</ol>'
    for reason in answer.get("missing", []):
        body += f'<p>{e(reason.get("session_id", "Calendar"))} · {e(reason["kind"])} · {e(", ".join(reason.get("block_ids", [])))}</p>'
    args = urlencode({"week": query.week, "availability_version_id": query.availability_version_id})
    body += (f'<p><a href="/calendar-fit?{e(args)}">Check another placement</a></p>'
             f'<details><summary>Exact UTC intervals, source revisions and assumptions</summary>'
             f'<pre style="white-space:pre-wrap">{e(json.dumps(answer, ensure_ascii=False, indent=2))}</pre></details>')
    return views.page("Workout time result", body)
