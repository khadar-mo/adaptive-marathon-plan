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
fieldset {{display: flex; flex-wrap: wrap; align-items: end; gap: 1rem; padding: 1rem}}
label, input {{display: block}} button, input {{font: inherit; padding: .35rem}}
dt {{font-weight: bold}} dd {{margin: 0 0 .7rem}} li {{margin: .5rem 0}} a {{color: #17523d}}
</style></head><body><nav><a href="/imports">Imports</a></nav><main><h1>{escape(title)}</h1>
{content}</main></body></html>"""


SUBMIT = """
<script>
const form = document.querySelector('form');
const message = document.getElementById('message');
form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    message.textContent = 'Starting import…';
    try {
        const dates = Object.fromEntries(new FormData(form));
        if (!dates.from && !dates.to) {
            delete dates.from;
            delete dates.to;
        }
        const response = await fetch('/api/imports/garmin', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(dates)
        });
        const result = await response.json();
        if (response.status === 202) {
            location.assign(result.html_url);
        } else if (response.status === 409) {
            const link = document.createElement('a');
            link.href = result.html_url;
            link.textContent = 'Open the running import';
            message.replaceChildren('An import is already running. ', link);
        } else {
            message.textContent = response.status === 422
                ? 'Enter two valid dates with From on or before To.' : 'Could not start the import. Check local storage and retry.';
        }
    } catch {
        message.textContent = 'Could not reach the local app. Reload Imports to check if the request was saved.';
    } finally {
        button.disabled = false;
    }
});
</script>"""


def period(row):
    if row.get("source") == "fit":
        return "FIT import"
    return f'{escape(row["requested_from"])} to {escape(row["requested_to"])}'


def imports(rows, first, last):
    attempts = "".join(f'<li><a href="{row["html_url"]}">Import {row["id"]}</a> · {period(row)}'
                       f' · {escape(row["state"])} · {escape(row["started_at"])}</li>'
                       for row in rows)
    history = f"<ul>{attempts}</ul>" if attempts else "<p>No saved imports.</p>"
    return page("Imports", f"""
<form method="post" action="/api/imports/garmin"><fieldset><legend>Garmin date range</legend>
<p>Leave both dates empty for normal sync, or enter both to recheck a range.</p>
<label>From <input type="date" name="from" value="{escape(first)}"></label>
<label>To <input type="date" name="to" value="{escape(last)}"></label>
<button type="submit">Sync Garmin</button></fieldset></form>
<p id="message" role="status"></p><h2>Saved reports</h2>{history}{SUBMIT}""")


def scan_summary(scan):
    if scan["mode"] == "legacy":
        return "<p>Coverage unchecked. This earlier importer did not record pagination or original request counts.</p>"
    labels = {"checked": "Fully checked", "checked_empty": "Fully checked · No runs returned",
              "unchecked": "Coverage incomplete · Unchecked", "missing_activity": "Coverage incomplete · Missing activity"}
    checked = ", ".join(f'{r["from"]} to {r["to"]}' for r in scan["checked_ranges"]) or "None"
    saved_dates = (f'<p>Saved runs span {escape(scan["earliest_saved"])} to {escape(scan["latest_saved"])}.</p>'
                   if scan["earliest_saved"] else "")
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
{len(scan["missing_original_ids"])} missing.</p>{saved_dates}{retry}"""


def report(row):
    state = row["state"]
    first, last = row["requested_from"], row["requested_to"]
    activities = "".join(f'<li><a href="/activities/{a["id"]}">'
                         f'{("Garmin activity " + escape(a["garmin_id"])) if a["garmin_id"] else ("Activity " + str(a["id"]))}</a>'
                         f' · {escape(a["outcome"])}</li>' for a in row["activities"])
    issues = "".join(f'<li><strong>{escape(i["step"])}</strong> · {escape(i["item"])} · {escape(i["message"])}</li>'
                     for i in row["issues"])
    progress = ""
    if state == "running":
        progress = f'<p><a href="{row["html_url"]}">Refresh report</a></p><script>setTimeout(() => location.reload(), 2000);</script>'
    elif state == "interrupted":
        progress = "<p>The app stopped before this import finished.</p>"
    saved = f"<ul>{activities}</ul>" if activities else "<p>No activities saved in this attempt.</p>"
    retry = scans = ""
    if row.get("source") != "fit":
        query = escape(urlencode({"from": first, "to": last}))
        retry = f'<p><a href="/imports?{query}">Recheck these dates</a></p>'
        scans = "<h2>Coverage</h2>" + "".join(scan_summary(s) for s in row["scans"])
    counts = row["counts"]
    return page(f'Import {row["id"]}', f"""
<p><strong>{escape(state.capitalize())}</strong> · {period(row)}</p>
<p>Started {escape(row["started_at"])}</p>{progress}{scans}
<h2>Saved activities</h2><p>{counts["inserted"]} inserted · {counts["updated"]} updated ·
{counts["unchanged"]} unchanged.</p>{saved}
{('<h2>Import issues</h2><ul>' + issues + '</ul>') if issues else ''}
<p><a href="{row["report_url"]}">Report JSON</a></p>
{retry}""")


def duration(seconds):
    if seconds is None:
        return "Not available"
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def activity(row):
    measurements = [
        ("Recorded local start", row["start_local"]),
        ("Distance", f'{row["distance_m"] / 1000:.2f} km' if row["distance_m"] is not None else "Not available"),
        ("Watch timer", duration(row["timer_seconds"])), ("Elapsed time", duration(row["elapsed_seconds"])),
        ("Moving time", duration(row["moving_seconds"])),
    ]
    values = "".join(f"<dt>{label}</dt><dd>{escape(value)}</dd>" for label, value in measurements)
    warnings = "".join(f"<p>{escape(warning)}</p>" for warning in row["warnings"])
    return page(row["name"] or "Activity detail", f"""
<p><a href="{escape(row["garmin_url"])}" rel="noreferrer">Garmin Connect activity {escape(row["garmin_id"])}</a></p>
<dl>{values}</dl>{warnings}<p><a href="/api/activities/{row["id"]}">Activity JSON</a></p>""")
