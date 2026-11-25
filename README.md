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
**Recompute offline with default QC**. `POST /api/canonical/recompute` with `{}`
backfills all saved activities; `POST /api/activities/{id}/canonical/recompute`
processes one. Neither contacts a provider. Changed settings require a new
`config_version` plus a complete `config` object (CLI: `--config-version` and
`--config /path/to/settings.json`). Existing version names cannot be redefined.

`GET /api/activities/{id}/canonical` exposes the persisted view. Optional `version`
selects a saved version ID, `source` an owned FIT checksum, and `offset`/`limit`
paginate original sample indices (`limit` 1–500). `header.end_utc` is the selected
FIT session's recorded UTC end, when present. `summary.end_utc` may instead be
derived from reported elapsed time and has an explicit `end_basis`. Raw acquisition,
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
an explicit maximal declaration requires a recorded test protocol and remains a
user attestation, not scientific validation.

Recent windows use segment end inclusively in `[T−W,T]` and require the whole source
activity to end by T. The 112-day default and 56/84/168-day comparisons are provisional.
An expired PB stays all-time while recent evidence can be `MISSING_RECENT`. If a
boundary creates an unattained limiting optimum, the response reports
`REVIEW_RECENCY_LIMIT` and retains attained evidence separately. No endpoint is
shifted to make it recent. These are event-time reconstructions using currently
selected source versions, not frozen knowledge-time predictions or present capability.

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

## Check local storage

In another terminal, run `python3 runs.py health`. It calls only `GET http://127.0.0.1:8001/health`. The server opens SQLite read-only and reads its schema. A readable database, including an empty one, returns HTTP 200 and `{"status":"ok"}`. A missing, unreadable or invalid database returns 503. The CLI exits nonzero on failure. To check health before the first import, create an empty database with `python3 -c "from pathlib import Path; Path('runs.sqlite3').touch(mode=0o600)"` in the working directory.

To run the tests, activate the virtual environment, install `requirements-dev.txt` with pip and run `python3 -m pytest -q`. The tests use synthetic activities, temporary databases and loopback HTTP. The browser replay runs with Chrome or Chromium on PATH and is required in GitHub Actions.
