# G4 candidate estimator acceptance — issue #27

## 1. Requirements and authority

Issue #27 implements Part A §§6–7, §11, T06–T08 and G4, plus Part 3 §3.1 of
`Marathon_Coach_Developer_Master_Implementation_Specification_v1_0.docx`.
Original DOCX SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
The working text extraction has a separate byte identity. Parent #15 and #27's
coordinator decisions resolve engineering choices without asserting scientific validity.
The issue branch starts from merged G2 `59faa544096d9b6fc196e4d0aac2992ca84e9cce`.

The issue's acceptance criteria, verbatim, are:

- T06 reproduces exact synthetic d = 3.0 × t + 100, yielding CS = 3.0 m/s and D_prime = 100 m within fixture-defined floating tolerance.
- T07 with only two distances stays insufficient.
- T08 with high R² and ordinary/easy inputs stays unverified.
- Add meaningful fixtures for singular inputs, negative/nonpositive parameters, three-versus-four-distance sensitivity, source-activity dependence, missing windows, source-version replay, no future input and no fastest-forecast cherry-picking.
- Inspect the synthetic OLS ledger through the actual interface.
- Do not claim physiological validity from deterministic tests.

Implementation requirements map to public-interface and numerical evidence:

| Requirement from #27 | Evidence |
| --- | --- |
| “The direct achieved-performance profile is the descriptive anchor: v_obs(D) = D / B(D), using metres and elapsed seconds.” | Public T06 profile test; copied G2 target/time/geometry and speed. |
| “Keep a separate result for every selected qualifying source effort and target distance.” | Explicit faster and slower same-distance Riegel sources survive as separate rows; independent exponential/log comparison. |
| “Missing or singular inputs return an explicit insufficient/unavailable state, never invented points or a clamped value.” | T07, singular, repeated-distance, zero/nonfinite input and numerical-range fixtures; signed negative D′ retained. |
| “Persist a complete input ledger” | Full copied candidate/source/canonical evidence, settings, reviews, context and identity state; concurrent-writer and undurable-boundary-winner regressions. |
| “Leave-one-distance-out sensitivity requires at least four distinct distances; exactly three must return NOT AVAILABLE.” | Three-distance T06 and four-distance five-subset/four-removal tests. |
| “Personal predictive validity initially remains UNTESTED.” | Public JSON, immutable saved result, and actual browser assertions. |
| “Provide actual cutoff/window/source/config controls, calculate/save actions, and historical-result inspection.” | Two real Chromium flows cover settings, explicit non-PB selection, calculate/save, keyboard/mobile and history. |

## 2. Scope and boundaries

Baseline A is descriptive. Baseline B and CS are experimental comparison outputs
with no prescription authority. No personalized CS-to-marathon equation, combined
model, invented confidence percentage, calibrated prediction interval or fastest
forecast selection is implemented. The fixed marathon goal is not a formula input.
G3 exposure, G5 contextual recovery interpretation and G6 prospective validation
remain their own gates. Existing import/archive/SQLite/CLI behavior is preserved.

## 3. Changes and versioned decisions

`estimator_math.py` performs centered distance-on-time OLS on at most six supported
distances. Exact rational intermediate sums of the supplied numeric values avoid
large-time cancellation and false positive slopes when covariance is exactly zero;
reported parameters/residuals use binary64. Sources retain their original numeric
values. Fits outside representable diagnostic ranges return a reason, never NaN.
No epsilon becomes a physiological cutoff. T06 pins `1e-12` m/s and `1e-9` m
tolerances for its public interpolated fixture.

`estimators.py` owns immutable configuration/evaluation rows and one-transaction
capture. `estimator_api.py` and `estimator_views.py` provide the real local
interfaces. `app.py`, `storage.py` and the Best efforts view have additive
registration/navigation changes. The G2 hashed modules remain unchanged.

Configuration defaults: k=1.06; Riegel targets 5000/10000/21097.5/42195 m;
primary recent window 112 days; CS distances 400/800/1000/1500/3000/5000 m.
All 56/84/112/168-day comparisons are always shown. Optional review policies are
initially unset: D′ minimum/maximum, maximum relative leave-one-out CS change,
maximum relative subset CS range, maximum activity contribution fraction,
maximum effort-date span and distant extrapolation ratio. Their raw diagnostics
remain visible with UNKNOWN policies. A configured violation requests review;
it does not establish a scientifically calibrated limit. Negative D′ is never clamped.

Explicit CS requests select one source per distance; duplicates are rejected with
an actionable response. Default scenarios use G2's own winner for each window.
All supported subsets of size three through N are enumerated without ranking by
plausibility. The 400/800/5000 grouping is marked only when available. Sensitivity
distributions retain every calculated value, including conflicting fits.

## 4. Synthetic fixtures and independent expected values

`demo_estimators.py` produces three fictional FIT exports. Their 400/800/5000 m
crossings have elapsed times 100, 700/3 and 4900/3 seconds. The 800 and 5000 m
crossings are interpolated within original sample brackets; their activities
continue to 820 and 5020 m. Selecting those three targets gives CS=3, D′=100,
R²=1 within explicit floating tolerance. Ordinary provenance remains unverified.

Additional fixtures cover two-distance insufficiency, identical elapsed times,
repeated/unsupported distances, negative intercept, negative/zero slope,
large-time arithmetic, ordinary perfect-fit inputs, four-distance removal,
one-workout dependence, absent/stale windows, future complete activities,
saved non-PB sources, wrong ownership, configuration immutability, late context
and source-version changes, and G2 boundary winners without durable inspection IDs.
The Riegel expectation uses an independent exponential/log formulation.

