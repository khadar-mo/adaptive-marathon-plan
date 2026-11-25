# G0 acceptance report — 25 November 2026

## 1. Gate

**G0 — Raw activity access and sample/event inspection**, issue #13. This is the
first missing prerequisite to milestone 4 (G2), identified in the
[master gate audit](master-v1-gate-audit.md). G0 is ready for manual review; it is
not yet marked PASSED. The original ten-milestone tracker is preserved.

## 2. Existing implementation

Garmin history import, offline FIT upload, immutable raw archives, checksums,
duplicate review, UTC/local start handling, separate durations, activity detail,
recorded laps and calendar history were already implemented and previously accepted.

## 3. Changes

Added a read-only inspection view and JSON for each activity's original FIT.
It shows timestamped cumulative distance and HR, optional-channel coverage,
timer/other events, lap start/end markers and decoded source fields. It preserves
record order and zero-based source indices, including duplicates, reversals and
stationary plateaus. Linked originals/versions have distinct selectors/checksums.
Missing fields, absent/partial timer coverage, missing archives and checksum failures
are explicit. Decoding options, SDK version and inspection version accompany results.
No activity/archive data or existing schema is changed by inspection.

Standard separate FIT HR messages are also exposed, with their own indices, BPM
values (including arrays), timing fields and coverage. These remain separate from
the record stream, whose missing HR is labelled specifically as sample-record HR.

## 4. Components

- `recording.py`: source ownership, archive recovery for reading, decoding and coverage.
- `fit_data.py`: shares the existing decoder settings as a named constant.
- `app.py`, `views.py`: bounded JSON/HTML endpoints, activity link, accessible tables and pagination.
- `demo_recordings.py`: three reproducible synthetic FIT exports.
- `tests/test_recording.py`, `tests/test_recording_demo.py`, `tests/test_fit_browser.py`: regression and browser checks.
- `README.md`, this report, the audit and narrow `.gitignore` entries: usage and delivery evidence.

## 5. Requirements and test IDs

Implements Part A §14 **G0**, §3.1 raw sample/event availability, §2 explicit UTC/SI
time/distance semantics, and Data contract 01 source/code/config lineage for the
inspection output. Preserves raw inputs and absent event information under §4.1.

The fixtures provide inspection prerequisites for **T04, T18, T19, T22 and T23**:
missing channels, visible stop/start events, unknown absent-event history, stable
indices and source checksums. They do **not** establish that those full G1/G2
quality/extraction tests pass. T01–T05 and T16–T23 remain future G2 acceptance work.
No new scientific threshold or training policy is introduced.

## 6. Verification

- Baseline: 215 tests passed. Two pre-existing browser tests skipped without Chrome
  on PATH; explicitly launching the installed Chrome failed due to macOS process
  permission restrictions before either test could exercise the app.
- Local final suite: **232 passed, 3 browser tests skipped**. Run from `tests/` using
  `PYTHONPATH=.. ../.venv/bin/python -m pytest --rootdir=. -q`. Includes 17 new
  server/decoder/demo cases. Browser replay is separately required in CI.
- A deliberate sorting mutation failed the source-order regression test; the
  original code was restored. This demonstrates that the test detects reordered
  records rather than merely checking response shape.
- The running app was exercised in the Codex browser: pagination preserves indices
  and source; JSON opens; missing HR/events and summary-only files remain explicit;
  keyboard disclosure works. A 375px viewport has no page overflow; wide tables
  scroll within their own regions. No browser warning/error logs were recorded.
- The root coordinator inspected the runner's archived FITs locally: all inspected
  originals decoded with sample/timer coverage, and the database checksum stayed
  unchanged. Detailed results are private and are not committed or sent to reviewers.

Software checks establish inspection behavior. They do not validate a physiological
model or replace the runner's Garmin comparison below. CI/review results are recorded
on the pull request when complete.

## 7. Manual walkthrough

