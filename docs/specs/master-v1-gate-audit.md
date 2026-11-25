# Master specification gate audit — 25 November 2026

Audit baseline: `2c526958131d49e54207b3bc368d54cac664d439` (`main`).

Authority: *Marathon Coach Developer Master Implementation Specification v1.0*,
Parts 1–4 and A–C, supplied 25 November 2026. Source DOCX SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
The earlier *Adaptive Planning Engine Specification.pages* (16 November 2026)
supplies the functional context; the master document controls technical details.
This report records implementation evidence, not changes to either specification.

## Existing implementation

The Python application uses one loopback FastAPI process, SQLite and an immutable,
checksummed local archive. Garmin list/detail/original imports, resumable date-range
imports, offline FIT upload, duplicate review, recorded laps, and week/month history
are implemented. Imports retain raw bytes and distinguish elapsed, timer and moving
time. The Garmin FIT SDK already decodes records/events, but the application exposes
only summaries, laps and limited sensor availability. `runs.py` also implements
Strava REST authentication and recent-activity listing, with provider tests; it
does not persist Strava activity traces into the history/FIT pipeline. There is
no segment extractor, fitness snapshot producer or planning engine.

Existing evidence includes merged PRs #8–#12, closed issues #2–#7, automated tests,
and the runner's earlier Garmin comparisons. Private history and comparison records
remain outside the repository. That acceptance supports the existing import/history
features; it does not establish compliance with the new granular gates.

## Gate status before this change

`PASSED` requires the gate's acceptance evidence, not merely passing software tests.

| Gate | Status | Evidence | Missing work | Blockers |
|---|---|---|---|---|
| G0 — Raw access and inspection | PARTIAL | FIT upload, original archive/checksums, SDK decoding, lap detail; `fit_data.py`, `fit_import.py`, `tests/test_fit_import.py` | Inspect individual timestamp/distance/HR records and timer/lap events; report channel coverage and absent fields against actual files | None: exported FIT is sufficient under Part A §3.1/§14 |
| G1 — Canonical activities and QC | PARTIAL | Stable local/source identities, UTC start, separate durations, dedup review, raw provenance; `storage.py`, `fit_store.py`, concurrency/import tests | Canonical samples/events and end timestamps; versioned QC states, exact affected ranges and stop-state handling; complete G1 manual comparison | G0 inspection acceptance; full QC work remains |
| G2 — Fastest continuous efforts | NOT_STARTED | No extraction or segment-verification component | Part A §§4–6 algorithm, all-time/recent bests, source brackets, pause/plateau audit, T01–T05 and T16–T23 | G0/G1 incomplete |
| G3 — Training history/workload | PARTIAL | Local-date calendar week/month totals, coverage gaps; `history.py`, `tests/test_history.py` | Specified session-end rolling 7/28/56-day windows, preceding 30-day maximum, spacing and complete provenance/missingness | Canonical end timestamps and G1 acceptance |
| G4 — Candidate estimators | NOT_STARTED | No estimator implementation | Direct bests, Riegel comparator and CS/D′ with input ledger and synthetic checks | G2/G3 evidence |
| G5 — Contextual state | NOT_STARTED | Activity HR/GPS presence only; no contextual model | Separate capability, endurance, recovery and uncertainty evidence | Accepted fitness inputs; wellness/feedback where available |
| G6 — Validation/snapshots | NOT_STARTED | No snapshot or prediction ledger | Reproducible as-of snapshots, time-ordered validation and prediction errors | G4/G5 candidates and contracts |
| P0 — FitnessSnapshot adapter | NOT_STARTED | No adapter or snapshot consumer | Master cross-engine contract, availability/quality checks and provenance | FitnessSnapshot producer |
| P1 — Starting volume approval | NOT_STARTED | No volume proposals/approvals | History-based baseline proposal and recorded approval | P0 and usable history |
| P2 — Weekly progression | NOT_STARTED | No progression decisions | Versioned progression policy and history decisions | P1 and approved targets |
| P3 — Workout candidates | NOT_STARTED | Source sport tags are displayed; no workout classification/selection | Long-run/quality/easy candidates with exact distance and evidence | P0–P2 |
| P4 — Flexible scheduling | NOT_STARTED | No availability or schedule model | Exact approved mileage preservation, feasible placement or explicit CONFLICT | Approved targets and P3 candidates |
| P5 — Recovery/S&C | NOT_STARTED | No feedback or S&C workflow | Structural adaptation, explicit mileage-reduction approval and separate S&C | P4 and contextual inputs |
| P6 — Taper/fueling/forecast | NOT_STARTED | No forecasting implementation | Approved taper policy, data-only fueling stub, labelled experimental golden path | Earlier planning gates and validated input contracts |
| P7 — Audit/outcomes | NOT_STARTED | Import audit exists, but no planning audit | Immutable approvals/revisions, validation and outcome tracking | Planning decisions and outcome data |

## Mapping to the existing ten milestones

The original tracker is preserved. Its first three passed milestones cover the
delivered MVP; their narrower scope must not be equated with full G0/G1/G3 compliance.

| Existing milestone | Reported status | Verified implementation and specification mapping |
|---|---|---|
| 1. Garmin connection | Passed | Implemented and previously accepted; supplies part of G0. Live API is optional for initial G0–G2. |
| 2. Historical import | Passed | Implemented and previously accepted; supplies raw archives and part of G1/G3. |
| 3. Activity dashboard | Passed | Implemented and previously accepted; summary/lap views and calendar history supply part of G0/G3. |
| 4. Fastest-effort extraction | Not started | G2; prerequisite corrections are G0 inspection followed by G1 canonical trace/QC. |
| 5. Fitness model | Not started | G3–G6, retaining distinct observed and estimated states. |
| 6. Workout classification | Not started | Observed effort context and later P3 workout taxonomy; provider sport tags alone do not complete it. |
| 7. Planning engine | Not started | P0–P3. |
| 8. Flexible scheduling | Not started | P4. |
| 9. Adaptive coaching | Not started | P5 and P7. |
| 10. Golden-path forecasting | Not started | P6, dependent on fitness evidence and G6 validation. |

## Next deliverable

Complete **G0 — Raw activity access and sample/event inspection** first. Reuse the
existing importer/archive and add a read-only activity inspection page and JSON.
Show source-specific raw decoded records in their recorded order, UTC timestamps,
distance, HR, timer/events, lap markers, field coverage and missing-data notices.
Keep linked recordings/versions distinct and expose checksums and decoder/version
provenance. An absent event stream must remain unknown, not evidence of no pauses.

This is the smallest missing prerequisite that can be checked independently. No
schema rebuild, new connector, PB extraction, estimator or training policy is needed.
G1's QC and G2's algorithms/tests remain separate future deliverables.

## Baseline verification

On 25 November: 215 existing tests passed; the two browser tests initially skipped
because Chrome was not on PATH. An explicit replay with installed Chrome then failed
at browser launch: macOS denied the process's required services. This predates the
change. The affected new flows were checked in the Codex browser; the acceptance
report records final local/CI evidence separately.

Tests are collected from `tests/` with `--rootdir=.` and `PYTHONPATH=..` so pytest
does not traverse the private repository `.env`. No credential-file access is needed.

The supplied coordinator workflow requires a ten-item acceptance report and manual
acceptance after each gate. G0 remains pending that acceptance even after its software
checks pass. G1 starts only after explicit acceptance or requested corrections.
