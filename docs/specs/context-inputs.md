# Optional context input contract — v1

Issue #29 implements the side streams in master v1.0 Part 2 §§2.1/2.3,
Part 3 §3.1, Part A §§3/10/12 and Part B §§3.2/9.1. The source SHA-256 is
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
These are ingestion interfaces. G5 owns baselines and interpretation; no clinical
threshold, readiness score, mileage change or recovery approval is created here.

## Entry and supported exports

Open **Context** in the local dashboard. Expand Wellness observation, Post-run
check-in, Pre-week readiness, Strength session or Source coverage. Enter the source
and a stable source record ID, then save. Use **Correct this record** to edit a
saved record; each correction requires a reason and preserves the previous version.
The input policy and rating-scale definitions are editable by registering a new
version. Existing version names cannot change meaning.

The supported source is the documented `context-export-v1` UTF-8 JSON/CSV interchange
format, including self-reports and explicitly mapped owned exports. The importer does
not guess proprietary Garmin export layouts or claim a live wellness integration.
Copy only measurements actually present in the source, retain the source's units and
method, and use an explicit missing record for unavailable fields. A future vendor
adapter must name and version its mapping before it can claim that format is supported.

JSON accepts a record or array of 1–1000 records (maximum 2 MiB). CSV uses the same
field names; blank cells omit optional fields. `ratings`, `exercises` and `attention`
use JSON within quoted CSV cells. Unknown fields, ambiguous duplicate JSON keys,
invalid dates, non-finite values and incomplete corrections are rejected. A batch
commits all its records or none. Valid source bytes are checksummed in the existing
private archive before references commit; rejected input can leave unreferenced
archive bytes, never partially committed contextual records.

For example, this synthetic date-only observation deliberately has no timestamp:

```json
{
  "kind": "wellness",
  "source": "self-report",
  "source_id": "synthetic-rhr-2026-10-25",
  "local_date": "2026-10-25",
  "timezone": "Europe/London",
  "metric": "resting_hr",
  "value": "48.12345678901234567890",
  "unit": "bpm",
  "origin": "self_report",
  "method": "runner-reported measurement"
}
```

The equivalent flat CSV can be imported with the CSV selection:

```csv
kind,source,source_id,local_date,metric,value,unit
wellness,self-report,synthetic-sleep-2026-10-25,2026-10-25,sleep_duration,25200.25,s
```

## Fields

Every record has `kind`, `source`, `source_id`, optional `policy_version`
(default `context-policy-v1`), and optional source-original `source_updated_at`.
Corrections supply the current `supersedes` version ID and `correction_reason`.
A stale correction returns HTTP 409. A stable `(kind, source, source_id)` identifies
one record; changed content must be an explicit correction. Byte-identical import
replay returns the original receipt and never restores an old head. Equivalent
normalized input creates no new version/event while preserving its additional raw
receipt. Precision changes are retained as changes, even if numeric values compare equal.

| Kind | Required fields beyond common identity | Optional inputs |
|---|---|---|
| `wellness` | `local_date`, `metric`, `unit`; value or an explicit missing record | `timezone` (Europe/London default), `observed_at`, `value_type` (`decimal`/`category`), `validity` (`VALID`/`SUSPECT`/`INVALID`/`MISSING`), `missing_reason`, `method`, `origin` (`device`/`self_report`/`laboratory`/`unknown`) |
| `post_run` | `activity_id`, offset-aware `reported_at`; supplied content or `missing_reason` | `ratings`, `pain_discomfort`, `pain_location`, `context`, `notes`, explicit `attention` flags |
| `preweek` | Monday `week`, offset-aware `reported_at`; supplied content or `missing_reason` | Same feedback fields; week timezone is Europe/London |
| `strength` | `local_date`, plus `duration_s` or a non-unknown `status` | `timezone`, `observed_at`, `lower_body_emphasis` (`yes`/`no`/`unknown`), `exercises`, `ratings`, `soreness_reported_at`, `notes` |
| `coverage` | `domain`, inclusive `first`/`last`, `status`, `basis`, `reported_at`, `reason` | `timezone`, wellness `metric` |

Wellness `value` uses exact decimal text, or a category with `value_type=category`.
Raw JSON numeric lexemes are also parsed without binary-float rounding. Values are
bounded to 100 significant digits and exponent magnitude 100 to bound input size.
Units remain source units; no implicit conversions occur. SI sources retain their
SI precision, and incompatible units are kept separately. A missing value requires
`validity=MISSING` and a reason. Invalid/suspect observations remain visible but do
not count as valid observed days. `method=UNKNOWN` is explicit when not supplied.
Device-origin and recognized device metric names (`vo2max`, `hrv_status`,
`sleep_stages`, `body_battery`, `stress`, `max_hr`, `maximum_hr`, `readiness`,
`training_readiness`, `sleep_score`) carry `DEVICE_OUTPUT`; this is not physiological
certainty or an individually validated threshold.

Ratings have the shape `{"rpe":{"value":"4.5","scale_version":"rpe-0-10-v1"}}`.
Feedback allows `rpe`, `fatigue`, `soreness`, `pain`, `readiness`; strength allows
`rpe` and `soreness`. Every supplied rating explicitly selects a versioned scale.
The built-in RPE scale is an available choice, not an assumed symptom/readiness
scale. Register the intended alternative before entering those ratings. Definitions
contain `name`, exact `minimum`/`maximum`, `unit`, `description` and optional
`anchors` mapping decimal strings to labels. Validation checks the chosen bounds;
there are no scale conversions or pooled comparisons. Pain attention uses explicit
`distinct_pain`, `worsening_symptoms`, `illness` flags, never a numeric cutoff or a
diagnosis inferred from notes. These return `PLANNER_REVIEW_ONLY` signals.

