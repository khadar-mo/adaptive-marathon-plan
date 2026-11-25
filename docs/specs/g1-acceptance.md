# G1 acceptance report — 25 November 2026

## 1. Gate and authority

G1, issue #16, extends merged G0 (PR #14). Requirements come from master v1.0
Part 3 §3.1, Part A §§2–4.3 and §14. Source DOCX SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
This report distinguishes software evidence from human and scientific validation.
The latter remain **UNVERIFIED**; no real/private FIT data is included.

## 2. Existing implementation reused

The existing importer, immutable checksummed archive, source ownership, duplicate
review/redirects and SQLite connection serialization remain authoritative.
Canonical writes run inside the import/resolution transaction. Explicit offline
backfill uses the existing archive index. GET views never backfill or rewrite data.
Only additive tables are introduced; existing local IDs, summaries, reports and
raw bytes are preserved, including first-importer database upgrades.

## 3. Delivered behavior

Imports persist source-specific decoded samples, events, laps, separate HR streams
and original device-time evidence. Canonical versions identify their selected
source, all provider/raw input IDs, decoder/options, normalization/QC/config versions,
recorded UTC session bounds, precise SI metres and distinct durations. Summary-only
activities remain available with INSUFFICIENT trace quality.

The inspection page and JSON expose exact raw indices, timestamps, references and
calculations for gaps, plateaus, bad clocks/distances, speed and total disagreement.
Known timer stop/start boundaries split blocks even if distance rises. Unresolved
stationarity is flagged/quarantined without erasing elapsed time or inventing a stop.
Historical versions and alternate owned sources remain selectable. Repeating the
same inputs/config is idempotent; a new config, parser or source produces new proof.

## 4. Components and stable interfaces

- `canonical.py`: persisted sources, QC/config versions, canonical versions/heads,
  append-only selections, offline CLI and read API.
- `canonical_qc.py`: deterministic source checks and timer-separated block ranges.
- `storage.py`: transactional import/resolution integration; existing locking retained.
- `recording.py`: shared ownership inventory; G0 read behavior remains intact.
- `app.py` / `views.py`: canonical JSON/HTML and explicit recompute endpoints.
- `demo_canonical.py`: six made-up FITs; `tests/test_canonical.py` and browser replay.

`GET /api/activities/{id}/canonical` returns `version_id`, `sources`,
`selected_source_version_id`, `header`, `summary`, `qc`, and
`canonical_quality_state`. `source` and `version` are bounded owned selectors;
`offset`/`limit` paginate original records. `qc.flags` include exact `sample_indices`,
`sample_times_utc`, `event_indices`, `raw_references` and `calculation`.
`qc.blocks` retain original sample indices and explicit left/right event boundaries.
Records outside known running timer ranges remain in the trace and are listed as
`unassigned_sample_indices`. No source sample is sorted, filled, clamped or erased.

`header.end_utc` is the selected source's recorded session end. A separately labelled
`summary.end_utc` can be derived from reported elapsed time; its `end_basis` makes
that distinction explicit. Source acquisition (`raw_sources[].acquired_at`), first
derived creation (`created_at` / `source_created_at`), and head selection
(`selection_history[].selected_at`) are separate from activity event timestamps.
Historical donor IDs are retained after explicit dedup merges. These timestamps
support later as-of work; G1 does not claim a prospective validation ledger exists.

## 5. Versioned engineering decisions

`g1-engineering-v1` stores exactly: gap 30 s, stationary review 10 s,
stationary quarantine 30 s, endpoint bracket 15 s, plausible adjacent speed
12 m/s, summary tolerance max(100 m, 1%), and minimum candidate samples 4.
Gaps/speeds/discrepancies trigger on **greater than**; stationary thresholds trigger
on **greater than or equal**. Trace/header discrepancy uses the device-header total
as the percentage base; provider/FIT-header comparison uses the displayed provider
summary as its base. All comparisons retain their inputs and calculated tolerance.
Raw decimal metres are retained; integer-metre planning is a later boundary.

Endpoint brackets and four-sample candidate minimum are stored but **not applied**
as activity rejection. G2 will apply them to individual efforts. VALID is engineering
QC, never proof that every stop was recorded. Missing timer events alone are INFO;
partial/uninterpretable event history requires review. Invalid sample clocks/order
or reversing/negative distance are INVALID. Missing required trace is INSUFFICIENT.
Other unresolved quality anomalies are SUSPECT. Confirmed stops can explain gaps
or fully covered plateaus without making a cross-stop effort eligible.

