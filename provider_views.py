"""Source choices and honest capability status, using local persisted evidence."""
from html import escape

import views


LABELS = {"GARMIN_ONLY": "Garmin only", "STRAVA_ONLY": "Strava only — analytics unavailable",
          "BOTH": "Both — Garmin imports, Strava unavailable",
          "GARMIN_PRIMARY_FALLBACK": "Garmin first — Strava fallback unavailable"}
STATES = {"POLICY_BLOCKED": "Unavailable under current Strava policy", "NOT_SELECTED": "Not selected",
          "AVAILABLE": "Runs imported", "EMPTY_RELEVANT": "No relevant runs returned",
          "AUTH_REQUIRED": "Authorization required", "FORBIDDEN": "Access denied", "LIMITED_SCOPE": "Activity access is limited",
          "NOT_FOUND": "Source not found or inaccessible", "RATE_LIMITED": "Rate limit reached",
          "UNSUPPORTED": "Unsupported response", "OFFLINE": "Garmin is offline",
          "UNAVAILABLE": "Provider unavailable", "INCOMPLETE": "Import incomplete",
          "INTERRUPTED": "Import interrupted", "RUNNING": "Import in progress", "COMPLETE": "Complete",
          "PARTIAL": "Partly complete", "NOT_NEEDED": "Not needed"}


def state(value):
    return escape(STATES.get(value, value.replace("_", " ").capitalize()))


def attempt_view(item):
    result = item["result"]
    garmin, strava = result["providers"]["garmin"], result["providers"]["strava"]
    fallback = ""
    if result["fallback_reason"]:
        cause = result["fallback_reason"].removeprefix("GARMIN_")
        fallback = f'<p>Fallback reason: {state(cause)}. Strava fallback: {state(result["fallback_state"])}.</p>'
    report = f'<p><a href="{item["import_html_url"]}">Open Garmin import and date coverage</a></p>' if item.get("import_html_url") else ""
    progress = '<script>setTimeout(() => location.reload(), 2000);</script>' if result["state"] == "RUNNING" else ""
    return f'''<section aria-label="Selected import result"><h2>Source import {item["id"]}</h2>
<p><strong>{state(result["state"])}</strong> · {escape(LABELS[item["mode"]])}</p>
<dl><dt>Garmin</dt><dd>{state(garmin["state"])}</dd><dt>Strava</dt><dd>{state(strava["state"])}</dd></dl>
<p>{len(result["activity_ids"])} distinct runs in this attempt. Selected sources:
{escape(", ".join(result["selected_sources"]) or "None")}.</p>{fallback}{report}{progress}
<p>An empty response does not establish an outage or prove that every run was recorded.</p>
<a href="{item["attempt_url"]}">Source import JSON</a></section>'''


def page(data, attempt=None):
    config = data["config"]
    options = "".join(f'<option value="{key}"{" selected" if key == config["mode"] else ""}>{escape(label)}</option>'
                      for key, label in LABELS.items())
    attempt = attempt or next(iter(data["attempts"]), None)
    activities = []
    for activity in data["activities"]:
        capability = activity["capability"]
        sources = ", ".join(f'{p["provider"]} {p["id"]}' for p in activity["provider_ids"]) or "FIT export"
        activities.append(f'''<li><a href="/activities/{activity["activity_id"]}/canonical">Activity {activity["activity_id"]}</a>
· {escape(sources)} · Tier {capability["tier"]}<small>Continuous effort evidence: {escape(capability["continuous_efforts"])}.
Timer events: {escape(capability["timer_event_coverage"])}. {len(activity["raw_sources"])} saved source records.</small></li>''')
    activity_list = "<ul>" + "".join(activities) + "</ul>" if activities else "<p>No processed activity evidence yet.</p>"
    coverage = data["providers"]["garmin"]
    checked = ", ".join(f'{r["from"]} to {r["to"]}' for r in coverage["checked_ranges"]) or "None"
    incomplete = ", ".join(f'{r["from"]} to {r["to"]}' for r in coverage["incomplete_ranges"]) or "None recorded"
    revisions = "".join(f'<li>Revision {r["revision"]} · {escape(LABELS[r["mode"]])} · {escape(r["selected_at"])}</li>'
                        for r in data["revisions"])
    attempts = "".join(f'<li><a href="{a["html_url"]}">Source import {a["id"]}</a> · {state(a["result"]["state"])}</li>'
                       for a in data["attempts"])
    content = f'''<p>Choose the sources for your next selected-source import. Saved runs stay in your history when you change this choice.</p>
<form action="/api/providers/config" method="post"><fieldset><legend>Running data sources</legend>
<label>Source mode <select name="mode">{options}</select></label>
<input type="hidden" name="expected_revision" value="{config["revision"]}">
<button type="submit">Save source mode</button></fieldset></form>
<p>Garmin imports and <a href="/imports">offline FIT uploads</a> are available. FIT uploads work in every mode.</p>
<p><strong>Strava analytics unavailable.</strong> Current Strava policy prevents this app's analytical imports and permanent activity storage.
Signing in does not enable them. <a href="https://www.strava.com/legal/api_policy">Read the API policy</a>.</p>
<form action="/api/providers/import" method="post"><fieldset><legend>Import selected sources</legend>
<p>Leave both dates empty for normal Garmin sync, or enter both to recheck a range.</p>
<label>From <input type="date" name="from"></label><label>To <input type="date" name="to"></label>
<button type="submit">Import selected sources</button></fieldset></form><p id="message" role="status"></p>
{attempt_view(attempt) if attempt else ""}
<h2>Available evidence</h2><p>Tier A: session summaries. Tier B: timestamped distance. Tier C: distance with HR, elevation and timer events.
Quality checks still control continuous efforts. Missing timer events mean unknown pause history. Recovery evidence is assessed separately.</p>
{activity_list}<p><a href="/imports">{len(data["unresolved_matches"])} source matches or dates need review</a>.</p>
<h2>Garmin date coverage</h2><p>Fully checked ranges: {escape(checked)}.</p>
<p>Incomplete scanned ranges: {escape(incomplete)}. Other dates remain unverified. FIT uploads do not verify provider date ranges.</p>
<details><summary>Previous choices and imports</summary><ul>{revisions}</ul><ul>{attempts}</ul></details>
<p><a href="/api/providers">Source status JSON</a></p>'''
    script = views.SUBMIT.replace("form.getAttribute('action') === '/api/imports/garmin'",
                                 "form.getAttribute('action') === '/api/providers/import'")
    script = script.replace("headers['Content-Type'] = 'application/json';", """if ('expected_revision' in fields) fields.expected_revision = Number(fields.expected_revision);
        headers['Content-Type'] = 'application/json';""")
    return views.page("Running data sources", content + script)
