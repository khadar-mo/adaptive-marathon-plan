"""Accessible G4 controls and evidence tables; no combined confidence score."""
from html import escape
import json
from urllib.parse import urlencode

import views
from effort_views import details, table, show
import estimator_math as maths


NOTICE = ('<p>Observed achievements anchor these comparisons. Riegel and critical speed are experimental; '
          'they do not establish maximal physiology or prescribe training. Personal predictive validity: <strong>UNTESTED</strong>.</p>'
          '<p>Event-time reconstruction uses current source selections and known context. It is not a prediction frozen at the event cutoff. '
          'Recovery completeness is not assessed here; missing recovery data is not clearance.</p>')


def number(value):
    return format(value, '.9g') if isinstance(value, (int, float)) else 'Not available'


def source_link(row):
    r, c = row["ref"], row["candidate"]
    # A window-constrained G2 winner may exist only in the copied ledger. Link
    # its owning activity, not an undurable candidate URL that would return 404.
    return f'<a href="/activities/{r["activity_id"]}/efforts?version={r["version_id"]}">Activity {r["activity_id"]}</a> · {number(c["distance_m"])} m'


def result_content(report):
    ledger = {o["id"]: o for o in report["evidence"]["observations"]}
    def observation(oid):
        if not oid:
            return 'Not available'
        o = ledger[oid]
        return f'<a href="#observation-{oid}">{number(o["candidate"]["elapsed_seconds"])} s</a> · {source_link(o)}'
    content = '<h2>Achieved distance profile</h2><p>Metres divided by recorded elapsed seconds; no projection to another distance.</p>'
    content += table('Achieved profile', ('Distance (m)', 'All time', 'Recent windows'), (
        (number(r['distance_m']), show(r['all_time_state']) + '<br>' + observation(r['all_time']['observation_id']) + f'<br>{number(r["all_time"]["speed_mps"])} m/s' if r['all_time'] else show(r['all_time_state']),
         ''.join(f'<p>{recent["days"]} days · {show(recent["state"])}<br>{observation(recent["observation_id"])}<br>{number(recent["speed_mps"])} m/s</p>' for recent in r['recent'])) for r in report['observed_profile']))
    content += '<h2>Separate Riegel comparisons</h2><p>' + escape(report['riegel']['selection_policy']) + '</p>'
    content += table('Riegel source and target comparisons', ('Source effort', 'Target (m)', 'Projected elapsed (s)', 'k / distance ratio', 'Evidence and uncertainty'), (
        (observation(r['source_observation_id']), number(r['target_distance_m']), number(r['elapsed_seconds']),
         f'{number(r["k"])} / {number(r.get("distance_ratio"))}',
         show(r['comparison_state']) + '<br>' + show(r['reason']) + '<br>' + show(r['uncertainty']) + details(r['qualification'], 'Comparison qualification')) for r in report['riegel']['comparisons']))
    if not report['riegel']['comparisons']:
        content += '<p>No eligible recent Riegel sources. Historical PBs are not substituted.</p>'
    content += details(report['riegel']['excluded'], 'Riegel unavailable sources')
    content += '<h2>Critical speed and D′</h2><p>Centered distance-on-time OLS. Signed D′ is retained. R² measures this line’s fit only. '
    content += 'The three-distance minimum is procedural; it does not validate physiology. Unset review thresholds mean UNKNOWN.</p>'
    primary_days = report['critical_speed']['primary_window_days']
    for window in report['critical_speed']['windows']:
        p = window['primary']
        content += f'<h3>{window["days"]}-day window' + (' · selected default' if window['days'] == primary_days else '') + '</h3>'
        content += f'<p>{show(window["state"])} · {show(p["fit_state"])} · {show(p["reason"])}</p>'
        content += table(f'CS parameters {window["days"]} days', ('CS (m/s)', 'D′ (m)', 'R²', 'Source activities'), [
            (number(p['cs_mps']), number(p['d_prime_m']), number(p['r_squared']), str(p['source_overlap']['distinct_activities']))])
        content += table(f'OLS residuals {window["days"]} days', ('Effort', 'Elapsed (s)', 'Observed (m)', 'Fitted (m)', 'Residual (m)'), (
            (observation(r['observation_id']), number(r['elapsed_seconds']), number(r['observed_distance_m']), number(r['fitted_distance_m']), number(r['residual_m'])) for r in p['residuals']))
        content += '<p>Flags: ' + (', '.join(escape(f) for f in p['flags']) or 'None; missing policies remain unknown.') + '</p>'
        content += details(p['source_overlap'], 'Source activity overlap')
        content += details({'parameter_checks': p.get('policy_checks'), 'sensitivity_checks': window['sensitivity_policy_checks'],
                            'date_span_review': window['date_span_review']}, 'Configured review checks')
        content += '<details><summary>Distance subset comparisons</summary>' + table(f'Subsets {window["days"]} days',
            ('Distances (m)', 'CS (m/s)', 'D′ (m)', 'R²', 'Status / original P02 group'), (
            (show(f['distances_m']), number(f['cs_mps']), number(f['d_prime_m']), number(f['r_squared']),
             show(f['status']) + (' · 400/800/5000' if f['original_p02_group'] else '')) for f in window['subsets'])) + '</details>'
        content += details(window['subset_distribution'], 'Subset method sensitivity')
        loo = window['leave_one_distance_out']
        content += f'<p>Leave-one-distance-out: <strong>{show(loo["state"])}</strong>. {show(loo["reason"])}</p>'
        content += details(loo, 'Leave-one-distance-out calculations')
        content += details(window['excluded'], 'Window source exclusions')
    content += details(report['critical_speed']['window_distribution'], 'Window method sensitivity')
    content += '<h2>Copied input ledger</h2><p>Each calculation retains its exact source geometry, QC, context and review revisions. '
    content += 'Source links open the original extraction; the evidence below remains the saved copy.</p>'
    for o in ledger.values():
        c = o['candidate']
        content += f'<section id="observation-{o["id"]}"><h3>{source_link(o)} · {number(c["elapsed_seconds"])} elapsed s</h3>'
        content += f'<p>{show(c["start_utc"])} → {show(c["end_utc"])}. Trace: {show(c["effective_quality_state"])}. '
        content += f'Provenance: {show(c["provenance"]["category"])}. Qualification: {show(o["qualification"]["comparison_basis"])}. Age: {number(o["age_days"])} days.</p>'
        content += table('Copied endpoint brackets', ('Endpoint', 'Raw indices', 'Fraction', 'Crossing UTC'), (
            (name, show(c[name]['sample_indices']), number(c[name]['fraction']), show(c[name]['timestamp_utc'])) for name in ('start', 'end')))
        content += details(o, 'Complete observation and known context') + '</section>'
    content += details(report['evidence']['g2_selection']['excluded'], 'G2 exclusions and unresolved selection limits')
    content += details(report['validation'], 'Separate validation and unavailable interval')
    content += details({k: report[k] for k in ('input_hash', 'event_cutoff_utc', 'knowledge_cutoff_utc', 'time_basis', 'knowledge_time_frozen',
                                             'algorithm_version', 'code_sha256', 'config_version', 'config')}, 'Calculation lineage and configuration')
    return content