Source selection reuses G0: primary Garmin original first, otherwise owned FIT
checksum order. The UI names that source and distinguishes an alternate inspected
source. It does not choose an original by whichever produces a faster result.
FIT SDK units are fixed by the supported FIT field contract; no mixed-unit/custom
trace upload or GPS-reconstructed distance is introduced. No host zone is substituted.

## 6. Acceptance criteria and proof

Issue acceptance is reproduced verbatim below; AC labels are local report labels.

**AC-1:** Add deterministic, persisted canonical activity versions with stable canonical activity ID, source/provider IDs, selected raw checksum(s), parser/algorithm/config versions, UTC start/end, SI distances and separate elapsed/timer/moving durations. Preserve raw FIT sample/event indices and original device-time evidence; no host-zone substitution. Missing trace retains valid summary with INSUFFICIENT, never invented samples. Expose a public API and useful activity inspection UI.

Pass: `test_import_persists_canonical_precision_raw_provenance_and_read_only_pages`,
`test_relative_clock_is_invalid_not_host_timezone`, summary-only tests, and browser flow.

**AC-2:** Keep source-specific sample/event versions, existing duplicate links and historical versions; record which source version supplies a canonical view. New config/source version creates new derived provenance without overwriting raw data or historical derived output. Repeated processing of identical inputs/config is idempotent. No migrations or data loss for existing users; GET inspection should not mutate storage. Integrate canonicalization into imports and provide explicit offline backfill/recompute for already archived history.

Pass: config/parser/recompute, linked-old-source/restart, explicit-merge,
existing-imported-database and first-importer backfill tests. Existing tables are
not rebuilt by G1; the importer's pre-existing legacy migration is reused when needed.

**AC-3:** Version qc_config with the exact provisional engineering defaults: max_sample_gap_s=30; stationary_review_s=10; stationary_quarantine_s=30; endpoint_bracket_max_gap_s=15; plausible_speed_max_mps=12; summary tolerance=max(100m,1%); min_candidate_samples=4. Do not apply candidate-only endpoint/min-sample rules as arbitrary activity rejection. Retain all timing and cumulative-distance values; never sort/erase/clamp anomalies silently.

Pass: default-config assertion and parametrized gap, plateau, speed, tolerance,
candidate-only and raw-order tests.

**AC-4:** Identify invalid/relative clocks, duplicate/nonmonotonic timestamps, negative/reversing cumulative distance, absent/partial fields, gaps, stationary plateaus, implausible adjacent speed and trace/header disagreement. Emit reviewable flags with exact affected indices/times/raw references and calculations. Quality states VALID/SUSPECT/INSUFFICIENT/INVALID must be meaningful and deterministic; missing timer events alone do not reject an otherwise clean trace.

Pass: clock/distance/missing-field, negative-defense, relative-clock and threshold
tests. FIT distance is unsigned; the negative-value defensive check uses a decoded
fixture directly rather than claiming the standard FIT encoder can encode it.

**AC-5:** Preserve timer-event coverage RECORDED/NOT_PROVIDED/PARTIAL. Confirmed pause/stop/resume boundaries terminate eligible continuous blocks even if recorded distance rises. Do not bridge a confirmed pause or mark ambiguous/partial events as proof of continuity. Unknown stationary cause remains quarantined/flagged, not a fabricated stop. Preserve separate raw HR streams without inventing alignment.

Pass: confirmed-stop/rising-distance, confirmed-plateau, partial-event, absent-event
and separate-HR assertions. G2 candidate eligibility is not claimed.

**AC-6:** Tests through real import/recompute/API entry points: missing and relative timestamps; timestamp/distance anomalies; 29/30/31s gaps; 10/30s plateau boundaries; 12/12.1m/s; tolerance boundary; confirmed stops; absent/partial events; unchanged raw checksums; repeated processing; config/source changes; duplicate and older source versions; restart/concurrency; existing imported DB migration. Map to Part A T04/T15/T18–T21/T23 prerequisites honestly; leave G2 candidate/extraction assertions to G2.

