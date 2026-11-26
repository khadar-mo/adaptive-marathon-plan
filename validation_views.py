"""Inspector for an immutable cutoff, exact training inputs and the benchmark ledger."""
from fitness_views import details, e, field, link, select, table
import fm_snapshots as snapshots
import forecast_validation as validation
import views


def choices(records):
    return [(r["id"], f'{r["event_cutoff"]} · {r["kind"]} · {r["id"][:10]}') for r in records]


def observation_choices(rows):
    return [(r["observation_id"], f'Run {r["ref"]["activity_id"]} · {r["distance_m"]} m · {r["elapsed_coordinate_s"]} s · '
        f'{r["kind"]} · {r["provenance"]["category"]}') for r in rows]


def form(endpoint, content, button, *, kind=""):
    return f'<form data-endpoint="{e(endpoint)}" data-kind="{e(kind)}">{content}<button>{e(button)}</button><span role="status"></span></form>'


def page(store, identity=None, previous=None):
    history = snapshots.history(store)
    args = choices(history)
    content = (form("/api/snapshots", '<fieldset><legend>Create a fitness snapshot</legend>' +
        field("as_of", "Event cutoff (UTC or offset)") + field("knowledge_cutoff", "Knowledge cutoff (UTC or offset)") +
        select("mode", "Cutoff policy", [("AS_KNOWN", "As known at the event cutoff"), (snapshots.RECONSTRUCTION, "Event time with later evidence")], "AS_KNOWN") +
        field("config_version", "Fitness configuration (optional)") + '</fieldset>', "Save snapshot", kind="snapshot") +
        '<p>Blank cutoffs capture the local history as known now. Historical as-known snapshots require equal event and knowledge cutoffs.</p>')
    if not history:
        return views.page("Fitness snapshots and benchmarks", content + SCRIPT)
    identity = identity or history[0]["id"]
    item = snapshots.load_snapshot(store, identity)
    url = "/api/snapshots/" + identity
    content += '<form action="/validation">' + select("snapshot", "Saved snapshot", args, identity) + '<button>Inspect snapshot</button></form>'
    content += '<h2>Snapshot evidence</h2>' + table(["Event cutoff T", "Knowledge cutoff K", "Saved C", "Time basis"],
        [[e(item[key]) for key in ("event_cutoff", "knowledge_cutoff", "created_at", "kind")]])
    content += '<p>' + ' · '.join([link(url + "/inputs", "Frozen input and configuration ledger"),
        link(url + "/replay", "Replay from frozen inputs"), link(url + "/planner", "Planner actuals JSON"),
        link(url, "Full snapshot JSON"), link("/readiness?snapshot_id=" + identity, "Inspect readiness for this snapshot")]) + '</p>'
    runs = []
    for r in item["planner"]["actuals"]:
        runs.append([str(r["activity_id"]), e(r["start_utc"]), e(r["end_utc"]), e(r["distance_m"]), e(r["measures"]["elapsed_seconds"]),
                     link(url + "/activities/" + str(r["activity_id"]), "Frozen source and ownership")])
    content += table(["Run", "Accepted start", "Accepted end", "Distance (m)", "Elapsed (s)", "Source"], runs)
    content += details("Coverage, intensity and unassigned actuals", {k: item["planner"][k] for k in
                       ("coverage", "intensity", "unassigned", "excluded", "processing_gaps")})
    content += details("Verification and planning authority", item["validation"])
    if len(history) > 1:
        content += '<form action="/validation"><input type="hidden" name="snapshot" value="' + e(identity) + '">' + select("previous",
            "Compare with snapshot", [(v, label) for v, label in args if v != identity], previous or item["previous_id"]) + '<button>Compare snapshots</button></form>'
    delta = snapshots.compare(store, identity, previous)["changes"] if previous else item["changes"]
    content += details("Field changes and reasons", delta)
    candidates = validation.selectable(store, identity)
    opts = observation_choices(candidates)
    content += '<h2>Schedule a benchmark</h2>' + form("/api/validation/predictions",
        '<input type="hidden" name="snapshot_id" value="' + e(identity) + '">' +
        field("benchmark", "Benchmark name", required=True) + field("scheduled_start", "Scheduled start (UTC or offset)", required=True) +
        field("distance_m", "Target distance (metres)", required=True) + select("kind", "Forecast timing",
            [(k, k.replace("_", " ").title()) for k in validation.KINDS], "PROSPECTIVE" if item["kind"] == snapshots.AS_KNOWN else item["kind"]) +
        select("riegel_observation_id", "Riegel source effort", [("", "Unspecified / unavailable"), *opts], ""),
        "Freeze forecast", kind="forecast")
    content += '<p>The comparison baseline uses the last qualified whole workout at the target distance in the selected window.</p>'
    ledger = validation.list_ledger(store)
    if ledger["predictions"]:
        content += '<h2>Record an observed outcome</h2>' + form("/api/validation/predictions",
            select("prediction_id", "Frozen forecast", [(p["id"], f'{p["benchmark"]} · {p["scheduled_start"]} · {p["kind"]}') for p in ledger["predictions"]], "") +
            '<input type="hidden" name="outcome_snapshot_id" value="' + e(identity) + '">' +
            select("outcome_observation_id", "Outcome from this snapshot", opts, ""), "Record outcome", kind="outcome")
    content += '<h2>Forecasts and results</h2>'
    for p in ledger["predictions"]:
        content += '<h3>' + e(p["benchmark"]) + '</h3>' + table(["Method", "Frozen elapsed (s)", "Unavailable reason"], [
            [e(m), e(v["predicted_seconds"]), e(v["reason"])] for m, v in p["methods"].items()])
        content += '<p>' + link("/api/validation/predictions/" + p["id"] + "/replay", "Replay frozen forecast") + '</p>'
        content += details("Forecast timing, method and evidence", p)
    if not ledger["predictions"]:
        content += '<p>No frozen forecasts.</p>'
    for outcome in ledger["evaluations"]:
        content += details("Observed outcome · " + outcome["status"], outcome)
    report = validation.backtest_estimators(ledger)
    content += '<h2>Held-out error ledger</h2><p>Positive bias means the predicted time was longer. Personal predictive validity is unestablished.</p>'
    for s in report["strata"]:
        content += f'<h3>{e(s["distance_m"])} m · {e(s["provenance"])} · {e(s["kind"])}</h3>'
        content += table(["Method", "Common N", "MAE (s)", "Bias (s)", "MAPE (%)", "Reason"], [
            [e(m), str(v["n"]), e(v["mae_s"]), e(v["bias_s"]), e(v["mape_percent"]), e(v["reason"])] for m,v in s["metrics"].items()])
        content += details("Horizon, missingness and common outcomes", s)
    if not report["strata"]:
        content += '<p>N = 0. No qualifying held-out outcomes.</p>'
    return views.page("Fitness snapshots and benchmarks", content + '<p>' + link("/api/validation/backtest", "Error ledger JSON") + '</p>' + SCRIPT)


SCRIPT = """
<script>
document.querySelectorAll('form[data-endpoint]').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter, status = form.querySelector('[role=status]');
  const data = Object.fromEntries([...new FormData(form)].filter(([, value]) => value !== ''));
  let url = form.dataset.endpoint;
  if (form.dataset.kind === 'outcome') { url += '/' + encodeURIComponent(data.prediction_id) + '/outcomes'; delete data.prediction_id; }
  button.disabled = true;
  try {
    const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const value = await response.json();
    if (!response.ok) { status.textContent = typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail); return; }
    const id = form.dataset.kind === 'snapshot' ? value.id : (data.snapshot_id || data.outcome_snapshot_id);
    location.assign('/validation?snapshot=' + encodeURIComponent(id));
  } catch (error) { status.textContent = String(error); }
  finally { button.disabled = false; }
}));
</script>
"""
