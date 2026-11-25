# Context ingestion acceptance — issue #29

## 1. Requirements and authority

Authority: master v1.0 Part 2 §§2.1/2.3, Part 3 §3.1, Part A §§3/10/12,
Part B §§3.2/9.1; source SHA-256
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
Issue #29 follows merged G1 #16 / PR #34, base
`1319e64fad8d8cdda8f54724f144151fc624e493`.

The issue's acceptance wording is reproduced exactly:

> Acceptance: T13 demonstrates a valid recorded run with missing HRV/sleep still has working workout computations and a missing wellness state. Add synthetic import/entry tests for optional fields, multiple daily observations, duplicate/corrected records, UTC/local-date/DST boundaries, unknown measurement time, incompatible scales/units, decimal preservation, missing reports and source coverage, pain attention signaling, and S&C isolation from running totals. Exercise T14/T15-style cutoff and correction invariants without claiming the full G6 evaluation harness is complete. Inspect actual entry/import interfaces and the missingness/history displays.

The runtime-input requirement is also explicit:

> Runtime input completeness: provide working editable/importable UI and API paths for the inputs and configuration this issue owns, with validation, persisted changes and an inspected end-to-end update. A permanent placeholder-only UNKNOWN field is not implementation of a specified input capability. UNKNOWN/MISSING is appropriate for genuinely absent or unsupported evidence; undefined physiological policies remain explicit versioned configuration with REVIEW and honest limitations. Do not manufacture measurements or numerical scientific defaults to remove an unknown state.

## 2. Scope and boundaries

Delivered: wellness observations, versioned post-run/pre-week reports, separate
strength sessions, explicit source coverage, scale/policy registration, correction
history and stable context events. The documented owned-export/self-report JSON/CSV
schema is the supported source. Proprietary Garmin layouts and a live wellness API
are not claimed. All fixtures are synthetic; no credentials, private history or real
FITs were used. Baseline medians/deltas, clinical interpretation, G6 predictions and
planner dose/approval decisions remain downstream responsibilities.

## 3. Changes and reused interfaces

`context_data.py` owns append-only input versions/receipts/events, exact decimal and
time validation, configurations, cutoff reads, compatible daily observation groups,
target redirects and the CLI. `context_web.py` owns API routes and entry/correction,
missingness/history and configuration forms. Additive schema registration reuses
`Store.connect`, SQLite serialization and the existing private checksum archive.
`app.py` includes the router and the shared navigation links Context. Existing
importer, dashboard, raw archive and Strava CLI behavior remain covered by the full
suite. See [the contract and decisions](context-inputs.md).

## 4. Synthetic fixtures and acceptance mapping

`tests/test_context.py` supplies made-up export/report/strength data. Its public
API/CLI tests cover the following acceptance clauses:

| Criterion | Result and evidence |
|---|---|
| T13: missing HRV/sleep with a valid run | Pass: `test_t13_missing_hrv_sleep_does_not_block_running_computations`; canonical trace remains VALID and activity distance/elapsed computations remain available, wellness and target check-in remain MISSING. Full downstream effort/fitness integration is deferred until its modules merge. |
| Optional inputs, validity, device output labels and missing reasons | Pass: `test_missing_observations_device_outputs_and_validity`, invalid-input parameter cases, strength test and explicit missing-report test. |
| Multiple observations, incompatible units/scales and decimal preservation | Pass: `test_multiple_daily_observations_units_methods_and_versioned_aggregation`, `test_precision_raw_archive_unknown_time_idempotence_and_read_only`, `test_scales_comments_pain_attention_and_missing_reports`. |
| UTC/local-date/DST, unknown measurement time and future evidence | Pass: `test_dst_utc_local_boundaries`, `test_sunday_readiness_target_is_not_event_time_and_current_day_not_complete`, `test_future_definition_unavailable_at_cutoff`. |
| Duplicate/corrected records, immutable source lineage, T14/T15-style cutoffs | Pass: `test_t14_t15_correction_cutoff_and_old_import_replay`, event-log cursor test, concurrent duplicate/correction test, donor-merge and alias sport/date tests. |
| Missing reports/source coverage and pain attention | Pass: explicit-missing report, coverage evidence/complete-day tests, coverage-intersection test, and three explicit pain/worsening/illness attention flags. |
| S&C isolation and later labels | Pass: `test_strength_isolation_and_later_soreness_availability`; 3600.25 s strength entry leaves running history identical, retains exact exercise load and excludes later soreness from earlier evidence. |
| Actual imports/entry/history and configurable inputs | Pass: multipart JSON and CSV API tests, nested CSV strength fields, standalone CLI test, actual CUA forms and `tests/test_context_browser.py` replay. Final CI proof is recorded below. |