SCRIPT = """<script>
const status = document.getElementById('estimator-status');
async function send(form, body) {
 const button = form.querySelector('button[type=submit]'); button.disabled = true; status.textContent = 'Calculating…';
 try { const response = await fetch(form.getAttribute('action'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Check the settings and source selections.');
  if (result.html_url) location.assign(result.html_url);
  else { const url = new URL(location.href); url.searchParams.set('config_version', result.version); location.assign(url); }
 } catch(error) {status.textContent = error.message; button.disabled = false;}
}
document.getElementById('calculate-form').addEventListener('submit', event => {
 event.preventDefault(); const form = event.currentTarget, data = new FormData(form);
 const body = {cutoff:data.get('cutoff'), config_version:data.get('config_version')};
 if (!data.has('default_riegel')) body.riegel_sources = data.getAll('riegel').map(JSON.parse);
 if (!data.has('default_cs')) body.cs_sources = [...data.entries()].filter(([key,value]) => key.startsWith('cs-') && value).map(([,value]) => JSON.parse(value));
 send(form, body);
});
document.getElementById('config-form').addEventListener('submit', event => {
 event.preventDefault(); const form = event.currentTarget, data = new FormData(form), policy = {};
 for (const [key, value] of data.entries()) if (key.startsWith('policy-')) policy[key.slice(7)] = value.trim() === '' ? null : Number(value);
 const numbers = value => value.split(',').map(x=>Number(x.trim()));
 send(form, {version:data.get('version'), config:{riegel_k:Number(data.get('riegel_k')), recent_days:Number(data.get('recent_days')),
  target_distances_m:numbers(data.get('target_distances_m')), cs_distances_m:numbers(data.get('cs_distances_m')), review_policy:policy}});
});
</script>"""


