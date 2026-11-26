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

## Current software delivery and next work

The table above is the original audit baseline, not the current software state.
G0 software merged in PR #14 (issue #13), with 235 CI tests and independent review.
It exposes raw samples, separate HR, events, laps and decoder/source provenance.
The runner's detailed original-file comparison is still separate human evidence.

G1 software is delivered by issue #16 and its linked PR: additive, persisted
canonical/source/QC versions, exact source ranges, explicit offline recomputation,
timer-separated blocks, a public API and inspection UI. See
[the ten-item G1 report](g1-acceptance.md) for acceptance/test mappings, CI/review
proof and unverified comparisons. Source activity event time, raw acquisition time,
first derivation time and selection/correction availability are distinct. G1 has
no fastest-effort extractor and no physiological or planning authority.

G2 software is implemented by #17: exact first-arrival/last-departure continuous
efforts, candidate-local QC, immutable extraction lineage, all-time/recent selection,
raw endpoint verification and auditable review/context entry. See the
[ten-item G2 report](g2-acceptance.md) for normative tests, independent arithmetic,
real HTTP/browser evidence and final review/CI requirements. Whole-activity evidence
uses original samples, never silently substituted summary elapsed. Non-attained
recency, rejection and open-QC optima remain explicit review states with separate
attained evidence. Review regressions cover untimed original-record support,
decimal plateau identity after rejection, absolute-start ties across timer blocks,
and attained representatives in flat open cells without an earliest-start claim.
Limits now carry direction-specific open-cell QC and exact recency support.
Repeated rejected representatives, microsecond review-identity bins and exact
coordinate form round-trips have causal regressions and an independent QC-cell oracle.
Legacy member/lap indexing, canonical backfill and requested G2 extraction now share
one SQLite transaction; public single/mixed-batch regressions and a real HTTP retry
verify no committed heads, configuration or selections after extraction failure.
Garmin's summary-first/summary-last compatibility guidance is now reflected in G0/G1
v2: Session/Lap ends derive from start plus reported elapsed; written clocks remain
separate. Synthetic span/cutoff/conflict and upgrade regressions preserve raw data,
custom settings and immutable old versions. Legacy normalization is visibly marked
for explicit offline recompute and excluded from current G2 bests until upgraded.
The [dated G1 correction](g1-acceptance.md#4-components-and-stable-interfaces)
supersedes earlier G0/G1 wording that treated a message-written clock as a span end.

**Human/scientific validation for G0/G1/G2 remains UNVERIFIED.** Software merge is
not a claim that those validation gates passed. G3 workload, G4 candidate estimators
and #30 provenance classification follow the G2 software merge and preserve that status.

## Baseline verification

On 25 November: 215 existing tests passed; the two browser tests initially skipped
because Chrome was not on PATH. An explicit replay with installed Chrome then failed
at browser launch: macOS denied the process's required services. This predates the
change. The affected new flows were checked in the Codex browser; the acceptance
report records final local/CI evidence separately.

Tests are collected from `tests/` with `--rootdir=.` and `PYTHONPATH=..` so pytest
does not traverse the private repository `.env`. No credential-file access is needed.

The user's later explicit instruction superseded the coordinator workflow's
per-gate development pause: continue all development and merge each in-scope change
after tests, CI and independent review pass. Routine engineering choices use the
recommended option consistent with the specification. This authorization does not
fabricate manual comparisons, scientific validation, or runtime athlete approvals.
Unverified outputs must not become downstream coaching authority merely because
their software is merged.

## Provider-mode software — issue #28

Supported provider orchestration adds persisted mode revisions, explicit attempt
outcomes, source/capability/coverage UI and API, and a loopback configuration CLI.
Garmin/FIT imports continue through G1 canonical identities and immutable source
versions; modes never remove saved actuals. Empty relevant data, outage, offline,
authorization, forbidden, rate-limit and unsupported response states remain
distinct. Fallback records its cause and the unavailable secondary branch.
See the [ten-item provider acceptance report](provider-modes-acceptance.md) for
synthetic contracts, duplicate/correction evidence, API/CLI/UI checks and limits.

**Live persistent Strava analytics remains EXTERNALLY BLOCKED by #33.** Official
terms effective 1 June 2026, verified 25 November 2026, conflict with the requested
combined analytical archive. #28 denies that branch with `POLICY_BLOCKED` before
fetch or raw persistence. Strava-only analytics is unavailable; Both retains
supported Garmin output with the limitation; Garmin-primary fallback exposes the
block. Synthetic eligible-secondary decisions are not live-mode verification.
The original full four-mode requirement remains visible and unfulfilled in part.

#33 does not block G2–G6 or planner software: G6 consumes supported ingress and
explicit unavailable states. A23/M08 downstream planner acceptance, manual
Garmin comparisons and prospective scientific evidence remain separate. No
provider policy or runtime athlete approval is waived by software merge.

The provider branch incorporates merged contextual-input main `e7b05cd` and
preserves both schema sets, navigation entries and runtime forms. Integration
checks passed 368 local tests (six browser replays require final CI) and a fresh
Chrome source/context save-and-return walkthrough retained exact context decimals.
An independent-review correction retains live provider-attempt ownership until
the outer result commits, preventing false interrupted pages after Garmin ends.
Barrier, worker-failure and fresh-process regressions preserve read-only GETs and
genuine interruption recovery; the corrected local suite passes 371 tests with
six browser skips pending final-head CI.

## Optional contextual inputs — issue #29

The independent ingestion prerequisite for G5 now has additive append-only wellness,
versioned post-run/pre-week feedback, strength and explicit coverage records. JSON/CSV
imports, entry/correction forms, CLI, event feed and versioned scale/policy controls
retain raw source bytes, exact decimal units, missingness and separate event/known-at
times. Activity donor redirects and canonical source reclassification do not erase
feedback targets. S&C is isolated from running totals; pain/illness reports only emit
attention signals. See [the contract](context-inputs.md) and
[ten-item software acceptance record](context-ingestion-acceptance.md).

PR #35's initial CI passed all 328 tests including five browser flows. Independent
review found browser decimal rounding and two malformed-input 500 paths. Targeted
regressions reproduced them; the fixes pass 336 local tests with five browser skips.
Latest-head CI and changed-diff approval are required before serialized merge.

This is ingestion software, not G5 recovery interpretation, G6 prospective validation,
or P5 approved recovery adaptation. Missing sources remain missing. Real Garmin
wellness mapping/manual comparison and physiological validation remain UNVERIFIED;
the generic owned-export/self-report schema does not claim a vendor-specific adapter.

## Candidate estimator software — issue #27

G4 adds the achieved distance profile, separate Riegel source/target comparisons,
centered OLS CS/D′, residuals/R², source-activity overlap, all supported subsets,
three-versus-four-distance leave-one-out behavior and 56/84/112/168-day sensitivity.
Signed D′ and uncomputable diagnostics remain inspectable. Undefined scientific
cutoffs are editable immutable review policies; defaults remain UNKNOWN.

The Best efforts entry opens actual cutoff/configuration/source controls,
calculate/save and history. Saved non-PB G2 inspections can be selected. Each
saved evaluation copies observations, full source geometry/samples, versions,
QC, reviews, context/optional qualification and exclusions in one transaction.
Later labels/rejections/recomputations do not rewrite earlier evaluations.
Event cutoff, unknown retrospective knowledge cutoff and creation time remain
separate; these are event-time reconstructions, not prospective predictions.
See the [ten-item G4 acceptance record](g4-acceptance.md) for tests and API/UI proof.

G4 software awaits final independent review and CI in its focused PR. Human and
physiological validation remain **UNVERIFIED**, personal predictive validity
**UNTESTED**, and prescription authority false. No personal CS-to-marathon
regression, combined model or calibrated confidence interval is supplied.