## 5. Test results and regression strength

Repository check: from `tests/`, run
`PYTHONPATH=.. ../.venv/bin/python -m pytest --rootdir=.. -q`.
The local environment has its own `.venv`. No shared checkout or package environment
is modified. Five browser flows are skipped locally because Chrome is not on the
worktree test PATH; CI requires Chrome and executes them. There is no separate
repository lint/typecheck command.

Two temporary production-code mutations were run and restored:

- Removing the known-at predicate made
  `test_t14_t15_correction_cutoff_and_old_import_replay` fail its frozen-view
  equality assertion (one failed test, 0.55 s).
- Rounding decimal output through binary float made
  `test_precision_raw_archive_unknown_time_idempotence_and_read_only` fail the exact
  value assertion: `48.12345678901234567890` became `48.123456789012344`
  (one failed test, 0.41 s).

Restored final local suite: **323 passed, 5 skipped** (the five browser replays),
19.96 s. CI and independent review are recorded in item 10.

## 6. Actual API and CLI inspection

An isolated loopback Uvicorn process with a temporary synthetic SQLite/archive
accepted a made-up FIT through the existing upload API (202; report complete).
`GET /api/context` returned MISSING HRV before any contextual data. Public tests
call the real ASGI routes for JSON and multipart imports, validation/conflict
responses, correction/history, missing target reports, definitions and events.
The CLI test launches separate Python processes to import a file and inspect the
persisted database after restart; no provider/settings load is needed.

The existing loopback origin guard also rejects a cross-origin contextual POST.
Exact source bytes, checksum references and 0600 archive permissions are checked.
Read-only views leave the SQLite bytes unchanged. Invalid batches roll back without
partial context versions; concurrent writes produce one revision and one conflict.

## 7. Actual UI inspection

CUA exercised the local synthetic app in Chrome at desktop 1280×900 and mobile
375×812. It saved exact wellness data, corrected it with both revisions visible,
saved a post-run RPE/pain report, entered Sunday readiness for the next Monday,
saved a 3600.25 s strength session, registered a median policy and custom symptom
scale, then applied that scale in a readiness correction. Mobile keyboard actions
saved an explicitly MISSING HRV record and a complete-date coverage attestation.

The same browser cutoff form restored the original wellness revision and excluded
later feedback/strength. Observed labels, source units, unknown measurement time,
known-at/correction times, missingness and attention state were visible. Console
error/warning log was empty. Final document width equalled viewport width at both
breakpoints. Browser inspection found and fixed blank-filter validation, accessible
labels that included help/option text, and import-grid mobile overflow. Viewport
was reset and only the issue's own synthetic tab/server were manipulated.

CUA's file chooser could not attach the local CSV because the Chrome extension's
file-URL permission is disabled. No browser permissions were changed. Actual HTTP
multipart import passes; the mandatory Linux CI browser test performs real file
selection/upload plus every entry/configuration form, correction history, keyboard
submission, console checks and both viewport widths.

## 8. Lineage, replay and temporal contract

Each input retains raw receipt checksum/index, parser/algorithm/policy version,
original source time, server-assigned first-known time and version/correction time.
The event feed includes each immutable revision and stable cursor/ID. Replaying old
bytes cannot replace a newer correction. Units/scales remain separate; the optional
median is within compatible groups only. Date-only rows expose UNKNOWN_INSTANT,
not fabricated UTC times. Current partial days do not establish complete coverage.

Report targets are independent of report timestamps. Sunday readiness can target
Monday. Donor redirects and Garmin alias reclassification retain original entry
target/version; `target_now` is explicitly a current display aid. It is not allowed
as historical snapshot evidence. G6 remains responsible for the full frozen
prediction/evaluation contract.

## 9. Manual and scientific limitations

Real Garmin wellness export mapping, 10–20 activity comparisons, physiology and
prospective individual validation remain **UNVERIFIED**. Passing synthetic checks
does not make device readiness/VO2max outputs physiological thresholds, prove a
runner recovered, validate S&C interference windows or authorize mileage changes.
No completed weekly strength session is invented from the approximate baseline.
Exercise choice and actual load remain runner-owned. No deployment/release occurs.

## 10. Final independent review and CI

Final local-suite, PR, independent-review verdict and CI evidence will be attached
here after the final code is frozen. Merge remains gated on independent approval,
passing latest-head CI and root's serialized integration with current main. The user
has already authorized development/merge; runtime athlete approvals are unchanged.