For the running demo prepared in this chat, open
`http://127.0.0.1:8012/imports/1`. These are synthetic examples in a separate database.
Open the activity beside each filename, then **Inspect recorded samples and events**.

To recreate the demo after that process stops, from the repository directory:

```sh
.venv/bin/python demo_recordings.py /tmp/marathon-g0-examples
RUNS_GARMIN_OFFLINE=1 RUNS_DB_PATH=/tmp/marathon-g0-review/runs.sqlite3 RUNS_ARCHIVE_PATH=/tmp/marathon-g0-review/archive .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8012 --no-access-log
```

Open `/imports` on port 8012 and upload the three generated FIT files. Keep these
made-up runs in the separate demo database. Then:

1. Open `sample-rich.fit`, inspect session fields, coverage, four timer events and
   two laps. Use Next/Previous samples. Expand a sample's decoded fields and compare
   its timestamp/distance/HR with the visible row and Recording JSON.
2. Look at records 180–210 (use `?offset=175&limit=40#samples` on the inspection URL).
   Distance stays at 540 m while timestamps continue through the stop.
3. Open `no-timer-events.fit`: HR and timer events are explicitly not provided.
4. Open `summary-only.fit`: the 2,000 m summary remains available with no samples.
5. For actual Garmin acceptance, use the app serving your existing saved history.
   Restart that process from this branch if it was started without reload. Open
   several real runs and their **Inspect recorded samples and events** links.
   Compare original FIT session start/end, distance, elapsed/timer times, available
   HR and lap/event markers with the exported source and Garmin Connect. Record
   which channels are missing; include a known pause if available. Convert UTC to
   the original offset when comparing Garmin's local clock. The raw activity/time
   fields remain available in the session disclosure. Report discrepancies privately.

## 8. Expected results

| Synthetic file | Expected inspection |
|---|---|
| sample-rich.fit | 401 records; start 2026-01-10 09:00:00 UTC; end 09:06:40; 1,110 m; elapsed 400 s; timer 370 s; HR 140–149 bpm; timer start at 09:00, stop_all 09:03, start 09:03:30, stop_all 09:06:40; two laps (540 m / 210 s elapsed / 180 s timer, 570 m / 190 s / 190 s); missing GPS/temperature/moving time explicit |
| no-timer-events.fit | 205 records; 612 m; 204 s elapsed/timer; timestamp/distance recorded; HR, laps and timer events not provided; no claim of uninterrupted movement |
| summary-only.fit | 2,000 m and 600 s summary; zero samples; no invented HR, trace or events |

For real files, values must reproduce that original's decoded data. Garmin's edited
summary or another linked recording can legitimately differ; the selected checksum
must identify which original is being inspected. Missing data is not zero. A recorded
timer channel is not proof that every stop was captured.

## 9. Limitations and unresolved work

Only the existing supported single-session running FIT path is covered. The Strava
CLI exists, but persisted Strava trace ingestion is not implemented or required here.
The view decodes the original on each request; sample output is paginated, not the
underlying decoder workload. SDK scaling applies; raw bytes are still retained in
the archive. Decoded record, HR, event, lap, session, file/device and time-correlation
fields are inspectable; other FIT message groups are not exposed by this view.
Records are not corrected,
resampled or assigned a G1 quality state or a claimed B/C capability tier.

G1 canonical trace/event storage and versioned QC remain incomplete. G2 fastest
continuous-effort extraction has not begun. No gate is accepted from software tests
alone. The local Chrome launch restriction remains distinct from browser checks
performed through Codex and the required CI replay.

## 10. Recommendation and stop point

**Recommend accepting G0 once the manual original-file comparison passes.** Until
then its status is PARTIAL (implementation delivered; human acceptance pending).
Leave the pull request open for that result. The coordinator instructions supplied
with this task explicitly require stopping after this report and waiting for
acceptance or corrections. After acceptance, re-audit and complete G1 before G2.