## 5. Test results and regression sensitivity

The focused numerical/public suite passes **21 tests**. Both Chromium workflows
have passed on the assigned headless Chromium executable, including desktop
1280 px/mobile 375 px, keyboard operations, no overflow and no console warnings.
Final broad-suite and deliberate-regression results are recorded below after the
final check. The repository defines pytest CI, with no separate lint/typecheck job.

## 6. Public API and CLI inspection

Generate synthetic exports using `python3 demo_estimators.py /tmp/marathon-g4-examples`.
Start the documented offline app with a new `RUNS_DB_PATH`, separate
`RUNS_ARCHIVE_PATH` and an unused loopback port, then upload the three FITs.
Never use a real athlete database for this walkthrough.

```json
POST /api/estimators/configurations
{"version":"three-trials","config":{"cs_distances_m":[400,800,5000]}}

POST /api/estimators
{"cutoff":"2026-01-15T00:00:00Z","config_version":"three-trials"}
```

The latter returns `evaluation`, `url` and `html_url`. In `evaluation`:

- `observed_profile[].recent[].observation_id` links to `evidence.observations[].id`.
- Each observation retains `candidate.distance_m`, `elapsed_seconds`, raw
  endpoint sample brackets, interpolation, timestamps, source IDs and effective QC.
- `critical_speed.windows[]` contains `days`, `primary` (all selected distances),
  `subsets`, `leave_one_distance_out`, overlap and sensitivity diagnostics.
- `riegel.comparisons[]` retains `source_observation_id`, `target_distance_m`,
  `k`, `elapsed_seconds`, qualification and extrapolation annotation.

`GET /api/estimators` reads the preview, source catalog, definitions and history.
Optional explicit `riegel_sources`/`cs_sources` arrays select
`{activity_id,version_id,source_sha256,candidate_id}`; omitted arrays use defaults,
empty arrays intentionally use no sources. Wrong ownership returns 404; repeated
CS distances return 422. `GET /api/estimators/{id}` reads the saved copy only.
Identical evidence/config/code reuses the same evaluation ID without changing its
creation time. Configuration version reuse with different settings returns 422.

## 7. Actual UI inspection

Best efforts exposes **Compare performance estimators**, retaining its event
cutoff. The dashboard provides configuration selection, new immutable settings,
separate source selectors, calculate/save and saved history. A saved non-PB
400 m interval from the third activity is selectable through the same interface.
With both default checkboxes cleared, an empty CS selection stays unavailable.

Real Chromium tests import the exports via multipart HTTP and use the visible
forms through keyboard actions. They inspect the 112-day 3 m/s, 100 m, R²=1 table,
three residual rows, copied source geometry, missing LOO reason and JSON, then
reopen the same saved evaluation through history. Tables are keyboard-scrollable;
mobile width has no horizontal page overflow. A first replay found an invalid
HTML Unicode-regexp hyphen in the version-name pattern; escaping it removed the
console warning. Synthetic screenshots are generated in the test temporary store.

## 8. Lineage, consistency and replay

All source/head/alias/review/context reads share one SQLite transaction with the
G2 selector. Calculate/save holds that same transaction through the append; a
concurrent context writer cannot change the captured evidence partway through.
GET uses the same read-only capture and never creates a database or writes rows.

The evaluation freezes full candidate payloads, complete supporting canonical
source samples/events, canonical payloads, extraction/QC/code/config identity,
G2 selection inputs/exclusions, current identity state and context/review histories.
This includes transient boundary/rejection candidates, not just their IDs.
Legacy identity knowledge is explicitly UNKNOWN. Original event timestamps,
raw acquisition, derived creation, selection and context known-at remain distinct.

`event_cutoff_utc` is separate from `knowledge_cutoff_utc=null` and artifact
`created_at`; `knowledge_time_frozen=false` remains explicit. Late-known context
can appear in a retrospective reconstruction and is never backdated. The input
hash contains the captured evidence/selection/configuration, not execution or
artifact creation time. Original saved reads do not resolve today's G2 state,
even after recomputation, rejection or a late context correction.

Optional #30 qualification is copied exactly from `provenance.qualification` or
the latest context event. Supported rested/controlled test attestation requires
explicit maximal confirmation and protocol plus the matching qualification
fields. Supported race comparison requires its separate distance/time evidence.
Category/QC acceptance/unsupported legacy declarations never imply those fields.
Absent qualification stays UNVERIFIED. Even supported attestation retains
physiological UNVERIFIED and predictive UNTESTED. This branch tests the additive
storage contract synthetically; integrated #30 verification follows its merge.

## 9. Manual and scientific limitations

All evidence is synthetic. No real FIT files, private training history, credentials,
live provider fetches, manual Garmin comparisons or prospective outcomes were used.
Trace quality, effort provenance/maximality, temporal relevance, model
identifiability, recovery completeness and personal predictive validity remain
distinct. Recovery is not assessed by this estimator gate. The error ledger is
empty; no fabricated population error or personal interval is supplied.
Unverified G0–G4 evidence does not become downstream prescription authority.

## 10. Final review and CI

Software implementation is subject to the coordinator's independent review and
latest-head CI before the authorized merge. The coordinator owns review/merge;
this worker opens the focused PR and freezes its issue branch. CI/review results
and any corrected-head evidence are recorded in that PR and parent #15. This
gate does not authorize deployment/release or waive runtime athlete approvals.