def dashboard(data):
    report, config = data['preview'], data['preview']['config']
    content = NOTICE + '<form method="get"><fieldset><legend>View current evidence</legend>'
    content += f'<label>Event cutoff UTC<input name="cutoff" value="{show(report["event_cutoff_utc"])}" required></label>'
    content += '<label>Configuration<select name="config_version">' + ''.join(
        f'<option value="{show(d["version"])}"' + (' selected' if d['version'] == report['config_version'] else '') + f'>{show(d["version"])}</option>' for d in data['configurations']) + '</select></label>'
    content += '<button>Refresh evidence</button></fieldset></form>'
    content += '<h2>Calculate and save</h2><p>Defaults use each window’s G2 bests. To compare a saved non-PB interval, clear the relevant default checkbox and select its source below. '
    content += 'Choose at most one CS source per distance. Different choices are separate saved evaluations.</p>'
    content += '<form id="calculate-form" method="post" action="/api/estimators">'
    content += f'<input type="hidden" name="cutoff" value="{show(report["event_cutoff_utc"])}"><input type="hidden" name="config_version" value="{show(report["config_version"])}">'
    content += '<label><input type="checkbox" name="default_riegel" checked>Use window bests for Riegel</label><label><input type="checkbox" name="default_cs" checked>Use window bests for CS</label>'
    rows = []
    for i, option in enumerate(data['catalog']):
        value = escape(json.dumps(option['ref'], separators=(',', ':')), quote=True)
        label = f'{number(option["distance_m"])} m · {number(option["elapsed_seconds"])} s · activity {option["ref"]["activity_id"]} · {option["end_utc"]}'
        disabled = ' disabled' if option['unavailable_reasons'] else ''
        riegel = f'<label><input type="checkbox" name="riegel" value="{value}"{disabled}>Riegel source {i + 1}</label>'
        cs = f'<label><input type="radio" name="cs-{option["distance_m"]}" value="{value}"{disabled}>CS source {i + 1}</label>' if option['distance_m'] in config['cs_distances_m'] else 'Unsupported CS distance'
        rows.append((escape(label), riegel, cs, show(option['quality']) + '<br>' + show(option['provenance']['category']) + '<br>' + show(option['unavailable_reasons'])))
    content += table('Available source efforts', ('Effort', 'Riegel selection', 'CS selection', 'Quality and exclusions'), rows)
    content += '<button type="reset">Reset selections</button> <button type="submit">Calculate and save evaluation</button></form><p id="estimator-status" role="status" aria-live="polite"></p>'
    content += '<details><summary>Create a configuration version</summary><p>Defaults are provisional. Blank review thresholds remain UNKNOWN. '
    content += 'Setting a threshold requests a review; it does not calibrate a physiological limit.</p><form id="config-form" method="post" action="/api/estimators/configurations"><fieldset><legend>New immutable configuration</legend>'
    content += '<label>New version name<input name="version" pattern="[A-Za-z0-9._\\-]{1,80}" required></label>'
    content += f'<label>Riegel exponent k<input type="number" step="any" min="0.000001" name="riegel_k" value="{number(config["riegel_k"])}" required></label>'
    content += '<label>Primary recent days<select name="recent_days">' + ''.join(f'<option' + (' selected' if d == config['recent_days'] else '') + f'>{d}</option>' for d in maths.WINDOWS) + '</select></label>'
    for key, label in (('target_distances_m', 'Riegel targets in metres, comma separated'), ('cs_distances_m', 'CS distances in metres, comma separated')):
        content += f'<label>{label}<input name="{key}" value="{show(", ".join(number(d) for d in config[key]))}" required></label>'
    for key, value in config['review_policy'].items():
        label = {'d_prime_min_m':'D′ lower review bound (m)', 'd_prime_max_m':'D′ upper review bound (m)',
            'max_loo_relative_cs_change':'Maximum leave-one-out relative CS change', 'max_subset_relative_cs_range':'Maximum subset relative CS range',
            'max_activity_fraction':'Maximum activity contribution fraction', 'max_effort_date_span_days':'Maximum effort date span (days)',
            'far_extrapolation_ratio':'Distant extrapolation distance ratio'}[key]
        content += f'<label>{label}<input type="number" step="any" name="policy-{key}" value="{number(value) if value is not None else ""}"></label>'
    content += '<button type="submit">Save configuration version</button></fieldset></form></details>'
    content += '<h2>Saved evaluation history</h2>' + table('Saved evaluations', ('Created UTC', 'Event cutoff', 'Configuration', 'Open'), (
        (show(r['created_at']), show(r['cutoff_utc']), show(r['config_version']), f'<a href="/estimators/{r["id"]}">Open saved evaluation</a>') for r in data['history']))
    content += '<h2>Current preview</h2><p>This preview is read-only. Calculate and save preserves the ledger for later inspection.</p>' + result_content(report)
    content += '<p><a href="' + escape('/api/estimators?' + urlencode({'cutoff':report['event_cutoff_utc'], 'config_version':report['config_version']})) + '">Estimator preview JSON</a></p>'
    return views.page('Performance estimators', content + SCRIPT)


def saved(report):
    content = NOTICE + '<p><a href="/estimators">Configure another evaluation and view history</a></p>'
    content += f'<p><strong>Saved immutable evaluation</strong> · created {show(report["created_at"])} · event cutoff {show(report["event_cutoff_utc"])}</p>'
    content += result_content(report)
    content += f'<p><a href="/api/estimators/{report["id"]}">Saved evaluation JSON and complete copied source evidence</a></p>'
    return views.page('Saved performance evaluation', content)
