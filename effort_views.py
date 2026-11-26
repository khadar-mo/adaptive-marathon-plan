"""Human-readable G2 results and exact source-bracket verification."""
from html import escape
import json
from urllib.parse import urlencode

import views


def show(value):
    return escape(str(value)) if value is not None else "Not provided"


def details(value, title):
    return '<details><summary>' + escape(title) + '</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + escape(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)) + '</pre></details>'


def table(caption, headers, rows):
    return f'<div class="table-wrap" tabindex="0" role="region" aria-label="{caption}"><table class="recording-table"><caption>{caption}</caption><thead><tr>' + ''.join(
        f'<th scope="col">{h}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{v}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


SCRIPT = """<script>
document.querySelectorAll('form[data-json]').forEach(form => form.addEventListener('submit', async event => {
 event.preventDefault(); const button = event.submitter; button.disabled = true;
 const status = document.getElementById('effort-status'); status.textContent = 'Saving…';
 try {
 const body = Object.fromEntries(new FormData(form));
 for (const key of ['start_distance_m','distance_m']) if (key in body) body[key] = Number(body[key]);
 if ('tags' in body) body.tags = body.tags.split(',').map(value => value.trim()).filter(Boolean);
 if ('maximal_confirmed' in body) {
  const declaration=body.maximal_confirmed;
  body.maximal_confirmed = declaration === 'yes';
  if(body.protocol===''&&form.dataset.protocolNull==='yes')body.protocol=null;
  body.qualification={maximality:declaration==='yes'?'CONFIRMED':declaration==='known_no'?'NOT_MAXIMAL':'UNKNOWN'};
  for(const key of ['rest_state','control_state','race_distance_verification','race_time_verification']){body.qualification[key]=body[key]||'UNKNOWN';delete body[key];}
  body.qualification.evidence_refs=JSON.parse(body.evidence_refs||'[]');
  if(!Array.isArray(body.qualification.evidence_refs))throw new Error('Evidence references must be a JSON array.');
  delete body.evidence_refs;
  if(body.evidence_reference||body.evidence_method)body.qualification.evidence_refs.push({kind:body.evidence_kind,reference:body.evidence_reference,method:body.evidence_method});
  for(const key of ['evidence_reference','evidence_method','evidence_kind'])delete body[key];
 }
 if (button.name) body[button.name] = button.value;
  const response = await fetch(form.getAttribute('action'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Check the values and try again.');
  if (result.html_url) location.assign(result.html_url); else location.reload();
 } catch(error) {status.textContent = error.message; button.disabled = false;}
}));
document.querySelectorAll('[data-clear-evidence]').forEach(button=>button.addEventListener('click',()=>{document.getElementById(button.dataset.clearEvidence).value='[]';}));
</script>"""
NOTICE = ('<p>Observed continuous performances from recorded elapsed time. Current capability is not estimated. '
          'Engineering QC is provisional; human comparison and scientific validation remain unverified. '
          'No maximal effort is inferred.</p>')


def result_text(c):
    if not c:
        return "Not available"
    return f'{c["elapsed_seconds"]:.6f} s · {c["pace_seconds_per_km"]:.6f} s/km · {show(c.get("effective_quality_state", c["computed_quality_state"]))}'


def link(c, aid, version):
    if c is None:
        return "Not available"
    return f'<a href="/activities/{aid}/efforts/{version}/{c["id"]}">{result_text(c)}</a>'


def inspect_form(c):
    if c is None:
        return "Not available"
    aid = c["activity_id"]
    if not c.get("inspection_required"):
        return link(c, aid, c["version_id"])
    # Explicit POST registers any window-constrained interval before review;
    # read-only global selection never writes an inspection into history.
    inputs = {"version": c["version_id"], "source": c["source_sha256"],
              "start_distance_m": c["start_distance_m"], "distance_m": c["distance_m"]}
    if "distance_coordinate_m" in c["start"]:
        inputs["start_coordinate_m"] = c["start"]["distance_coordinate_m"]
    return result_text(c) + f'<form data-json action="/api/activities/{aid}/efforts/inspect" method="post">' + ''.join(
        f'<input type="hidden" name="{k}" value="{show(v)}">' for k, v in inputs.items()) + f'<button>Inspect activity {aid}</button></form>'


def bests(row):
    content = NOTICE + '<form method="get"><fieldset><legend>Observed performance window</legend>'
    content += f'<label>Cutoff UTC<input name="cutoff" value="{show(row["cutoff_utc"])}" required></label>'
    content += f'<label>Recent days<input name="days" type="number" min="1" max="36500" value="{row["requested_recent_days"]}"></label><button>Show best efforts</button></fieldset></form>'
    content += '<p>Recent windows include segment ends on both boundaries and require the complete activity to have ended by the cutoff. '
    content += 'The 112-day default and 56/84/168-day comparisons are provisional. All-time PBs are retained when recent evidence is missing.</p>'
    content += '<p>This is an event-time reconstruction using current selected source versions, not a frozen prospective prediction.</p>'
    if any(e["reason"] == "SOURCE_NORMALIZATION_REBUILD_REQUIRED" for e in row["excluded"]):
        content += '<p><strong>Recompute required:</strong> saved evidence uses an older session/lap time interpretation. Recompute saved history offline to update it; previous versions remain inspectable.</p>'
    content += table("Observed best efforts", ("Distance (m)", "All time", "Whole activity", "Internal segment", "Recent windows", "Flagged evidence"), (
        (show(r["distance_m"]), f'<p>{show(r["state"])}</p>' + inspect_form(r["all_time"]), inspect_form(r["best_whole_activity"]), inspect_form(r["best_internal_segment"]),
         ''.join(f'<p>{recent["days"]} days · {show(recent["state"])}</p>' + inspect_form(recent["best"]) for recent in r["recent"]),
         inspect_form(r["flagged_candidate"])) for r in row["distances"]))
    if not row["distances"]:
        content += '<p>No extracted evidence. Import an original FIT or run explicit offline recompute for saved history.</p>'
    content += '<form data-json method="post" action="/api/efforts/recompute"><button>Recompute saved history offline</button></form>'
    content += details(row["excluded"], "Excluded activities and unresolved selection limits")
    content += details({k: row[k] for k in ("selection_id", "inputs", "time_basis", "eligibility") if k in row}, "Selection provenance")
    content += '<p><a href="' + escape('/api/efforts?' + urlencode({"cutoff": row["cutoff_utc"], "days": row["requested_recent_days"]})) + '">Best efforts JSON</a></p>'
    return views.page("Best continuous efforts", content + '<p id="effort-status" role="status"></p>' + SCRIPT)


def activity(row):
    aid = row["activity_id"]
    content = f'<p><a href="/activities/{aid}">Back to activity {aid}</a> · <a href="/efforts">All-time and recent bests</a></p>' + NOTICE
    content += f'<form data-json method="post" action="/api/activities/{aid}/efforts/recompute"><button>Recompute efforts offline</button></form>'
    if row["state"] == "NOT_PROCESSED":
        return views.page("Activity continuous efforts", content + '<p>NOT_PROCESSED: explicit offline recompute is required.</p><p id="effort-status" role="status"></p>' + SCRIPT)
    vid, source = row["version_id"], row["inspected_source"]
    if row.get("recompute_required"):
        content += '<p><strong>Recompute required:</strong> this saved version uses an older session/lap time interpretation. Its values remain historical evidence and are excluded from current bests.</p>'
    if vid != row["current_version_id"]:
        content += '<p><strong>Historical extraction version</strong></p>'
    content += f'<form method="get"><fieldset><legend>Saved extraction</legend><label>Extraction version<select name="version">' + ''.join(
        f'<option value="{v["id"]}"' + (' selected' if v["id"] == vid else '') + f'>{show(v["config_version"])} · {v["id"][:16]}…</option>' for v in row["versions"]) + '</select></label><button>Show version</button></fieldset></form>'
    if source:
        content += f'<form method="get"><input type="hidden" name="version" value="{vid}"><label>Original source<select name="source">' + ''.join(
            f'<option value="{s["fit_sha256"]}"' + (' selected' if s["fit_sha256"] == source["fit_sha256"] else '') + f'>{s["fit_sha256"][:16]}…</option>' for s in row["sources"]) + '</select></label><button>Show source</button></form>'
        if source["fit_sha256"] != row["selected_fit_sha256"]:
            content += '<p>Alternate source inspection; global bests use the canonical selected source.</p>'
        content += f'<p>Archive: {show(row["archive_status_now"])}. Original record count: {source.get("original_sample_count", 0)}.</p>'
        content += table("Activity target distances", ("Distance (m)", "Status", "Best continuous interval", "Whole activity", "Internal segment", "Flagged candidate"), (
            (show(r["distance_m"]), show(r["state"]), link(r["best"], aid, vid), link(r["best_whole_activity"], aid, vid),
             link(r["best_internal_segment"], aid, vid), link(r["flagged_candidate"], aid, vid)) for r in source["result"]["distances"]))
        content += f'<form data-json method="post" action="/api/activities/{aid}/efforts/inspect"><fieldset><legend>Inspect a selected interval</legend>'
        content += f'<input type="hidden" name="version" value="{vid}"><input type="hidden" name="source" value="{source["fit_sha256"]}">'
        content += '<label>Start cumulative metres<input name="start_distance_m" type="number" min="0" step="any" required></label><label>Target metres<input name="distance_m" type="number" min="0.000001" step="any" required></label><button>Inspect interval</button></fieldset></form>'
        content += details(source.get("qc"), "Original source quality and timer blocks")
    content += details({k: v for k, v in row.items() if k not in {"sources", "inspected_source"}}, "Extraction provenance and settings")
    content += '<p><a href="' + escape(f'/api/activities/{aid}/efforts?' + urlencode({"version": vid, **({"source": source["fit_sha256"]} if source else {})})) + '">Efforts JSON</a></p>'
    return views.page("Activity continuous efforts", content + '<p id="effort-status" role="status"></p>' + SCRIPT)


def inspection(row):
    c, aid, vid = row["candidate"], row["activity_id"], row["version_id"]
    base = f'/activities/{aid}/efforts/{vid}/{c["id"]}'
    selection = row.get('context_selection', {})
    selection_query = {k:selection[k] for k in ('as_of','knowledge_as_of') if selection.get(k)}
    def query(offset, limit):
        return escape(urlencode({'offset':offset,'limit':limit,**selection_query}))
    content = f'<p><a href="/activities/{aid}/efforts?version={vid}">Back to activity efforts</a> · <a href="/activities/{aid}">Original activity {aid}</a> · <a href="/activities/{aid}/workout">Workout labels</a></p>' + NOTICE
    if row.get("recompute_required"):
        content += '<p><strong>Recompute required:</strong> this saved candidate uses an older session/lap time interpretation. Its original values remain inspectable and are excluded from current bests.</p>'
    content += f'<p><strong>{show(c["distance_m"])} m in {c["elapsed_seconds"]:.6f} s</strong> · {c["pace_seconds_per_km"]:.6f} s/km · {c["mean_speed_mps"]:.9f} m/s</p>'
    content += f'<p>Computed QC: <strong>{show(c["computed_quality_state"])}</strong>. Effective QC: <strong>{show(c["effective_quality_state"])}</strong>. Reviewer verdict: {show(c["review_verdict"])}.</p>'
    content += f'<p>{show(c["start_utc"])} → {show(c["end_utc"])} UTC. Cumulative distance: {show(c["start_distance_m"])} → {show(c["end_distance_m"])} m.</p>'
    content += f'<p>Kind: {show(c["kind"])}. Provenance: {show(c["provenance"]["category"])}. Confirmed maximal: {"Explicit user attestation; protocol below" if c["provenance"]["verified_maximal"] else "NO"}. Timer coverage: {show(c["timer_event_coverage"])}.</p>'
    content += '<p>Extracted elapsed is end crossing minus start crossing. The separately reported device-header elapsed below is not substituted for sample precision.</p>'
    if c.get("selection", {}).get("basis") == "FLAT_OPEN_CELL_MIDPOINT":
        content += '<p>This interval is the deterministic midpoint of an open range with constant elapsed time. It attains that elapsed time; the eligible open range has no earliest start.</p>'
        content += details(c["selection"], "Attained representative selection")
    content += table("Exact endpoint brackets", ("Endpoint", "Raw indices", "Timestamp and distance pairs", "Fraction", "Crossing UTC", "Bracket gap"), (
        (label, show(c[endpoint]["sample_indices"]), '<br>'.join(show(s["timestamp_utc"]) + ' · ' + show(s["distance_m"]) + ' m' for s in c[endpoint]["samples"]),
         show(c[endpoint]["fraction"]), show(c[endpoint]["timestamp_utc"]), show(c[endpoint]["bracket_gap_s"])) for label, endpoint in (("Start / last departure", "start"), ("End / first arrival", "end"))))
    content += details(c["start"], "Start raw records and interpolation") + details(c["end"], "End raw records and interpolation")
    content += details(c["audit"], "Pause, plateau and gap audit") + details(c["flags"], "Computed quality flags")
    content += details(row["events"], "Original timer and other events")
    samples = row["samples"]
    content += table("Original interval records", ("Raw index", "UTC timestamp", "Cumulative metres", "Original fields"), (
        (show(s["record_index"]), show(s["timestamp_utc"]), show(s["distance_m"]), details(s["fields"], "Decoded fields")) for s in samples["rows"]))
    content += f'<p>{samples["total"]} excerpt records including adjacent context; offset {samples["offset"]}.</p><p>'
    if samples["offset"]:
        content += f'<a href="{base}?{query(max(0, samples["offset"] - samples["limit"]),samples["limit"])}">Previous records</a> '
    if samples["offset"] + samples["limit"] < samples["total"]:
        content += f'<a href="{base}?{query(samples["offset"] + samples["limit"],samples["limit"])}">Next records</a>'
    content += '</p>'
    content += '<h2>Reviewer audit</h2><p>Acceptance records your review without clearing computed QC. Rejection excludes this exact candidate from authoritative selection. Annotation adds context. None establishes maximality.</p>'
    content += f'<form data-json method="post" action="/api{base}/review"><fieldset><legend>Record a verdict</legend><label>Reviewer<input name="actor" required maxlength="120"></label><label>Review note<textarea name="note" required maxlength="4000"></textarea></label>'
    content += '<button name="action" value="accept">Accept</button><button name="action" value="reject">Reject</button><button name="action" value="annotate">Annotate</button></fieldset></form>'
    content += details(c["reviews"], "Review history with actor and timestamp")
    content += '<h2>Recorded effort context</h2><p>User context is separate from source measurements, computed QC and the review verdict. A race label alone does not establish maximality.</p>'
    content += '<form method="get"><fieldset><legend>Inspect historical declarations</legend><label>Context event cutoff UTC<input name="as_of" value="' + escape(selection.get('as_of') or '') + '"></label><label>Context knowledge cutoff UTC<input name="knowledge_as_of" value="' + escape(selection.get('knowledge_as_of') or '') + '"></label><button>Show context at cutoff</button></fieldset></form>'
    content += '<p>These cutoffs filter declarations and reviewer records only. Source/extraction availability is separate; this view is not a frozen prediction.</p>'
    q = c['provenance'].get('qualification', {})
    content += f'<p id="effort-qualification">Declared category: {show(c["provenance"]["category"])}. Test evidence: <strong>{show(q.get("test_evidence_status", "UNVERIFIED"))}</strong>. Race evidence: <strong>{show(q.get("race_evidence_status", "UNVERIFIED"))}</strong>. Attestations/documents are not independent scientific validation.</p>'
    content += details(c['provenance'], 'Workout classification and effort qualification')
    selected_context = c['context_history'][-1] if c['context_history'] else {}
    protocol_null = 'yes' if selected_context.get('protocol') is None else 'no'
    content += f'<form data-json data-protocol-null="{protocol_null}" method="post" action="/api{base}/context"><fieldset><legend>Record context or correction</legend><label>Effort category<select name="category">'
    content += ''.join(f'<option' + (' selected' if category==c['provenance']['category'] else '') + f'>{category}</option>' for category in ('EASY_OR_UNKNOWN', 'TRAINING_FAST', 'FATIGUED_BLOCK', 'RACE', 'VERIFIED_TEST')) + '</select></label>'
    content += '<label>Tags, separated by commas<input name="tags" value="' + escape(', '.join(c['provenance'].get('tags',[]))) + '"></label><label>Recorded by<input name="actor" required maxlength="120" value="' + escape(selected_context.get('actor','')) + '"></label><label>Context note<textarea name="note" required maxlength="4000">' + escape(selected_context.get('note','')) + '</textarea></label>'
    content += '<label>Benchmark protocol<textarea name="protocol" maxlength="4000">' + escape(selected_context.get('protocol') or '') + '</textarea></label><label>Explicit maximal confirmation<select name="maximal_confirmed">'
    maximal = {'CONFIRMED':'yes','NOT_MAXIMAL':'known_no'}.get(q.get('maximality'),'no')
    content += ''.join(f'<option value="{value}"' + (' selected' if value==maximal else '') + f'>{label}</option>' for value,label in (('no','Unknown / unconfirmed'),('yes','Yes, explicitly confirmed'),('known_no','Explicitly not maximal'))) + '</select></label>'
    for name, label, options in (('rest_state','Rest or fatigue context',('UNKNOWN','RESTED','FATIGUED')),('control_state','Controlled test context',('UNKNOWN','CONTROLLED','UNCONTROLLED')),('race_distance_verification','Race distance evidence',('UNKNOWN','ATTESTED','DOCUMENTED')),('race_time_verification','Race elapsed-time evidence',('UNKNOWN','ATTESTED','DOCUMENTED'))):
        content += f'<label>{label}<select name="{name}">' + ''.join(f'<option' + (' selected' if v==q.get(name,'UNKNOWN') else '') + f'>{v}</option>' for v in options) + '</select></label>'
    content += '<label>Saved effort evidence references (JSON)<textarea id="effort-evidence-refs" name="evidence_refs" rows="8">' + escape(json.dumps(q.get('evidence_refs',[]),indent=2)) + '</textarea></label>'
    content += '<p>All source, document and attestation references are retained here. Edit this list to change individual references; new references below are appended.</p><button type="button" data-clear-evidence="effort-evidence-refs">Remove all effort evidence references</button>'
    content += '<fieldset><legend>Add an evidence reference</legend><label>Evidence kind<select name="evidence_kind"><option value="athlete_attestation">Athlete attestation</option><option value="document">Recorded document</option></select></label><label>Evidence reference<textarea name="evidence_reference" maxlength="2000"></textarea></label><label>Verification method<textarea name="evidence_method" maxlength="1000"></textarea></label></fieldset>'
    content += f'<input type="hidden" name="supersedes" value="{show(c["context_history"][-1]["id"]) if c["context_history"] else ""}">'
    content += '<button>Save effort context</button></fieldset></form>'
    content += details(c["context_history"], "Context history and availability timestamps")
    content += details(c.get('context_exclusions',{}), 'Context, review and label records excluded by cutoffs')
    provenance = {k: v for k, v in row.items() if k not in {"candidate", "samples", "source", "events"}}
    provenance["source"] = {k: v for k, v in row["source"].items() if k not in {"result", "qc"}}
    content += details(provenance, "Reproducibility and source header")
    content += f'<p><a href="/api{base}?{query(samples["offset"],samples["limit"])}">Verification JSON</a></p>'
    return views.page("Verify continuous effort", content + '<p id="effort-status" role="status"></p>' + SCRIPT)
