"""HTML views of the same local records returned by the API."""
from datetime import date
from html import escape
import json
from urllib.parse import urlencode

from history import calendar_period, kilometres, UNAVAILABLE


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
nav {{display:flex; flex-wrap:wrap; gap:1.5rem; padding-bottom:.65rem; border-bottom:1px solid #bccbc2}}
h1, h2, h3 {{line-height:1.2}} h2 {{margin:1.75rem 0 .75rem}}
.totals, .detail-metrics {{display:grid; grid-template-columns:repeat(auto-fit,minmax(min(12rem,100%),1fr)); gap:.7rem 1rem}}
.totals {{padding:1rem; background:#eff5f0; border-top:2px solid #31654c}}
.totals dd {{font-size:1.35rem}} small {{display:block; font-size:.85rem; color:#46564b}}
.run-list {{padding:0; list-style:none}} .run-list li {{padding:.6rem 0; border-bottom:1px solid #d9e1db}}
.run-list strong {{display:inline-block; margin-right:.65rem}}
.table-wrap {{max-width:100%; overflow:auto}} table {{border-collapse:collapse}}
th, td {{padding:.35rem .65rem; text-align:left; border-bottom:1px solid #d9e1db}}
.recording-table {{min-width:42rem; width:100%}} .recording-table td {{vertical-align:top}}
.recording-table pre {{max-width:28rem; white-space:pre-wrap}}
:focus-visible {{outline:3px solid #1a704e; outline-offset:2px}}
</style></head><body><nav aria-label="Main"><a href="/history">History</a><a href="/workload">Workload</a><a href="/imports">Imports</a><a href="/efforts">Best efforts</a><a href="/providers">Sources</a><a href="/context">Context</a><a href="/workouts">Workout labels</a></nav><main><h1>{escape(title)}</h1>
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


def run_link(row):
    return f'<a href="/activities/{row["id"]}">{escape(row["name"] or ("Activity " + str(row["id"])))}</a>'


def run_list(rows):
    items = []
    for row in rows:
        ids = ", ".join(escape(gid) for gid in row["garmin_ids"])
        source = f"Garmin {ids}" if ids else "FIT"
        when = row["start_local"] or row["local_date"] or "Undated"
        items.append(f'<li><strong>{run_link(row)}</strong> {row["display"]["distance_m"]}'
                     f'<small>{escape(when)} · {source}</small></li>')
    return '<ul class="run-list">' + "".join(items) + '</ul>'


def history_error(message):
    return page("History", f'<p>{escape(message)}</p><p><a href="/history">Choose another period</a></p>')


def history_page(data):
    first, last = data["from"], data["to"]
    anchor = date.fromisoformat(first)
    is_week = calendar_period(anchor, "week") == (anchor, date.fromisoformat(last))
    selector = f"""<form action="/history" method="get"><fieldset><legend>Choose a period</legend>
<div><label for="history-period">Period</label><select id="history-period" name="period"><option value="month">Month</option>
<option value="week"{' selected' if is_week else ''}>Week (Monday–Sunday)</option></select></div>
<label>Date in period <input type="date" name="date" value="{first}" required></label>
<button type="submit">Show history</button></fieldset></form>"""
    rows = data["activities"]
    by_id = {r["id"]: r for r in rows}
    leaders = ", ".join(run_link(by_id[i]) for i in data["longest_run_ids"])
    longest = f'{kilometres(data["longest_distance_m"])}<small>{leaders}</small>' if leaders else UNAVAILABLE
    summary = f'<div><dt>Runs</dt><dd>{data["run_count"]}</dd></div>'
    for key, label in (("distance_m", "Distance"), ("timer_seconds", "Watch timer"),
                       ("elapsed_seconds", "Elapsed time"), ("moving_seconds", "Moving time")):
        item = data["totals"][key]
        missing = item["missing_count"]
        note = f'<small>{missing} missing</small>' if missing else ""
        subtotal = item["display"]
        summary += f'<div><dt>{"Known " + label.lower() if missing else label}</dt><dd>{subtotal}{note}</dd></div>'
    summary += f'<div><dt>Longest known run</dt><dd>{longest}</dd></div>'
    coverage = data["coverage"]
    coverage_html = "<p>Garmin coverage complete.</p>"
    if coverage["gaps"]:
        gaps = []
        for gap in coverage["gaps"]:
            query = escape(urlencode(gap))
            gaps.append(f'<li>{gap["from"]} to {gap["to"]} · <a href="/imports?{query}">Recheck these dates</a></li>')
        coverage_html = "<p>Garmin coverage incomplete. Unverified dates</p><ul>" + "".join(gaps) + "</ul>"
    activities = run_list(rows) if rows else "<p>No saved runs in this period.</p>"
    if coverage["complete"] and not rows:
        activities = "<p>No runs returned for this fully checked period.</p>"
    undated = ""
    if data["undated_activities"]:
        undated = ('<h2>Undated runs</h2><p>Outside period totals until a local date is confirmed on '
                   '<a href="/imports">Imports</a>.</p>' + run_list(data["undated_activities"]))
    weeks = ""
    if not is_week and data["weeks"]:
        items = []
        for week in data["weeks"]:
            distance = week["totals"]["distance_m"]
            missing = f' · known subtotal, {distance["missing_count"]} missing' if distance["missing_count"] else ""
            query = escape(urlencode({"from": week["from"], "to": week["to"]}))
            count = week["run_count"]
            items.append(f'<li><a href="/history?{query}">{week["from"]} to {week["to"]}</a>'
                         f' · {count} {"run" if count == 1 else "runs"} · {distance["display"]}{missing}</li>')
        weeks = "<h2>Weekly totals within this period</h2><ul>" + "".join(items) + "</ul>"
    query = escape(urlencode({"from": first, "to": last}))
    return page("History", f"""{selector}<h2>{first} to {last}</h2><dl class="totals">{summary}</dl>
{coverage_html}<h2>Contributing runs</h2>{activities}{weeks}{undated}<p><a href="/api/history?{query}">History JSON</a></p>""")


def activity(row):
    display = row["display"]
    values = []
    def metric(label, value, *keys):
        names = {"detail": "Garmin detail", "list": "Garmin summary", "fit": "FIT", "user": "Confirmed"}
        sources = dict.fromkeys(row["metric_sources"].get(key) for key in keys)
        origins = ", ".join(names[source] for source in sources if source in names)
        note = f"<small>{origins}</small>" if origins and value != UNAVAILABLE else ""
        values.append(f"<div><dt>{label}</dt><dd><span>{escape(value)}</span>{note}</dd></div>")

    confirmed = row["local_date"] and not row["start_local"]
    metric("Confirmed local date" if confirmed else "Recorded local start", row["start_local"] or row["local_date"] or "Undated",
           "local_date" if confirmed else "start_local")
    for label, key in (("Distance", "distance_m"), ("Watch timer", "timer_seconds"), ("Elapsed time", "elapsed_seconds"),
                       ("Moving time", "moving_seconds")):
        metric(label, display[key], key)
    for label, kind in (("Timer pace", "timer"), ("Moving pace", "moving")):
        metric(label, display[kind + "_pace_seconds_per_km"], kind + "_seconds", "distance_m")
    metric("Average heart rate", f'{row["average_hr_bpm"]:g} bpm' if row["average_hr_bpm"] is not None else UNAVAILABLE,
           "average_hr_bpm")
    metric("GPS", "Recorded" if row["gps_available"] else UNAVAILABLE, "gps_available")
    warnings = "".join(f"<p>{escape(warning)}</p>" for warning in row["warnings"])
    garmin_link = "".join(f'<p><a href="https://connect.garmin.com/modern/activity/{escape(gid)}" rel="noreferrer">'
                          f'Garmin Connect activity {escape(gid)}</a></p>' for gid in row["garmin_ids"])
    moving = any(lap["moving_seconds"] is not None for lap in row["laps"])
    laps = "".join(f'<tr><th scope="row">{lap["sequence"]}</th><td>' +
                   (f'{lap["distance_m"]:.2f}'.rstrip("0").rstrip(".") + ' m' if lap["distance_m"] is not None else "Not available") +
                   f'</td><td>{lap["display"]["timer_seconds"]}</td><td>{lap["display"]["elapsed_seconds"]}</td>' +
                   (f'<td>{lap["display"]["moving_seconds"]}</td>' if moving else "") + '</tr>'
                   for lap in row["laps"])
    lap_table = ('<div class="table-wrap"><table><caption>FIT laps in recorded order</caption><thead><tr>'
                 '<th>Lap</th><th>Distance</th><th>Watch timer</th><th>Elapsed time</th>' +
                 ('<th>Moving time</th>' if moving else '') + '</tr></thead><tbody>' + laps +
                 '</tbody></table></div>') if laps else '<p>Not available</p>'
    return page(row["name"] or "Activity detail", f"""
{garmin_link}<dl class="detail-metrics">{''.join(values)}</dl>{warnings}<h2>Recorded laps</h2>{lap_table}
<p><a href="/activities/{row["id"]}/recording">Inspect recorded samples and events</a></p>
<p><a href="/activities/{row["id"]}/canonical">Inspect canonical trace and quality</a></p>
<p><a href="/activities/{row["id"]}/source-history">Inspect source and identity history</a></p>
<p><a href="/activities/{row["id"]}/efforts">Inspect continuous efforts</a></p>
<p><a href="/activities/{row["id"]}/fueling">Record fueling practice and tolerance</a></p>
<p><a href="/activities/{row["id"]}/workout">Inspect workout labels</a></p>
<p><a href="/api/activities/{row["id"]}">Activity JSON</a></p>""")


def recording_page(row):
    def value(item, unit=""):
        return "Not provided" if item is None else escape(f"{item}{unit}")

    def raw(fields, title="Decoded fields"):
        return f'<details><summary>{title}</summary><pre style="white-space:pre-wrap">' + escape(
            json.dumps(fields, indent=2, ensure_ascii=False, sort_keys=True)) + '</pre></details>'

    def table(caption, headings, rows):
        return '<div class="table-wrap" tabindex="0" role="region" aria-label="' + caption + '"><table class="recording-table"><caption>' + caption + '</caption><thead><tr>' + ''.join(
            f'<th scope="col">{h}</th>' for h in headings) + '</tr></thead><tbody>' + ''.join(
            '<tr>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>' for cells in rows) + '</tbody></table></div>'

    aid = row["activity_id"]
    base = f'/activities/{aid}/recording'
    source = row["selected_fit_sha256"]
    samples = row["samples"]
    query = {"source": source, "limit": samples["limit"]} if source else {"limit": samples["limit"]}
    json_link = f'/api/activities/{aid}/recording?' + urlencode({**query, "offset": samples["offset"]})
    content = f'<p><a href="/activities/{aid}">Back to activity {aid}</a> · <a href="{escape(json_link)}">Recording JSON</a></p>'
    content += '<p>Inspect the original watch recording. All timestamps below are UTC; distances are cumulative metres. '
    content += 'On narrow screens, scroll tables sideways to see every column.</p>'
    content += '<h2>Original file</h2>'
    if row["recordings"]:
        options = ''.join(f'<option value="{r["fit_sha256"]}"' + (' selected' if r["fit_sha256"] == source else '') +
                          f'>{i + 1} · {r["fit_sha256"][:16]}…</option>' for i, r in enumerate(row["recordings"]))
        content += f'<form method="get" action="{base}"><fieldset><legend>Choose a recording</legend>' + (
            '<label>Original FIT<select name="source">' + options + '</select></label>' +
            f'<input type="hidden" name="limit" value="{samples["limit"]}"><button>Show recording</button></fieldset></form>')
        content += '<p>Linked files and older versions remain separate here. These values come from the selected file; '
        content += 'the activity summary may use a different source.</p>'
        content += f'<dl><dt>FIT SHA-256</dt><dd>{source}</dd></dl>'
        selected = next(r for r in row["recordings"] if r["fit_sha256"] == source)
        content += raw({**row["provenance"], "archive_sources": selected["sources"]}, "Source and decoder provenance")
    content += '<h2>Coverage and limitations</h2><ul>' + ''.join(f'<li>{escape(t)}</li>' for t in row["limitations"]) + '</ul>'
    if row["state"] != "AVAILABLE":
        content += '<p><a href="/imports">Open Imports</a> to restore an original file.</p>'
        return page("Recorded samples and events", content)
    header = row["header"]
    content += '<h2>Recorded session</h2><dl class="detail-metrics">' + ''.join(
        f'<div><dt>{title}</dt><dd>{value(header[key], unit)}</dd></div>' for title, key, unit in (
            ("Start (UTC)", "start_utc", ""), ("Derived end (UTC)", "end_utc", ""),
            ("Message written (UTC)", "message_timestamp_utc", ""), ("End basis", "end_basis", ""), ("Sport", "sport", ""),
            ("Distance", "distance_m", " m"), ("Elapsed time", "elapsed_seconds", " s"),
            ("Watch timer", "timer_seconds", " s"), ("Moving time", "moving_seconds", " s"))) + '</dl>'
    content += raw(header, "Session, device and recorded time fields")
    channels = row["coverage"]
    names = {"timestamp_utc": "Timestamp (UTC)", "distance_m": "Cumulative distance (m)",
             "heart_rate_bpm": "Heart rate in sample records (bpm)", "elevation_m": "Elevation (m)", "speed_m_per_s": "Speed (m/s)",
             "cadence_rpm": "Cadence (recorded rpm)", "temperature_c": "Temperature (°C)", "gps": "GPS coordinates"}
    statuses = {"RECORDED": "Recorded", "PARTIAL": "Partial", "NOT_PROVIDED": "Not provided"}
    content += table("Sample field coverage", ("Field", "Coverage", "Present / total", "Missing"), (
        (names[key], statuses[c["status"]], f'{c["present"]} / {c["total"]}', str(c["missing"]))
        for key, c in channels["samples"].items()))
    content += '<p>Counts include readable values only. Decoded fields retain the original SDK values, including '
    content += 'unusable timestamps. GPS uses FIT semicircles; cadence is shown in the file’s recorded rpm.</p>'
    content += '<h2>Separate heart-rate messages</h2>'
    if row["heart_rate_messages"]:
        content += f'<p>{channels["heart_rate_message_count"]} separate HR messages are supplied by this file. '
        content += 'These readings have their own timing fields and are not merged into the sample records above. '
        content += 'Expand Decoded fields for fractional timestamps and beat-event timing; absent base timestamps remain unknown.</p>'
        content += '<p>Base timestamp coverage: ' + statuses[channels["heart_rate_message_timestamp_utc"]["status"]]
        content += '; filtered BPM field coverage: ' + statuses[channels["heart_rate_message_bpm"]["status"]] + '.</p>'
        content += table("Separate HR messages in file order", ("HR index", "Base timestamp (UTC)", "Filtered BPM", "Source fields"), (
            (str(h["hr_index"]), value(h["timestamp_utc"]), value(h["filtered_bpm"]), raw(h["fields"]))
            for h in row["heart_rate_messages"]))
    else:
        content += '<p>No separate HR messages supplied. Any heart rate in sample records is shown in the coverage table above.</p>'
    content += '<h2>Timer and other events</h2><p>Timer-event coverage: <strong>' + statuses[channels["timer_events"]] + '</strong>. '
    content += f'{channels["timer_event_count"]} timer events; {channels["event_count"]} events in total.</p>'
    if row["events"]:
        content += table("Events in file order", ("Event index", "Timestamp (UTC)", "Event", "Type", "Source fields"), (
            (str(e["event_index"]), value(e["timestamp_utc"]), escape(e["event"].replace("_", " ")),
             escape(e["event_type"].replace("_", " ")), raw(e["fields"])) for e in row["events"]))
    else:
        content += '<p>No event messages supplied. Pause and resume history is unknown.</p>'
    content += '<h2>Lap markers</h2>'
    if row["laps"]:
        content += '<p>Start timestamp coverage: ' + statuses[channels["lap_start_utc"]["status"]] + '; derived end coverage (start + reported elapsed): '
        content += statuses[channels["lap_end_utc"]["status"]] + '. Lap indices start at zero and retain file order.</p>'
        content += table("Laps in file order", ("Lap index", "Start (UTC)", "Derived end (UTC)", "Message written (UTC)", "End basis", "Distance", "Elapsed", "Timer", "Source fields"), (
            (str(lap["lap_index"]), value(lap["start_utc"]), value(lap["end_utc"]), value(lap["message_timestamp_utc"]), value(lap["end_basis"]), value(lap["distance_m"], " m"),
             value(lap["elapsed_seconds"], " s"), value(lap["timer_seconds"], " s"), raw(lap["fields"])) for lap in row["laps"]))
    else:
        content += '<p>Lap markers not provided.</p>'
    content += '<h2 id="samples">Recorded samples</h2>'
    total, offset, limit = samples["total"], samples["offset"], samples["limit"]
    content += f'<p>{total} records in total. Indices start at zero and are stable within this original file.</p>'
    links = []
    if offset > 0:
        previous = min(max(0, offset - limit), max(0, ((total - 1) // limit) * limit))
        links.append('<a href="' + escape(base + '?' + urlencode({**query, "offset": previous})) + '#samples">Previous samples</a>')
    if offset + limit < total:
        links.append('<a href="' + escape(base + '?' + urlencode({**query, "offset": offset + limit})) + '#samples">Next samples</a>')
    pager = '<p>' + ' · '.join(links) + '</p>' if links else ''
    content += pager
    if samples["rows"]:
        content += table(f'Records {offset}–{offset + len(samples["rows"]) - 1}',
                         ("Record index", "Timestamp (UTC)", "Distance (m)", "Heart rate (bpm)", "Source fields"), (
            (str(s["record_index"]), value(s["timestamp_utc"]), value(s["distance_m"]), value(s["heart_rate_bpm"]), raw(s["fields"]))
            for s in samples["rows"]))
    elif total:
        content += '<p>No records on this page. Use Previous samples to return to the recording.</p>'
    else:
        content += '<p>No sample records supplied. This file provides summary data only.</p>'
    return page("Recorded samples and events", content + pager)


def canonical_page(row):
    aid = row["activity_id"]
    base = f"/activities/{aid}/canonical"

    def show(value):
        return escape(str(value)) if value is not None else "Not provided"

    def details(value, title="Evidence and calculations"):
        return '<details><summary>' + title + '</summary><pre style="white-space:pre-wrap">' + escape(
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)) + '</pre></details>'

    def table(caption, headers, rows):
        return f'<div class="table-wrap" tabindex="0" role="region" aria-label="{caption}"><table class="recording-table"><caption>{caption}</caption><thead><tr>' + ''.join(
            f'<th scope="col">{h}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
            '<tr>' + ''.join(f'<td>{value}</td>' for value in cells) + '</tr>' for cells in rows) + '</tbody></table></div>'

    content = f'<p><a href="/activities/{aid}">Back to activity {aid}</a></p>'
    content += '<p>Engineering quality checks preserve recorded UTC timestamps, cumulative metres and elapsed time. '
    content += 'Thresholds are provisional. Human comparison and scientific validation remain unverified. '
    content += f'<a href="/activities/{aid}/efforts">Inspect calculated continuous efforts separately.</a></p>'
    content += f'<button id="recompute" data-url="/api/activities/{aid}/canonical/recompute">Recompute offline with current QC</button><p id="result" role="status"></p>'
    content += """<script>
document.getElementById('recompute').addEventListener('click', async event => {
 const button = event.currentTarget; button.disabled = true;
 try {
   const response = await fetch(button.dataset.url, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
   const result = await response.json();
   if (!response.ok) throw new Error(result.detail || 'Recompute failed.');
   location.assign(location.pathname);
 } catch (error) { document.getElementById('result').textContent = error.message; button.disabled = false; }
});
</script>"""
    if row["state"] == "NOT_PROCESSED":
        return page("Canonical trace and quality", content + '<p>' + escape(row["message"]) + '</p>')
    if row.get("recompute_required"):
        content += '<p><strong>Recompute required:</strong> this saved version uses an older session/lap time interpretation. Recompute offline to update it; historical versions remain inspectable.</p>'
    query = {"version": row["version_id"]}
    if row["inspected_fit_sha256"]:
        query["source"] = row["inspected_fit_sha256"]
    content += f'<p>Canonical quality: <strong>{row["canonical_quality_state"]}</strong> · Inspected source quality: <strong>{row["source_qc_state"]}</strong> · Timer-event coverage: <strong>{row["qc"]["timer_event_coverage"]}</strong></p>'
    content += '<p>Missing events are not proof of uninterrupted movement. Recorded events can omit stops.</p>'
    if row["version_id"] != row["current_version_id"]:
        content += '<p><strong>Historical version</strong> · <a href="' + base + '">Open current version</a></p>'
    if row["archive_status_now"] != "VERIFIED":
        content += '<p>Original archive unavailable or checksum failed. Persisted evidence is historical; restore the original before downstream use.</p>'
    versions = ''.join(f'<option value="{v["id"]}"' + (' selected' if v["id"] == row["version_id"] else '') +
                       f'>{show(v["config_version"])} · {v["id"][:16]}… · activity {v["activity_id_at_creation"]}</option>' for v in row["versions"])
    content += f'<form method="get" action="{base}"><fieldset><legend>Saved versions</legend><label>Canonical version<select name="version">{versions}</select></label><button>Show version</button></fieldset></form>'
    if row["sources"]:
        options = ''.join(f'<option value="{s["fit_sha256"]}"' + (' selected' if s["fit_sha256"] == row["inspected_fit_sha256"] else '') +
                          f'>{s["fit_sha256"][:16]}…</option>' for s in row["sources"])
        content += f'<form method="get" action="{base}"><fieldset><legend>Source-specific trace</legend><input type="hidden" name="version" value="{row["version_id"]}"><label>Original FIT<select name="source">{options}</select></label><button>Show source</button></fieldset></form>'
    content += '<p>Canonical selected FIT: ' + show(row["selected_fit_sha256"]) + '</p>'
    if row["inspected_fit_sha256"] != row["selected_fit_sha256"]:
        content += '<p>This is an alternate owned source, not the selected canonical trace.</p>'
    content += '<h2>Activity summary</h2>' + details(row["summary"], "Summary values and sources")
    if row["summary_comparisons"]:
        content += '<p>The activity summary differs from the selected original. Review the recorded differences.</p>'
        content += details(row["summary_comparisons"], "Summary discrepancies")
    if not row["running_sport"]:
        content += '<p>This activity is not classified as running and is excluded from running-performance use.</p>'
    provenance = {k: row[k] for k in ("version_id", "canonical_activity_id", "activity_id_at_creation", "provider_ids",
        "merged_activity_ids", "raw_sources", "selected_source_version_id", "canonical_algorithm_version", "qc_algorithm_version",
        "qc_config_version", "qc_config", "selection_policy", "sources", "created_at", "selection_history")}
    content += details(provenance, "Canonical provenance and QC settings")
    content += '<h2>Quality flags</h2>'
    content += table("Reviewable quality flags", ("Condition", "Effect", "Record indices", "Details"), (
        (show(f["code"]), show(f["effect"]), show(f["sample_indices"]), details(f)) for f in row["qc"]["flags"]))
    content += '<h2>Timer-separated blocks</h2><p>Only confirmed timer boundaries split these ranges. Ambiguous stationary spans stay flagged; no stop is invented.</p>'
    content += details(row["qc"]["blocks"], "Block indices and boundary evidence")
    trace = row["trace"]
    if trace:
        content += '<h2>Selected source header</h2>' + details(trace["header"], "Recorded header and device-time evidence")
        content += details(row["parser"], "Parser version and settings")
        content += '<h2>Separate HR streams</h2><p>Separate HR messages are retained without invented alignment to samples.</p>'
        content += details(trace["heart_rate_messages"], "Separate HR messages")
        content += details(trace["events"], "Timer and other raw events")
        samples = trace["samples"]
        query.update(offset=samples["offset"], limit=samples["limit"])
        content += f'<h2 id="samples">Canonical samples</h2><p>{samples["total"]} original records; indices and values retain file order.</p>'
        content += table("Canonical records in file order", ("Index", "UTC timestamp", "Distance (m)", "Decoded values"), (
            (str(s["record_index"]), show(s["timestamp_utc"]), show(s["distance_m"]), details(s["fields"])) for s in samples["rows"]))
        links = []
        if samples["offset"]:
            previous = min(max(0, samples["offset"] - samples["limit"]), max(0, ((samples["total"] - 1) // samples["limit"]) * samples["limit"]))
            links.append('<a href="' + escape(base + '?' + urlencode({**query, "offset": previous})) + '#samples">Previous samples</a>')
        if samples["offset"] + samples["limit"] < samples["total"]:
            links.append('<a href="' + escape(base + '?' + urlencode({**query, "offset": samples["offset"] + samples["limit"]})) + '#samples">Next samples</a>')
        content += '<p>' + ' · '.join(links) + '</p>'
    elif row["message"]:
        content += '<p>' + escape(row["message"]) + '</p>'
    content += '<p><a href="' + escape(f'/api/activities/{aid}/canonical?' + urlencode(query)) + '">Canonical JSON</a></p>'
    return page("Canonical trace and quality", content)