There is one check-in per source and target. Update it through corrections rather
than inventing another source ID. Independent source reports remain separate;
multiple reports after explicit activity merging are disclosed. A report may be
explicitly missing; absence never means zero fatigue, recovery or illness. A report's
original activity ID and canonical entry version are retained across donor redirects
and provider alias reclassification. `target_now` is a labelled current display aid,
not historical as-of evidence. Provider ID movement never silently retargets a report.
Changing a report's target requires separately correcting the old report to missing
and creating a report for the correct target; historical target identity is immutable.

Strength status is `completed`, `partial`, `not_completed` or `unknown`. Exercises
are runner-owned entries with `name`, optional integer `sets`, free-form `reps`,
exact `load` and mandatory paired `load_unit`. Later soreness has its own report time
and is added by correction. A future soreness timestamp withholds that version at an
earlier cutoff. No approximately-weekly baseline session is fabricated; no strength
row enters running activities, kilometre totals or a combined-load index.

Coverage status is `COMPLETE`, `PARTIAL` or `UNAVAILABLE`, with explicit
`athlete_attestation` or `import_manifest` basis. COMPLETE requires the last local
day to have ended both by the report time and by server receipt time. Observation
presence alone cannot establish coverage. Coverage reports can describe unsupported
fields without fabricating values; a complete source interval with no HRV observation
still has MISSING HRV. Conflicting declarations remain separate auditable reports.

## Time and aggregation decisions

`observed_at`, `reported_at`, `source_updated_at` and later soreness time retain
their original offset strings. Timestamped event moments also normalize to UTC.
Wellness/strength local date must agree with its supplied instant in the supplied
IANA timezone, including DST. Unknown measurement times stay null; no midnight or
noon value is invented. Date-only rows expose `UNKNOWN_INSTANT` and an explanation.

First-known time, each version/correction's known-at time and each raw receipt's
known-at time are assigned by the server. They cannot be backdated by an input field.
An as-of read first selects the latest version actually known at the cutoff; then
withholds future report/measurement times and future local observation dates.
A Sunday report targeting the coming Monday is eligible on Sunday once known.
A current-day date-only report can be known prospectively while its precise event-time
eligibility remains unknown; it never establishes full-day coverage. G6 must retain
that uncertainty rather than treating it as a timestamped observation.

`context-policy-v1` retains all observations. A single valid observation in a group
is reported directly; multiple valid observations are `MULTIPLE_OBSERVATIONS` with
no silently chosen daily value. An explicitly selected policy may use `median` for
numeric groups. Groups require the same date, timezone, metric, unit, source, method,
value type and interpretation. No cross-unit/method/source aggregation or categorical
median is performed. Exact decimal arithmetic is retained in even-sized medians.
This daily selection is an engineering policy; baseline medians/deltas are G5 work.
Expected metrics default to resting HR, HRV and sleep duration only to expose missingness.
They are configurable expectations, not claims of device support.

## Public interfaces

| Interface | Behavior |
|---|---|
| `POST /api/context/records` | JSON record/batch entry or correction; immutable receipt and IDs |
| `POST /api/context/import?format=json|csv` | Multipart `file` upload using the same contract |
| `GET /api/context` | Current optional context, daily groups, units/missingness, counts, coverage reports and attention signals |
| `GET /api/context?as_of=...&first=...&last=...&policy_version=...` | Known-at cutoff and optional inclusive date selection under an immutable policy |
| `GET /api/context/records/{record_id}?as_of=...` | All record versions known by cutoff; raw checksums/indices and reasons |
| `GET /api/context/events?after=0&limit=200&as_of=...` | Append-only events, stable IDs, sequence cursor and complete version/source lineage; maximum limit 1000 |
| `GET /api/context/feedback?activity_id=...` or `?week=YYYY-MM-DD` | Explicit target check-in/missing state; optional as-of cutoff |
| `GET /api/context/definitions` | Immutable policy and scale definitions |
| `POST /api/context/definitions/policy/{version}` | Register `daily_aggregation` and `expected_wellness_metrics` |
| `POST /api/context/definitions/scale/{version}` | Register a scale definition |

Date-range filters use observation local dates, pre-week target weeks, or feedback
report dates as appropriate. Coverage intervals are selected by overlap. Explicit
as-of cutoffs always use availability plus report/measurement time, independently
of target date. The event feed includes every correction, including inputs not yet
eligible by event time, with its eligibility labelled. Consumers must not confuse
emission/known time with event time. Context records/history are not a full G6
prediction ledger or a claim of complete historical identity reconstruction.

The standalone CLI never contacts a provider:

```sh
.venv/bin/python context_data.py --database /path/to/runs.sqlite3 import /path/to/export.json
.venv/bin/python context_data.py --database /path/to/runs.sqlite3 import /path/to/export.csv --format csv
.venv/bin/python context_data.py --database /path/to/runs.sqlite3 show --as-of 2026-11-25T22:00:00Z
```

SQLite writes share the existing process lock and transaction; the archive and
legacy import/history/Strava interfaces retain their previous behavior. Back up the
SQLite database and archive as a pair. GET views do not create or backfill records.