Pass for G1 software prerequisites: 52 new API/QC/CLI cases plus browser replay.
T04 autopause/missing timer state, T15 updated distance with new provenance,
T18 confirmed pause, T19 absent events with ambiguous stationarity,
T20 31-second gaps/12.1 m/s jumps, T21 distinct QC configuration versions, and
T23 a clean trace with informational absent-event coverage are prerequisites only. Exact endpoint/candidate and
best-effort assertions remain G2; no complete T04/T15/T18–T21/T23 gate pass is implied.

**AC-7:** Provide a ten-item acceptance report and synthetic demo/inspection view. Update the audit to distinguish software-merged state from outstanding human/scientific validation and record the user's explicit decision to continue without per-gate development pauses.

Pass: this report, `demo_canonical.py`, inspection UI and updated audit.

## 7. Commands and observed verification

- From `tests/`: `PYTHONPATH=.. ../.venv/bin/python -m pytest --rootdir=. -q`:
  **284 passed, 4 browser tests skipped** without Chrome on PATH.
- `test_canonical.py`: **52 passed** through synthetic import/recompute/API and CLI.
- Deliberately suppressing the 31-second gap flag and removing confirmed stop
  recognition each caused their public-interface tests to fail; source was restored.
- Native Chrome launch succeeded in an isolated 15-second probe, but its replay
  stalled during page creation before app assertions. The replay was terminated
  at an explicit 110-second host timeout. No lingering replay is relied on as proof.
- Codex Chrome inspection: 1280px and 375px layouts had no page overflow;
  keyboard pagination retained original record 200, provenance and QC disclosures
  opened, Canonical JSON opened, recompute returned the same saved version, and
  the 30-second unknown plateau showed indices 1–2 with quarantine and raw checksums.
  Summary-only data showed INSUFFICIENT with no invented records. No console errors.
- CI must run all four browser replays on Linux. Final CI and independent review
  results are recorded in the linked pull request before authorized merge.

## 8. Reproducible synthetic walkthrough

Use a fresh, separate directory and a free port; never upload demo runs into real history:

```sh
.venv/bin/python demo_canonical.py /tmp/marathon-g1-examples
RUNS_GARMIN_OFFLINE=1 RUNS_DB_PATH=/tmp/marathon-g1-review/runs.sqlite3 RUNS_ARCHIVE_PATH=/tmp/marathon-g1-review/archive .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8014 --no-access-log
```

Open Imports on that port, upload the six exports, open an activity and choose
**Inspect canonical trace and quality**. Inspect the source/header, QC disclosure,
blocks and JSON; try the source/version selectors and offline recompute.

| File | Expected result |
|---|---|
| sample-rich.fit | VALID; 401 records; 1,110 m; elapsed 400 s, timer 370 s; confirmed 180–210 s stop; two blocks |
| no-timer-events.fit | VALID; events NOT_PROVIDED; no claim that all stops are known |
| summary-only.fit | INSUFFICIENT; 2,000 m / 600 s summary and zero samples |
| gap-31s.fit | SUSPECT; records 1–2; 31 s gap against 30 s threshold |
| unknown-30s-plateau.fit | SUSPECT; records 1–2; 30 s quarantined plateau, no invented stop |
| distance-reversal.fit | INVALID; records 1–2; cumulative 3 m → 2 m retained |

## 9. Unverified evidence and limitations

Human comparison of 10–20 actual Garmin activities, dates/distances/durations and
known pauses remains **UNVERIFIED**, as does scientific calibration of every
engineering threshold. Keep detailed real-file comparisons private. This software
report cannot accept those gates on the runner's behalf. Runtime athlete approvals
remain unchanged. No PB, estimator, training decision or physiological claim is added.

Source records are persisted as versioned JSON; output pagination does not make
decoding/recompute streaming. G1 supports the existing single-session running FIT
path and Garmin summary lineage. There is no generic GPX/TCX/Strava trace importer,
QC manual-correction editor, aligned separate HR stream, or G2 effort extraction.
Archive integrity is checked again on reads; cached historical data is explicitly
labelled unavailable when original evidence is missing/corrupt.

## 10. Development authorization and next gate

The user explicitly authorized continued development and each in-scope merge after
tests, CI and independent review pass, superseding the earlier per-gate development
pause. Software delivery may continue with human/scientific validation honestly
unverified. The next software gate is G2 (#17), consuming source/QC versions and
raw-index block/flag evidence. Merge does not deploy, release, accept a physiological
model, change the marathon objective, or supply an athlete approval.
