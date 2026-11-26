# Adaptive marathon plan

## Check Garmin or Strava

Run either command from this directory with Python 3. The provider commands use the standard library and work with the local app stopped. Set the selected provider's credentials in `.env` (see `.env.example`) or your environment.

```sh
python3 runs.py garmin --limit 1
python3 runs.py strava --limit 1
```

The default is ten runs. Use `--limit` to request 1–10. An empty list is a successful connection. To grant Strava activity access or reconnect, run `python3 runs.py strava --connect`. The example callback uses port 8000.

For Garmin, **Time** is the watch timer duration (`duration`). Calculate training pace only when timer time and distance are both positive, by dividing timer seconds by kilometres. **Elapsed Time** (`elapsedDuration`) covers the whole activity from start to save, including pauses. If a timing field is missing, its column shows a dash. [Garmin’s timing definitions](https://support.garmin.com/en-GB/?faq=k5TPjwyAWi5f4hnObUAVf7)

If Garmin rejects the token exchange with HTTP 401, the message identifies where the CLI found the key. An exported `GARMIN_PERSONAL_API_KEY` takes precedence over the value in `.env`. To try the file's value for one run, use `env -u GARMIN_PERSONAL_API_KEY python3 runs.py garmin --limit 1`. Use the raw key with no authorization prefix. You can check the key's active status and expiration on the Personal API Key page in Garmin Connect. [Garmin authentication FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq)

To verify Garmin, stop the local app, run the one-run command above and open the activity link in Garmin Connect. Compare the ID in the link, recorded local start, distance in kilometres at the same precision, and **Elapsed Time** within one second under **Stats → Timing**. The **Time** row there corresponds to the CLI's first duration column. Keep the activity and measurements private. Report only the match result.

## Sync Garmin history

From the repository directory, use Python 3.11 or newer to install the server and start one local process.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m uvicorn app:app --host 127.0.0.1 --port 8001 --no-access-log
```

Open [Imports](http://127.0.0.1:8001/imports) and select **Sync Garmin**. With both dates blank, the first sync checks 1 June 2026 through today in Europe/London. Later syncs revisit the last 14 continuously verified dates and catch up through today. The same attempt retries outstanding failures on older dates. Each retry range has its own coverage in the report.

To recheck October, enter 1 October 2026 and 31 October 2026. Garmin search covers an extra day on either side and reads every page through an empty result. The importer checks the sport and recorded local date, refreshes details for each run, and reuses good archived originals. Normal sync refreshes details when a list record changes or its detail is missing.

The report lists saved Garmin IDs, counts of inserts, updates and unchanged runs, exclusions, fully checked dates and any missing activities or originals. Each scan shows the Garmin IDs it saved, including IDs linked to the same local activity. JSON formatting changes preserve both raw responses and count as unchanged. A checked empty range has no returned runs. An unread or repeated page leaves coverage incomplete. A missing original is a warning and its summary still counts. **Recheck these dates** retries the range and preserves existing activity IDs.

The first valid submission creates `runs.sqlite3` in the working directory and archives Garmin's original response bytes in `runs-archive/` beside the database. To keep them elsewhere, set `RUNS_DB_PATH` and optionally `RUNS_ARCHIVE_PATH` before starting the server. Use a dedicated archive directory with mode `0700`. The app restricts the database, its journal files and archived sources to the owner before writing or reusing them. Stop the server and back up or restore the pair together.

To reopen a run offline, stop the process and repeat the start command with `RUNS_GARMIN_OFFLINE=1` and the same storage paths. Open the saved report and the activity's **Activity JSON** link. Startup, report refresh and activity views use local storage. An interrupted report retains its dates and any activities already committed.

The JSON endpoint accepts `{}` for normal sync or an inclusive pair such as `{"from":"2026-10-01","to":"2026-10-31"}` at `POST /api/imports/garmin`, returning HTTP 202 with `id`, `report_url` and `html_url`. `GET /api/imports` lists attempts newest first. `GET /api/imports/{id}` and `GET /api/activities/{id}` read individual reports and activities. Date validation runs before storage writes. A concurrent submission returns HTTP 409 and the running report links.

Back up the database and archive together before upgrading. On the first submission the app adds scan tracking to an existing database. Earlier one-page reports and their saved activities remain available. A new normal sync verifies coverage from June.

## Choose running data sources

Open **Sources** at `/providers` to save one of four modes. The default is
`GARMIN_ONLY`. Choices are versioned; a repeated save is idempotent and stale
edits require a reload. **Import selected sources** uses the saved revision for
that attempt, even if you change the choice while the import is running.

| Mode | Current analytical import behaviour |
|---|---|
| `GARMIN_ONLY` | Uses the existing Garmin importer; exported FIT also works without Garmin or Strava credentials. |
| `STRAVA_ONLY` | Returns `POLICY_BLOCKED`; no Strava activity fetch or persistence. |
| `BOTH` | Imports Garmin and explicitly reports the blocked Strava branch. |
| `GARMIN_PRIMARY_FALLBACK` | Imports Garmin first. An empty relevant response or a provider failure records its own fallback cause; Strava fallback remains `POLICY_BLOCKED`. |

The [Strava API Policy](https://www.strava.com/legal/api_policy), effective
1 June 2026 and checked 25 November 2026, restricts the intended analytics,
combination and persistent storage. [Issue #33](https://github.com/khadar-mo/adaptive-marathon-plan/issues/33)
tracks external enablement. OAuth success does not remove the block; no runtime
flag enables persistent Strava ingestion. Existing `runs.py strava --limit` and
`--connect` retain their transient listing interface. Their saved grant now keeps
accepted scopes and authenticated athlete ID when supplied; older grants keep
unknown metadata. Optional `request(..., response_metadata={})` exposes read and
overall rate-limit headers without changing the returned payload. A 429 stops
the request and reports reset/retry advice; the CLI does not retry quota failures.

Mode selection controls **Import selected sources**. Explicit **Sync Garmin**
and **Upload FIT files** on Imports remain available in every mode. Changing
modes never filters or erases saved actuals. Source status shows exact canonical
versions, linked provider IDs, original checksums, unresolved matches, and observed
tiers A/B/C. Summary-only evidence remains `INSUFFICIENT` for continuous efforts;
missing timer events remain `NOT_PROVIDED`. Contextual wellness/feedback is assessed
separately, so activity ingress does not claim Tier D. Fully checked provider dates
mean the scan completed, not proof that every real-world run exists in that account.
An empty response is distinct from an outage; failed and unscanned dates remain
incomplete. Offline FIT uploads do not certify provider date coverage.

The following loopback endpoints use the same configured database/archive:

| Endpoint | Input / result |
|---|---|
| `GET /api/providers` | Current mode/revision, immutable choice history, latest 20 attempts, local capability/coverage and duplicate-review links. No provider calls. |
| `POST /api/providers/config` | `{"mode":"BOTH","expected_revision":0}`. Returns the saved revision; stale edits return 409. |
| `POST /api/providers/import` | `{}` or the existing inclusive `{"from":"2026-10-01","to":"2026-10-31"}`. Returns 202 plus attempt links, including a persisted blocked result for Strava-only. 202 accepts an attempt, not proof of data availability. |
| `GET /api/provider-attempts/{id}` | Frozen mode, provider outcomes, fallback cause, Garmin report/scans and exact canonical versions available in that import. |
| `POST /api/imports/strava` | Same date syntax; returns 403 with `POLICY_BLOCKED` before credentials, network or raw writes. |

Use the local CLI without reading provider credentials:

```sh
.venv/bin/python provider_cli.py status
.venv/bin/python provider_cli.py configure GARMIN_PRIMARY_FALLBACK --revision 0
.venv/bin/python provider_cli.py sync --from 2026-10-01 --to 2026-10-31
```

Use the revision returned by `status`, and `--port 8028` before the subcommand
for a separate local demo server. The CLI connects only to `127.0.0.1` and does
not follow redirects. Garmin still uses its existing Personal API configuration;
`RUNS_GARMIN_OFFLINE=1` gives an explicit offline status. FIT remains independent.
See the [provider acceptance report](docs/specs/provider-modes-acceptance.md) for
synthetic verification and the remaining external/manual limitations.

## Read running history

Open [History](http://127.0.0.1:8001/history). Choose **Month** or **Week (Monday–Sunday)**, enter a date in the period and select **Show history**. The initial view uses the current month in Europe/London. Each activity belongs to its recorded local date, including when travelling. Undated FIT runs appear below the period totals until you confirm their dates on Imports.

For October reconciliation, open [1–31 October 2026](http://127.0.0.1:8001/history?from=2026-10-01&to=2026-10-31) and [19–25 October 2026](http://127.0.0.1:8001/history?from=2026-10-19&to=2026-10-25). The contributing runs list gives the linked Garmin IDs. Totals sum the recorded metres and seconds before rounding. Missing measurements have a known subtotal and a missing count. All runs tied at the longest unrounded distance are linked. Coverage identifies dates still to verify and links to their recheck form.

An explicitly linked recording counts once on its primary running activity’s local date. The period lists the linked running IDs dated inside it. If Garmin changes the primary to another sport, a remaining running ID supplies the date and measurements.

Open a run for its watch timer, elapsed time, timer pace, available moving measurements and recorded laps. Each available measurement names its source. Optional heart rate and GPS availability are read from the saved Garmin summaries and FIT evidence on the detail page. History and activity detail work offline with the same local database and archive.

**History JSON** opens `GET /api/history` for the displayed dates. It accepts an inclusive `from` and `to` pair, or `period=week|month` and a `date` within it. The response includes contributing activities, known totals, longest local IDs, checked ranges and gaps. The `weeks` and `months` arrays group contributing runs within the requested bounds. **Activity JSON** opens the local detail, including raw measurements, display values, metric sources, ordered laps and download warnings.

## Recover an offline FIT

On **Imports**, choose one or more exported FIT files and select **Upload FIT files**. You can do this with `RUNS_GARMIN_OFFLINE=1`. The report lists every file, including failed exports. Open an activity to see the recorded laps, watch timer and elapsed time. Uploads never contribute to Garmin date-range coverage.

A renamed FIT and the identical member of a downloaded ZIP keep the same local activity ID. A unique match on manufacturer, product, device serial, creation time and UTC session start also links recordings. A nearby start or conflicting identity appears under **Needs review**. Open the proposed activities, then choose **Link to activity** or **Keep separate**. A FIT without a recorded local offset stays undated until Garmin supplies its date or you confirm it. Decisions survive a restart and a repeated upload.

The multipart endpoint is `POST /api/imports/fit` with one or more `files` fields. The 202 response links to the saved report. To resolve a reported issue, send JSON to `POST /api/imports/{id}/issues/{issue_id}/resolve` with `{"action":"link","activity_id":1}`, `{"action":"keep_separate"}` or `{"action":"set_local_date","local_date":"2026-11-01"}`. Before upgrading a database from the first Garmin importer, stop the server and back up the database and archive together. On startup the app indexes its archived originals locally.

## Inspect an original recording

Open an activity and choose **Inspect recorded samples and events**. The view reads
its archived FIT offline and shows UTC timestamps, cumulative metres, recorded HR,
timer events and lap markers. Coverage tables identify missing fields. Choose an
original file to distinguish linked recordings or older versions, and expand
**Source and decoder provenance** for checksums and decoding settings.

Some FITs carry HR in separate messages. These have their own section and source
indices; their timing fields remain inspectable without merging readings into the
sample stream. Sample HR coverage describes only the record messages.

Sample indices start at zero in the FIT's record order. **Next samples** and
**Previous samples** preserve that order and the selected original. **Decoded
fields** exposes the SDK-decoded source values; **Recording JSON** opens the same
page of data at `GET /api/activities/{id}/recording`. The `source` query selects an
owned FIT checksum, `offset` selects the first record, and `limit` accepts 1–500
(default 200). On narrow screens, scroll tables sideways.

Missing timer events mean unknown pause history. This view does not repair traces,
assess PB eligibility or calculate fastest efforts. Session fields can differ from
the activity's Garmin summary because this view describes the selected original.

The [gate audit](docs/specs/master-v1-gate-audit.md) and
[G0 acceptance walkthrough](docs/specs/g0-acceptance.md) describe scope and checks.
Generate three made-up example files with
`python3 demo_recordings.py /tmp/marathon-g0-examples`; upload these only into a
separate demo database, as explained in the walkthrough.

## Inspect canonical trace quality

Open **Inspect canonical trace and quality** on an activity. New imports and
explicit identity/date resolutions persist a canonical version, original source
versions and versioned QC. The view shows VALID, SUSPECT, INSUFFICIENT or INVALID,
exact affected sample/event indices, calculations, timer-separated blocks,
source/header discrepancies and the original device-time evidence. Missing events
remain unknown. Separate HR streams remain separate. All distances retain the
decoder's precision in metres; elapsed, timer and moving seconds stay distinct.

Choose a historical canonical version or another owned original to compare its
evidence. The page labels which source supplies the canonical view. GET reads never
backfill, recalculate or rewrite derived data. Original bytes and earlier derived
versions remain immutable. QC is provisional engineering evidence, not scientific
validation or permission to use a PB or change training.

For already imported history, stop the app and explicitly backfill offline:

```sh
.venv/bin/python canonical.py --database /path/to/runs.sqlite3 --archive /path/to/runs-archive
```

Use `--activity 123` to process a single activity. The canonical page also offers
**Recompute offline with current QC**. `POST /api/canonical/recompute` with `{}`
backfills all saved activities; `POST /api/activities/{id}/canonical/recompute`
processes one. Omitted settings preserve each activity's selected configuration;
explicitly naming a configuration, including the default, selects it. Neither
endpoint contacts a provider. Changed settings require a new
`config_version` plus a complete `config` object (CLI: `--config-version` and
`--config /path/to/settings.json`). Existing version names cannot be redefined.

`GET /api/activities/{id}/canonical` exposes the persisted view. Optional `version`
selects a saved version ID, `source` an owned FIT checksum, and `offset`/`limit`
paginate original sample indices (`limit` 1–500). `header.end_utc` derives from the
FIT Session's start plus reported elapsed, including fractional seconds. Lap ends
use the same formula, with explicit `end_basis`. `message_timestamp_utc` is when
the summary was written and never fills a missing end. `summary.end_utc` separately
uses the activity summary's start and elapsed. Raw acquisition,
first derived-version creation and selection-history timestamps are retained
separately from activity event time; later reconstruction is not prospective proof.

Generate six made-up examples with `python3 demo_canonical.py /tmp/marathon-g1-examples`.
See the [G1 acceptance report](docs/specs/g1-acceptance.md) for the separate demo
database, exact thresholds, software checks and outstanding human comparisons.

## Verify continuous efforts

Open **Best efforts** for distinct all-time, whole-activity, internal-segment and
recent results. New imports and corrections extract efforts automatically from the
complete selected canonical trace. Configured targets retain fractional metres,
including 1609.344 m and 21097.5 m. Confirmed timer breaks split the search; interior
stationary and jogging time is retained. Flagged candidates never become PBs.

Open **Inspect continuous efforts** on an activity to choose a historical extraction
or alternate source. Each result links to exact UTC bounds, original endpoint rows,
interpolation fractions, elapsed/pace, timer coverage, pause/plateau audit and QC
flags. The excerpt is paginated; extraction always uses all original records.
**Inspect a selected interval** records an arbitrary cumulative start and target
distance for independent verification without calling it a fastest effort.

The verification page records **Accept**, **Reject** and **Annotate** with actor,
note and timestamp. Acceptance does not clear computed QC; rejection excludes the
reviewed interval from selection. **Recorded effort context** stores explicit
categories, tags and corrections separately. A race label is not maximality proof;
a verified-test declaration requires a recorded test protocol. A race's maximality
stays independent of its distance/time verification. These are recorded declarations,
not scientific validation.

**Workout labels** records planned intent, inferred completed structure and runner
corrections independently. Open it from an activity or the main navigation. The
inference uses exact configured workout-step names only where recorded laps link
to those steps; unsupported/mixed patterns stay UNKNOWN. Titles alone are hints.
Corrections retain the original evidence and require the current revision.
Upload an owned label JSON document or edit the forms; classifier mappings are
editable immutable versions. Strength remains in Context and adds no running km.

Effort verification also records separate rest/control state, explicit maximal or
nonmaximal declarations, race distance/time evidence and supporting references.
A legacy or unsupported VERIFIED_TEST/RACE label remains an unverified declaration.
ATTESTED and DOCUMENTED remain distinct; neither means independent scientific
validation. Historical context controls separate event time from knowledge time.
See [workout-label contracts](docs/specs/workout-provenance.md) and the
[acceptance record](docs/specs/workout-provenance-acceptance.md).

Recent windows use segment end inclusively in `[T−W,T]` and require the whole source
activity to end by T. The 112-day default and 56/84/168-day comparisons are provisional.
An expired PB stays all-time while recent evidence can be `MISSING_RECENT`. If a
boundary creates an unattained limiting optimum, the response reports
`REVIEW_RECENCY_LIMIT` and retains attained evidence separately. No endpoint is
shifted to make it recent. These are event-time reconstructions using currently
selected source versions, not frozen knowledge-time predictions or present capability.

Flat ranges can have eligible interior intervals even when their endpoints lack
four original records. The extractor uses a documented midpoint representative
and does not claim an earliest start in an open range. An unattained QC infimum
reports `REVIEW_ELIGIBILITY_LIMIT`; an unresolved rejected optimum reports
`REVIEW_REJECTION_LIMIT`. These diagnostics also prevent a slower activity from
being presented as the exact global best. Original records with missing timestamps
retain their QC flags wherever an interval's raw support spans them.
Limiting evidence must be approached through an eligible open interval: an endpoint
that merely touches a jump or a recent-window boundary cannot supply missing
eligibility. Rejected flat-range representatives leave other tied intervals available.
Exact rational coordinates accompany generated inspection forms so browser rounding
cannot change the reviewed interval.

For existing archives, explicitly backfill offline:

```sh
.venv/bin/python efforts.py --database /path/to/runs.sqlite3 --archive /path/to/runs-archive
```

Optional `--activity`, `--config-version` and `--config` select activities and a
complete versioned G2 configuration. `POST /api/efforts/recompute` and
`POST /api/activities/{id}/efforts/recompute` expose the same workflow. G1 QC settings
remain governed by canonical recompute. Existing configuration names are immutable.
GET requests never write or backfill. Recompute after a code update before creating
new intervals under that code; stored historical evidence remains readable.
An omitted extraction configuration preserves its current selection. Recompute also
upgrades old G0/G1 span normalization without changing selected custom QC settings.
Legacy versions show a recompute warning and are excluded from current bests until
upgraded; historical payloads and raw archives remain inspectable.

`GET /api/efforts?cutoff=<offset-aware timestamp>&days=112` returns global selection.
`GET /api/activities/{id}/efforts` accepts optional owned `version` and `source`.
`POST /api/activities/{id}/efforts/inspect` accepts `version`, `source`,
`start_distance_m` and `distance_m`. It returns a saved verification URL whose JSON
counterpart accepts `offset` and `limit` (1–500). Append `/review` or `/context` to
that JSON verification URL for the auditable POST actions. See the
[G2 acceptance report](docs/specs/g2-acceptance.md) for contracts, fixtures and checks.

Generate made-up exports with `python3 demo_efforts.py /tmp/marathon-g2-examples`;
upload them only to a separate demo database. Human Garmin comparison and scientific
validation remain unverified. Observed fastest efforts are not coaching authority.

## Record optional fueling practice

Open a recorded activity and choose **Record fueling practice and tolerance**.
Save your own strategy, practice status, timestamped notes and optional GI tolerance;
correct a saved report without losing its original revision or activity reference.
Missing practice/tolerance stays unknown and research status is `RESEARCH_PENDING`.
No intake quantity, timing, fitness or readiness recommendation is generated.
The [fueling input contract](docs/specs/fueling-inputs.md) documents JSON/CSV entry,
exact revision inspection and the read interface for later planned-session linking.

## Record wellness, feedback and strength

Open **Context** to enter wellness observations, post-run check-ins, pre-week
readiness and separate strength sessions. Sources, units, explicit rating scales,
missingness and corrections remain inspectable. Date-only observations keep their
unknown measurement time. Use **Correct this record** to retain each revision.
The forms also register new immutable input policies and rating-scale definitions.

Import owned or self-reported UTF-8 JSON/CSV in the documented
[context-export-v1 format](docs/specs/context-inputs.md), or use the offline CLI:

```sh
.venv/bin/python context_data.py --database /path/to/runs.sqlite3 import /path/to/export.json
.venv/bin/python context_data.py --database /path/to/runs.sqlite3 show
```

The same contract is available at `POST /api/context/records` and multipart
`POST /api/context/import?format=json|csv`. `GET /api/context` accepts an explicit
`as_of` timestamp; the dashboard links correction histories, source definitions and
the append-only event feed. HRV/sleep can remain missing while running calculations
continue. Strength contributes no running kilometres. No medical assessment,
recovery threshold or mileage change is performed by these input interfaces.
See the [acceptance record](docs/specs/context-ingestion-acceptance.md) for tests,
actual interface checks and unverified scientific/manual evidence.

## Record weekly availability

Open **Context → Weekly availability** to enter a Europe/London week. Keep missing
days unknown, explicitly mark unavailable days, and supply real windows plus daily
session limits. Doubles require explicit permission. Repeated autumn times need an
occurrence or offset; nonexistent spring times are rejected. Revisions preserve
original local inputs, UTC bounds and actor/time history. Overlaps count once.
Saving this calendar does not approve a target or activate a plan. See the
[API, clock policy and acceptance evidence](docs/specs/weekly-availability.md).

## Compare performance estimators

On **Best efforts**, choose **Compare performance estimators**. Select an event
cutoff and an immutable configuration. The achieved profile keeps all-time and
56/84/112/168-day observations separate. Riegel shows every source/target comparison;
critical speed shows signed D′, residuals, R², activity overlap, all eligible
distance subsets and leave-one-distance-out sensitivity. Three distances cannot
support leave-one-distance-out; four or more retain at least three per fit.

**Calculate and save evaluation** preserves the complete input ledger and result.
Open earlier results through **Saved evaluation history**. They retain the source
versions, original sample geometry, QC, context and review revisions captured at
creation. This is an event-time reconstruction using currently known evidence,
not a prediction known at the earlier cutoff. Human/scientific validation remains
unverified and personal predictive validity remains untested.

By default comparisons use the G2 winners for each window. Clear the relevant
**Use window bests** checkbox to select specific Riegel or CS sources, including
saved non-PB intervals from G2. Each CS scenario uses one observation per distinct
supported distance; save another evaluation to compare an alternative. No selection
means no observations when defaults are disabled. Missing recent evidence never
silently uses an old PB.

Expand **Create a configuration version** to change the provisional primary window,
Riegel exponent/targets, CS distance set or optional review thresholds. Blank review
thresholds remain UNKNOWN. A threshold is a review policy, not a physiological
calibration. Existing version names cannot be redefined.

`GET /api/estimators?cutoff=2026-01-15T00:00:00Z` reads the preview, available owned
source references, configurations and saved history without writing. Register a
configuration with `POST /api/estimators/configurations`, for example
`{"version":"three-trials","config":{"cs_distances_m":[400,800,5000]}}`.
Omitted configuration fields take the documented defaults, which are fully saved.
`POST /api/estimators` accepts
`{"cutoff":"2026-01-15T00:00:00Z","config_version":"three-trials"}` and returns
the `evaluation`, `url` and `html_url`. Optional `riegel_sources` and `cs_sources`
lists select exact `{activity_id,version_id,source_sha256,candidate_id}` references;
omission uses window bests, while an empty list explicitly selects none.
`GET /api/estimators/{id}` returns the immutable saved result.

Generate made-up T06 trial exports with
`python3 demo_estimators.py /tmp/marathon-g4-examples`. Import these only into a
separate demo database/archive. With the three-trials configuration and cutoff
above, the 400/800/5000 m observations yield CS 3 m/s and D′ 100 m. See the
[G4 acceptance record](docs/specs/g4-acceptance.md) for the ledger, interface checks
and outstanding manual/prospective evidence.

## Fitness profile

Open **Fitness profile** from History to inspect observed bests, recent windows, candidate critical speed, endurance, workload and recovery at a chosen event and knowledge cutoff. Comparison rules, run observations and wellness calibration are editable. Choose **Save fitness snapshot** to retain the exact sources and compare changed fields with the previous saved state.

Use `GET /api/fitness` to read the same evidence. `POST /api/fitness/capture` freezes it. An ordinary run can change the workload while a historical best and its source remain intact. The [fitness profile contract](docs/specs/g5-acceptance.md) documents versioned inputs, intervals, baselines, archive references and replay.

## Check local storage

In another terminal, run `python3 runs.py health`. It calls only `GET http://127.0.0.1:8001/health`. The server opens SQLite read-only and reads its schema. A readable database, including an empty one, returns HTTP 200 and `{"status":"ok"}`. A missing, unreadable or invalid database returns 503. The CLI exits nonzero on failure. To check health before the first import, create an empty database with `python3 -c "from pathlib import Path; Path('runs.sqlite3').touch(mode=0o600)"` in the working directory.

To run the tests, activate the virtual environment, install `requirements-dev.txt` with pip and run `python3 -m pytest -q`. The tests use synthetic activities, temporary databases and loopback HTTP. The browser replay runs with Chrome or Chromium on PATH and is required in GitHub Actions.

### Training workload and running coverage (G3)

Open **History → Workload** to inspect completed running volume over 7/28/56 days,
longest runs, spacing, missing measurements and proposed-session L30 comparisons.
Rolling windows count whole sessions ending in `(T−W,T]`. The new week tables show
FM END-week and planner START-week accounting in Europe/London; the existing
History calendar retains recorded-local-date accounting. Fractional metres remain
unrounded. Elapsed, timer and moving durations are separate.

The page provides working running-coverage, sourced zone-profile and measured
intensity forms, JSON import, corrections and revision history. Complete running
census requires explicit evidence; no runs in a list does not prove rest. Wellness
coverage does not establish running history. Calibration evidence and native
five-zone versus research three-zone methods stay visible; no physiological zone
thresholds are supplied.

`GET /api/workload` accepts absolute `as_of` and `knowledge_cutoff` timestamps.
`mode=AS_KNOWN` selects only timestamped canonical/input evidence known by the
cutoff; it is a retrospective calculation, not an original historical forecast.
The default event-time reconstruction discloses its later knowledge cutoff.
Legacy activities without timestamped canonical selections need explicit G1
recompute; reads never backdate their knowledge or mutate the database.

For L30, supply both `proposed_start` and `proposed_distance_m`. A complete 16 km
denominator and 18 km proposal yield 1.125 (+12.5%). Unknown coverage returns
INCOMPLETE_HISTORY with a null authoritative ratio; a complete empty/nonpositive
denominator returns NOT_CALCULABLE. This is exposure context, not an injury
probability or an automatic change to mileage.

`POST /api/workload/capture` freezes exact inputs and results. Historical views and
replay are available under `/workload/snapshots/{id}` and
`/api/workload/snapshots/{id}/replay`. The offline `workload.py` CLI requires `--db`
and supports `--import-json`, `--capture` and `--replay`; it makes no provider calls.
See the [G3 interface and ten-part acceptance record](docs/specs/g3-acceptance.md)
for input schemas, time/coverage policies and verification evidence.
