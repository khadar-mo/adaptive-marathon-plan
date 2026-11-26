"""Inspectable source-linked fitness state and editable runner-supplied descriptors."""
from html import escape
import json
from urllib.parse import urlencode

import views


def e(value):
    return escape("" if value is None else str(value), quote=True)


def link(url, label):
    return f'<a href="{e(url)}">{e(label)}</a>'


def details(title, value):
    return f'<details><summary>{e(title)}</summary><pre style="white-space:pre-wrap">{e(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>'


def field(name, title, value="", required=False, typ="text"):
    return f'<label>{e(title)}<input name="{e(name)}" value="{e(value)}" type="{e(typ)}"{" required" if required else ""}></label>'


def textarea(name, title, value):
    return f'<label>{e(title)}<textarea name="{e(name)}" rows="3" style="width:100%;box-sizing:border-box">{e(value)}</textarea></label>'


def select(name, title, vals, chosen):
    opts = ''.join(f'<option value="{e(v)}"{" selected" if v == chosen else ""}>{e(label)}</option>' for v, label in vals)
    return f'<label>{e(title)}<select name="{e(name)}">{opts}</select></label>'


def table(headers, rows):
    h = ''.join(f'<th scope="col">{e(k)}</th>' for k in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{v}</td>' for v in row) + '</tr>' for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def controls(state, query, configs):
    return ('<form action="/fitness"><fieldset><legend>Profile selection</legend>'
            + field("as_of", "Event cutoff (ISO time with offset)", query.get("as_of"))
            + field("knowledge_cutoff", "Knowledge cutoff (ISO time with offset)", query.get("knowledge_cutoff"))
            + select("mode", "Time basis", [(v, v.replace("_", " ").title()) for v in ("EVENT_TIME_RECONSTRUCTION", "AS_KNOWN")], state["time_basis"])
            + select("config_version", "Descriptor version", [(v, v) for v in configs], state["config_version"])
            + '<button>View profile</button></fieldset></form>')


def configuration_form(state):
    cfg = state["definition"]["config"]
    match = cfg["comparison"] or {}
    rpe = match.get("rpe") or {}
    def band(group, label, initial):
        values = initial or {}
        return field(group + "_low", label + " minimum", values.get("low")) + field(group + "_high", label + " maximum", values.get("high"))
    return ('<details id="configure"><summary>Define a descriptor version</summary><form id="fitness-config">'
            + field("version", "New descriptor version", required=True) + field("actor", "Configuration author", required=True)
            + field("reason", "Reason for this definition", required=True)
            + '<fieldset><legend>Recovery baseline</legend>' + field("baseline_days", "Lookback days", cfg["baseline_days"], True, "number")
            + field("minimum_days", "Minimum observed days", cfg["minimum_days"], True, "number")
            + field("expected_metrics", "Expected metrics (comma separated)", ", ".join(cfg["expected_metrics"]))
            + textarea("robust_deviations", "Robust-deviation rules (JSON array)", json.dumps(cfg["robust_deviations"], ensure_ascii=False))
            + '<small>Each optional rule names its metric, unit, source, method and calibration, with a positive epsilon and a direction of HIGHER, LOWER or BIDIRECTIONAL.</small>'
            + '</fieldset><fieldset><legend>Comparable runs</legend><p>Enter an exact route and conditions, inclusive duration and grade ranges, and a recorded HR or RPE band.</p>'
            + field("route", "Matched route", match.get("route")) + field("conditions", "Matched conditions", match.get("conditions"))
            + band("duration", "Duration (seconds)", match.get("duration_s")) + band("grade", "Grade (%)", match.get("grade_percent"))
            + band("hr", "Heart rate (bpm)", match.get("hr_bpm")) + band("rpe", "RPE", rpe.get("values"))
            + field("scale_version", "Matched RPE scale version", rpe.get("scale_version"))
            + '</fieldset><fieldset><legend>Continuous segments</legend>' + band("pace", "Sustained pace (seconds/km)", cfg["sustained_pace_s_per_km"])
            + field("late_after_m", "Late block after recorded metres", cfg["late_after_m"]) + '</fieldset>'
            + field("estimator_config", "Estimator configuration", cfg["estimator_config"], True)
            + '<button>Save descriptor version</button><p role="status"></p></form></details>')


def note_form(row, notes):
    current = notes[-1] if notes else {}
    rating = current.get("rpe") or {}
    return (f'<section id="run-context"><h2>Comparison evidence for run {row["activity_id"]}</h2><form id="fitness-observation" data-activity="{row["activity_id"]}" data-canonical="{e(row["canonical_version_id"])}" data-previous="{e(current.get("id"))}">'
            + field("route", "Recorded route", current.get("route")) + field("conditions", "Recorded conditions", current.get("conditions"))
            + field("grade_percent", "Recorded grade (%)", current.get("grade_percent")) + field("mean_hr_bpm", "Reported mean heart rate (bpm)", current.get("mean_hr_bpm"))
            + field("rpe_value", "Reported session RPE", rating.get("value")) + field("rpe_scale", "Reported RPE scale version", rating.get("scale_version"))
            + field("source", "Observation source", current.get("source"), True) + textarea("source_reference", "Source reference", current.get("source_reference"))
            + field("actor", "Reported by", current.get("actor"), True) + textarea("reason", "Entry or correction reason", "")
            + '<button>Save run evidence</button><p role="status"></p></form>'
            + details("Run evidence revisions", notes) + '</section>')


def profile(state, query, *, configs=(), history=(), record=None, notes=(), saved=None):
    content = f'<p>Event cutoff <time>{e(state["event_cutoff"])}</time> · Knowledge cutoff <time>{e(state["knowledge_cutoff"])}</time> · {e(state["config_version"])}</p>'
    content += '<p>' + e(state["time_basis"].replace("_", " ").title()) + ' · Descriptive fitness evidence</p>'
    if saved:
        root = "/api/fitness/snapshots/" + saved["snapshot_id"]
        content += '<p>' + " · ".join([link(root, "Frozen profile JSON"), link(root + "/inputs", "Frozen source evidence"), link(root + "/replay", "Rebuild and compare")]) + '</p>'
        changes = saved["changes"]
        content += f'<h2>Changes in this snapshot</h2><p>{len(changes)} changed fields.'
        if saved["previous_id"]:
            content += ' ' + link("/fitness/snapshots/" + saved["previous_id"], "Compare previous snapshot")
        content += '</p>' + details("Changed fields, reasons and values", changes)
    else:
        content += controls(state, query, configs)
        args = {k: v for k, v in query.items() if v and k != "activity_id"}
        content += '<p>' + link("/api/fitness?" + urlencode(args), "Profile JSON") + '</p>'
        content += f'<form id="fitness-save" data-query="{e(json.dumps(args))}"><button>Save fitness snapshot</button><span role="status"></span></form>'
    content += '<h2>Observed capability</h2>'
    recent_days = state["cs_candidate"]["primary_window_days"]
    rows = []
    for alltime, recent in zip(state["pb_all"], state["pb_recent"]):
        def summary(value, missing):
            if not value or value["status"] != "SUPPORTED_OBSERVATION":
                return e(missing)
            ref = value["ref"]
            path = f'/activities/{ref["activity_id"]}/efforts'
            if value.get("inspection_required"):
                path += "?" + urlencode({"version": ref["version_id"], "source": ref["source_sha256"]})
            else:
                path += f'/{ref["version_id"]}/{ref["candidate_id"]}?' + urlencode({"as_of": state["event_cutoff"], "knowledge_as_of": state["knowledge_cutoff"]})
            return e(f'{value["elapsed_seconds"]:.3f} s · {value["category"]} · ') + link(path, f'Run {ref["activity_id"]}')
        window = next((v for v in recent["windows"] if v["days"] == recent_days), {})
        rows.append([e(alltime["distance_m"]), summary(alltime["best"], alltime["state"]), summary(window.get("best"), window.get("state", "MISSING_RECENT"))])
    content += table(["Distance (m)", "All time", f"Recent ({recent_days} days)"], rows) if rows else '<p>No eligible observed intervals.</p>'
    content += '<h2>Candidate critical speed</h2>' + table(["Window", "Candidate (m/s)", "Status", "Evidence"], [
        [e(w["days"]), e(w["primary"]["cs_mps"] if w["primary"]["cs_mps"] is not None else "Unavailable"), e(w["primary"]["status"]), e(w["primary"]["fit_state"])]
        for w in state["cs_candidate"]["windows"]])
    content += '<p>Personal predictive validity is untested.</p><h2>Endurance and workload</h2>'
    end = state["endurance"]
    content += table(["Days", "Longest observed (m)", "Elapsed subtotal (s)", "Timer subtotal (s)", "History"], [
        [e(w["days"]), e(w["longest"]["observed_distance_m"] or "Unavailable"), e(w["elapsed_seconds"]["known_subtotal"]),
         e(w["timer_seconds"]["known_subtotal"]), e(w["longest"]["status"])] for w in end["windows"]])
    content += table(["Days", "Observed runs", "Distance subtotal (m)", "Coverage"], [
        [e(w["days"]), e(w["run_count"]), e(w["measures"]["distance_m"]["known_subtotal"]), e(w["measures"]["distance_m"]["status"])]
        for w in state["exposure"]["rolling"]])
    comp = end["comparable_effort"]
    content += f'<h3>Comparable runs</h3><p>{e(comp["status"])} · {comp["sample_count"]} matched · Median pace {e(comp["median_pace_s_per_km"] or "unavailable")} s/km.</p>' + details("Matching method, runs and exclusions", comp)
    content += '<h3>Continuous segments</h3><p>Sustained pace ' + e(end["sustained"]["status"]) + ' · Late blocks ' + e(end["late_blocks"]["status"]) + '</p>' + details("Sustained and late-block evidence", {k: end[k] for k in ("sustained", "late_blocks", "unavailable_traces")})
    content += '<h2>Recovery and strength</h2>'
    rec = state["recovery"]
    rows = []
    for name, metric in rec["wellness"].items():
        if not metric["streams"]:
            rows.append([e(name), "MISSING", "BASELINE_UNKNOWN", "0", "—", e(metric["reason"])])
        for stream in metric["streams"]:
            rows.append([e(name + " · " + stream["stream"]["source"] + " · " + stream["unit"]),
                         e(stream["current"] if stream["current"] is not None else stream["current_status"]),
                         e(stream["baseline"] if stream["baseline"] is not None else stream["baseline_status"]),
                         e(f'{stream["observed_days"]}/{stream["expected_days"]}'), e(stream["delta"] if stream["delta"] is not None else "—"),
                         e(stream["latest_observed_local_date"] or stream["baseline_reason"])])
    content += table(["Metric / source", "Current", "Prior baseline", "Days observed", "Change", "Freshness or reason"], rows)
    content += f'<p>Feedback {e(rec["feedback_status"])} · {len(rec["strength"])} strength reports · {len(rec["attention_signals"])} attention signals.</p>'
    content += details("Recovery sources, coverage and feedback", rec) + '<h2>Evidence quality</h2>' + details("Trace, provenance, recency, identifiability, validation and recovery", state["quality"])
    if not saved:
        content += '<h2>Run context and descriptors</h2>' + link("/context", "Enter wellness, strength or feedback")
        entries = end["session_evidence"]
        if entries:
            content += '<ul>' + "".join('<li>' + link("/fitness?" + urlencode({**args, "activity_id": r["activity_id"]}) + "#run-context", f'Edit comparison evidence for run {r["activity_id"]}') + ' · ' + e(r["event_end_utc"]) + '</li>' for r in reversed(entries)) + '</ul>'
        if record:
            content += note_form(record, notes)
        content += configuration_form(state)
        if history:
            content += '<h2>Saved profiles</h2><ul>' + ''.join('<li>' + link("/fitness/snapshots/" + s["id"], s["event_utc"] + " · " + s["config_version"]) + '</li>' for s in history) + '</ul>'
        content += SCRIPT
    return views.page("Fitness profile", content)


SCRIPT = """
<script>
document.querySelectorAll('#fitness-save,#fitness-config,#fitness-observation').forEach(form=>{
 form.addEventListener('submit',async event=>{
  event.preventDefault();
  const button=event.submitter,message=form.querySelector('[role=status]'),f=Object.fromEntries(new FormData(form));
  let url,body;
  const band=name=>{
   const low=f[name+'_low'], high=f[name+'_high'];
   if(!low && !high) return null;
   if(!low || !high) throw Error('Enter both limits for '+name+'.');
   return {low,high};
  };
  try {
   if(form.id==='fitness-save'){url='/api/fitness/capture'; body=JSON.parse(form.dataset.query);}
   if(form.id==='fitness-config'){
    url='/api/fitness/configurations';
    const rpe=band('rpe'),hr=band('hr'),duration=band('duration'),grade=band('grade');
    const match=f.route||f.conditions||hr||rpe||duration||grade||f.scale_version;
    body={version:f.version,actor:f.actor,reason:f.reason,config:{baseline_days:Number(f.baseline_days),minimum_days:Number(f.minimum_days),
     expected_metrics:f.expected_metrics.split(',').map(x=>x.trim()).filter(Boolean),robust_deviations:JSON.parse(f.robust_deviations||'[]'),
     estimator_config:f.estimator_config,comparison:match?{route:f.route,conditions:f.conditions,duration_s:duration,grade_percent:grade,hr_bpm:hr,
      rpe:rpe||f.scale_version?{scale_version:f.scale_version,values:rpe}:null}:null,sustained_pace_s_per_km:band('pace'),late_after_m:f.late_after_m||null}};
   }
   if(form.id==='fitness-observation'){
    url='/api/fitness/run-context'; body={source:f.source,source_reference:f.source_reference,actor:f.actor,reason:f.reason,
      canonical_version_id:form.dataset.canonical,previous_id:form.dataset.previous||null};
    for(const key of ['route','conditions','grade_percent','mean_hr_bpm']) body[key]=f[key]||null;
    body.rpe=f.rpe_value||f.rpe_scale?{value:f.rpe_value,scale_version:f.rpe_scale}:null;
   }
   let serialized=JSON.stringify(body);
   if(form.id==='fitness-observation'){serialized='{"activity_id":'+form.dataset.activity+','+serialized.slice(1);}
   button.disabled=true;message.textContent='Saving…';
   const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:serialized});
   const result=await response.json(); if(!response.ok) throw Error(typeof result.detail==='string'?result.detail:'Check the required fields and ranges.');
   if(form.id==='fitness-save'){location.assign(result.html_url);return;}
   const next=new URL(location.href);next.searchParams.delete('knowledge_cutoff');next.searchParams.set('mode','EVENT_TIME_RECONSTRUCTION');
   if(form.id==='fitness-config') next.searchParams.set('config_version',result.version);
   next.hash=form.id==='fitness-observation'?'run-context':'';location.assign(next.href);if(next.href===location.href)location.reload();
  } catch(error){message.textContent=error.message;} finally{button.disabled=false;}
 });
});
</script>
"""
