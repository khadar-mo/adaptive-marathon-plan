"""HTML views of the same local records returned by the API."""
from html import escape
from urllib.parse import urlencode


def page(title, content):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Running history</title>
<link rel="icon" href="data:,">
<style>
body {{font: 1rem/1.5 system-ui, sans-serif; max-width: 48rem; margin: 2rem auto; padding: 0 1rem;
       color: #18241e; overflow-wrap: anywhere}}
fieldset {{display: flex; flex-wrap: wrap; align-items: end; gap: 1rem; padding: 1rem; min-inline-size: 0}}
fieldset > label {{min-width: 0; max-width: 100%}}
label, input {{display: block}} input, select {{max-width: 100%; box-sizing: border-box}} button, input, select {{font: inherit; padding: .35rem}}
dt {{font-weight: bold}} dd {{margin: 0 0 .7rem}} li {{margin: .5rem 0}} a {{color: #17523d}}
</style></head><body><nav><a href="/imports">Imports</a></nav><main><h1>{escape(title)}</h1>
{content}</main></body></html>"""


SUBMIT = """
<script>
document.addEventListener('submit', async event => {
    const form = event.target;
    event.preventDefault();
    const button = event.submitter;
    const message = document.getElementById('message');
    const data = new FormData(form);
    let body = data;
    const headers = {};
    if (form.enctype !== 'multipart/form-data') {
        const fields = Object.fromEntries(data);
        if (form.getAttribute('action') === '/api/imports/garmin' && !fields.from && !fields.to) {
            delete fields.from;
            delete fields.to;
        }
        if (button.name === 'action') {
            fields.action = button.value;
            if (fields.action !== 'link') delete fields.activity_id;
            if (fields.activity_id) fields.activity_id = Number(fields.activity_id);
        }
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(fields);
    }
    button.disabled = true;
    message.textContent = 'Saving…';
    try {
        const response = await fetch(form.getAttribute('action'), {method: 'POST', headers, body});
        const result = await response.json();
        if (response.ok) {
            location.assign(result.html_url);
        } else if (response.status === 409 && result.html_url) {
            const link = document.createElement('a');
            link.href = result.html_url;
            link.textContent = 'Open the running import';
            message.replaceChildren('An import is already running. ', link);
        } else {
            message.textContent = typeof result.detail === 'string' ? result.detail : 'Check the form and try again.';
        }
    } catch {
        message.textContent = 'Could not reach the local app. Reload Imports to check the saved report.';
    } finally {
        button.disabled = false;
    }
});
</script>"""


def resolve_form(issue):
    if "kind" not in issue:
        return ""
    if issue["resolution"] == "excluded_sport":
        return "<p>Garmin now records this activity as another sport.</p>"
    if issue["resolution"] == "resolved_outside":
        return "<p>Garmin moved this activity outside the requested dates.</p>"
    if issue["resolution"]:
        return f'<p>Resolved · <a href="/activities/{issue["activity_id"]}">Open activity {issue["activity_id"]}</a></p>'
    action = f'/api/imports/{issue["import_id"]}/issues/{issue["id"]}/resolve'
    if issue["kind"] == "date":
        return f"""<form method="post" action="{action}"><label>Confirmed local date
        <input type="date" name="local_date" required></label>
        <button name="action" value="set_local_date">Save local date</button></form>"""
    options = "".join(f'<option value="{c["id"]}">Activity {c["id"]} · {escape(c["name"] or "Run")} · '
                      f'{escape(c["start_utc"] or c["local_date"] or "Undated")}</option>' for c in issue["candidates"])
    links = " · ".join(f'<a href="/activities/{c["id"]}">View activity {c["id"]}</a>' for c in issue["candidates"])
    return f"""<form method="post" action="{action}"><label>Possible activity
    <select name="activity_id">{options}</select></label> {links}
    <p><button name="action" value="link">Link to activity</button>
    <button name="action" value="keep_separate">Keep separate</button></p></form>"""


def period(row):
    if row["source"] == "fit":
        return "FIT import"
    return f'{escape(row["requested_from"])} to {escape(row["requested_to"])}'


def imports(rows, first, last, pending):
    attempts = "".join(f'<li><a href="{row["html_url"]}">Import {row["id"]}</a> · {period(row)}'
                       f' · {escape(row["state"])} · {escape(row["started_at"])}</li>' for row in rows)
    history = f"<ul>{attempts}</ul>" if attempts else "<p>No saved imports.</p>"
    reviews = "".join(f'<li>{escape(i["item"])} · <a href="/imports/{i["import_id"]}">Import {i["import_id"]}</a>'
                      f'{resolve_form(i)}</li>' for i in pending)
    return page("Imports", f"""
<form method="post" action="/api/imports/garmin"><fieldset><legend>Garmin date range</legend>
<p>Leave both dates empty for normal sync, or enter both to recheck a range.</p>
<label>From <input type="date" name="from" value="{escape(first)}"></label>
<label>To <input type="date" name="to" value="{escape(last)}"></label>
<button type="submit">Sync Garmin</button></fieldset></form>
<form method="post" action="/api/imports/fit" enctype="multipart/form-data"><fieldset><legend>Offline FIT upload</legend>
<label>FIT files <input type="file" name="files" accept=".fit" required multiple></label>
<button type="submit">Upload FIT files</button></fieldset></form>
<p id="message" role="status"></p>{('<h2>Needs review</h2><ul>' + reviews + '</ul>') if reviews else ''}
<h2>Saved reports</h2>{history}{SUBMIT}""")


def scan_summary(scan):
    if scan["mode"] == "legacy":
        return "<p>Coverage unchecked. This earlier importer did not record pagination or original request counts.</p>"
    labels = {"checked": "Fully checked", "checked_empty": "Fully checked · No runs returned",
              "unchecked": "Coverage incomplete · Unchecked", "missing_activity": "Coverage incomplete · Missing activity"}
    checked = ", ".join(f'{r["from"]} to {r["to"]}' for r in scan["checked_ranges"]) or "None"
    saved_dates = (f'<p>Saved runs span {escape(scan["earliest_saved"])} to {escape(scan["latest_saved"])}.</p>'
                   if scan["earliest_saved"] else "")
    ids = ", ".join(escape(gid) for gid in scan["saved_garmin_ids"])
    saved_ids = f"<p><strong>Saved Garmin IDs</strong> {ids}</p>" if ids else ""
    retry = ""
    if scan["mode"] == "retry":
        query = escape(urlencode({"from": scan["from"], "to": scan["to"]}))
        retry = f'<p><a href="/imports?{query}">Recheck {escape(scan["from"])} to {escape(scan["to"])}</a></p>'
    return f"""<h3>{labels[scan["coverage"]]} · {escape(scan["from"])} to {escape(scan["to"])}</h3>
<p>Search bounds {escape(scan["search_from"])} to {escape(scan["search_to"])} · {scan["pages_read"]} pages read.
Fully checked dates: {checked}.</p>
<p>{scan["unique_ids_found"]} unique run IDs found · {scan["excluded"]} excluded ·
{len(scan["missing_activity_ids"])} missing activities · {scan["unidentified_records"]} unidentified records.</p>
<p>Original files: {scan["originals_available"]} available · {scan["originals_requested"]} requested ·
{len(scan["missing_original_ids"])} missing.</p>{saved_ids}{saved_dates}{retry}"""


def report(row):
    state = row["state"]
    activities = "".join(f'<li><a href="/activities/{a["id"]}">' +
                         (f'Garmin activity {escape(a["garmin_id"])}' if a["garmin_id"] else f'Activity {a["id"]}') +
                         f'</a> · {escape(a["outcome"])}</li>' for a in row["activities"])
    issues = "".join(f'<li><strong>{escape(i["step"])}</strong> · {escape(i["item"])} · {escape(i["message"])}'
                     f'{resolve_form(i)}</li>' for i in row["issues"])
    files = "".join(f'<li>{escape(f["filename"])}' +
                    (f' · <a href="/activities/{f["activity_id"]}">Activity {f["activity_id"]}</a>' if f["activity_id"] else '') +
                    '</li>' for f in row["files"])
    progress = ""
    if state == "running":
        progress = f'<p><a href="{row["html_url"]}">Refresh report</a></p><script>setTimeout(() => location.reload(), 2000);</script>'
    elif state == "interrupted":
        progress = "<p>The app stopped before this import finished.</p>"
    saved = f"<ul>{activities}</ul>" if activities else "<p>No activities saved in this attempt.</p>"
    retry = '<a href="/imports">Upload FIT files</a>'
    scans = ""
    if row["source"] == "garmin":
        query = escape(urlencode({"from": row["requested_from"], "to": row["requested_to"]}))
        retry = f'<a href="/imports?{query}">Recheck these dates</a>'
        scans = "<h2>Coverage</h2>" + "".join(scan_summary(s) for s in row["scans"])
    counts = row["counts"]
    return page(f'Import {row["id"]}', f"""
<p><strong>{escape(state.capitalize())}</strong> · {period(row)}</p>
<p>Started {escape(row["started_at"])}</p>{progress}{scans}
<h2>Saved activities</h2><p>{counts["inserted"]} inserted · {counts["updated"]} updated ·
{counts["unchanged"]} unchanged.</p>{saved}
{('<h2>Uploaded files</h2><ul>' + files + '</ul>') if files else ''}
{('<h2>Import issues</h2><ul>' + issues + '</ul>') if issues else ''}
<p id="message" role="status"></p><p><a href="{row["report_url"]}">Report JSON</a></p><p>{retry}</p>{SUBMIT}""")


def duration(seconds):
    if seconds is None:
        return "Not available"
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def activity(row):
    measurements = [
        ("Confirmed local date" if row["local_date"] and not row["start_local"] else "Recorded local start",
         row["start_local"] or row["local_date"] or "Undated"),
        ("Distance", f'{row["distance_m"] / 1000:.2f} km' if row["distance_m"] is not None else "Not available"),
        ("Watch timer", duration(row["timer_seconds"])), ("Elapsed time", duration(row["elapsed_seconds"])),
        ("Moving time", duration(row["moving_seconds"])),
    ]
    values = "".join(f"<dt>{label}</dt><dd>{escape(value)}</dd>" for label, value in measurements)
    warnings = "".join(f"<p>{escape(warning)}</p>" for warning in row["warnings"])
    garmin_link = (f'<p><a href="{escape(row["garmin_url"])}" rel="noreferrer">Garmin Connect activity '
                  f'{escape(row["garmin_id"])}</a></p>' if row["garmin_url"] else "")
    laps = "".join(f'<tr><th scope="row">{lap["sequence"]}</th><td>' +
                   (f'{lap["distance_m"]:.2f}'.rstrip("0").rstrip(".") + ' m' if lap["distance_m"] is not None else "Not available") +
                   f'</td><td>{duration(lap["timer_seconds"])}</td><td>{duration(lap["elapsed_seconds"])}</td></tr>'
                   for lap in row["laps"])
    lap_table = ('<h2>Recorded laps</h2><table><thead><tr><th>Lap</th><th>Distance</th><th>Watch timer</th>'
                 '<th>Elapsed time</th></tr></thead><tbody>' + laps + '</tbody></table>') if laps else '<p>No recorded laps available.</p>'
    return page(row["name"] or "Activity detail", f"""
{garmin_link}<dl>{values}</dl>{warnings}{lap_table}<p><a href="/api/activities/{row["id"]}">Activity JSON</a></p>""")
