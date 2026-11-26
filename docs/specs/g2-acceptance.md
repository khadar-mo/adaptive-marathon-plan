# G2 acceptance report — 25 November 2026

## 1. Authority and base

Issue #17 depends on merged G1 #16 / PR #34. This branch started from main
`1319e64fad8d8cdda8f54724f144151fc624e493`. Authority is master v1.0 Part A
§§4–6, §13.2.1, §13.5 T01–T05/T09/T16–T23 and §14 G2. Source DOCX SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
Engineering conventions are recorded in parent #15 and issue #17. Software evidence
does not establish manual Garmin agreement or physiological/predictive validity.
After review fixes, main `e7b05cd834914daa9226750b605a867444d60948`
(context ingestion #29 / PR #35) was merged in `4c4461d`. Both additive schemas,
route groups, navigation entries and acceptance reports were retained.
Round-two fixes also integrate provider main `98d91391b417730741d7ce128b4794ef62dc373d`
in `ce9c99d`, retaining provider/context/effort schemas, routes and all five navigation links.

## 2. Existing implementation preserved

The importer, raw archive/checksums, source selection, duplicate redirects,
dashboard, CLI and serialized SQLite transactions remain authoritative. The G1
canonical persistence hook extracts efforts in the same import/correction
transaction, including linked-owner refreshes. Additive tables retain earlier
versions and donor identities; no raw records are sorted, clamped or discarded.
Explicit offline recompute backfills saved sources. GET requests do not write.

## 3. Delivered behavior

`effort_math.py` searches each G1 running block with first arrival A and last
departure L. It tests observed distances, shifted endpoints, domain bounds,
one-sided limits and deterministic representatives of flat open cells. Confirmed interruptions are never bridged, including when GPS
distance rises. Interior elapsed stationarity/jogging remains in the result.
Source-wide flags and candidate-local interior/bracket checks stay separate.

`efforts.py` stores reproducible extraction/config versions, winning/flagged/whole
and internal evidence, selected inspections, reviewer events and explicit context.
`effort_api.py` and `effort_views.py` provide global bests, per-source/history views,
exact bracket verification and working forms. The inspection includes raw indices,
timestamps/distances, fractions, UTC crossings, sample count, pace/speed, flags,
pause/plateau/gap audit, checksums and code/parser/config lineage.

## 4. Versioned engineering contracts

- Targets are 400/800/1000/1500/1609.344/3000/5000/10000/15000/21097.5/30000/42195
  SI metres. No integer conversion occurs. Whole-activity tolerance defaults to
  exact equality and is separately versioned. Whole evidence uses the full original
  sample interval; header/summary elapsed is displayed separately, never substituted.
- G1 QC remains exactly gap >30 s, stationary review ≥10 s / quarantine ≥30 s,
  interpolated endpoint bracket >15 s, speed >12 m/s, summary mismatch strictly
  greater than max(100 m, 1% of the device-header total), four original records
  overlapping/bounding a candidate. Missing event coverage alone is informational;
  partial history and unresolved applicable anomalies withhold VALID.
  A record without a usable timestamp retains its raw index and QC flag. Its
  uncertain support applies when the original interval/brackets span that index;
  intervals entirely before or after it stay independently eligible. No clock is invented.
- Distance coordinates are exact rationals of the persisted decimal SI values.
  Observed `10.02` and shifted `5010.02−5000` therefore have one identity, retaining
  the original plateau sides; no arbitrary epsilon snap is used. The original
  source values remain unchanged. Crossing clocks and recency comparisons are also
  rational, constructed from original datetime differences without binary64 epoch
  subtraction. Public numeric seconds use binary64; elapsed ranking rounds to nine
  decimal places. UTC presentation rounds half-even to microseconds and never
  determines mathematical recency membership.
  Ties use elapsed, activity ID, absolute UTC start (across timer blocks), then
  deterministic candidate identity. Limits are identified as non-attained.
  A positive exact duration that cannot be represented as positive finite public
  seconds returns 422. Pace and speed ratios are formed rationally before numeric
  conversion. A failed inspection/recompute does not persist a zero-time result,
  configuration, extraction version or head change.
- On a flat open cell, QC can admit four original records while both endpoint
  candidates have only three. The exact coordinate midpoint is an attained
  representative of that constant elapsed optimum. Selection records its open
  domain and distinguishes elapsed attainment from the absence of an earliest
  eligible start. Start tie-breaks compare the finite enumerated representatives;
  they do not invent a first point of an open domain. A nonflat QC domain whose
  infimum is faster than every attained candidate returns `REVIEW_ELIGIBILITY_LIMIT`,
  including across activities. An attained tie may win normally.
- Each one-sided limit names its approached open affine cell, exact rational bounds,
  original endpoint supports and an exact interior witness. QC, recency and rejection
  eligibility come from that cell, not the boundary's instantaneous coincidence.
  A right approach through a speed jump is ineligible even if the limiting end only
  touches the jump. A left approach whose ends remain before the recency lower bound
  is ineligible even if the limiting end equals it. Attained boundary candidates
  are evaluated independently; no review or QC override is applied.
- Segment-end recency uses inclusive `[T−W,T]`; complete recorded activity end and
  every source sample must be at or before T. Missing recorded end excludes as-of
  authority. Comparisons include provisional 56/84/112/168-day windows. All activities
  are assessed against each selected window even when extraction configurations differ.
- Boundary-straddling sources add the actual distance crossing at T−W and re-evaluate
  read-only under their stored configuration. If T−W falls inside a plateau, A still
  means its older first arrival. An unattained infimum faster than every attained
  candidate produces `REVIEW_RECENCY_LIMIT`, including across activities; attained
  evidence remains separate. No epsilon endpoint or delayed achievement is invented.
- Rejected exact intervals are excluded during read-only selection. A tied attained
  alternative may replace them. An unattained post-rejection optimum remains a review
  diagnostic. Identity/rejection uses the reported target, UTC interval and kind,
  scoped by source/version (candidate IDs also include block and attainment).
  Sub-microsecond coordinate variations cannot acquire a new reviewer identity
  for the same reported interval. Stored computed QC and extraction payloads never change.
  The exact ±0.5-microsecond boundaries of rejected reported-time bins partition
  each domain, including half-even endpoint membership. Remaining flat cells yield
  new deterministic midpoints; eligible partition boundaries are also evaluated.
  Repeatedly rejecting a representative does not erase other attained tied intervals.
  These are exact eligibility boundaries, not arbitrary epsilon probes.
- Boundary/review re-evaluation requires the same code checksum as the stored
  extraction. Otherwise a rebuild-required reason is exposed. Historical saved
  candidates remain inspectable; explicit recompute creates new code lineage.

## 5. Public APIs and downstream data contracts

| Entry point | Contract |
|---|---|
| `POST /api/efforts/recompute`, activity-specific `/efforts/recompute` | Explicit offline backfill/recompute; complete immutable config or default. |
| `GET /api/efforts` | `cutoff` with UTC offset, optional `days`; all-time/whole/internal/recent evidence, exclusions and deterministic selection ID. No writes. |
| `GET /api/activities/{id}/efforts` | Optional owned extraction `version` and FIT `source`; source-specific results and lineage. |
| `POST /api/activities/{id}/efforts/inspect` | `version`, `source`, `start_distance_m`, `distance_m`, optional exact `start_coordinate_m` rational string; persist an independently selected interval in one usable block. The exact coordinate must match the displayed numeric value. |
| `GET /api/activities/{id}/efforts/{version}/{candidate}` | Exact verification plus raw excerpt `offset`/`limit`; 1–500 rows, full endpoints always included separately. |
| Same verification URL + `/review` POST | `action` accept/reject/annotate, `actor`, `note`; append-only previous-review linkage. |
| Same verification URL + `/context` POST | `category`, tags, actor, note, optional protocol and explicit `maximal_confirmed`; append-only context ID and previous-context linkage. |

Recompute commits legacy original-member/lap indexing, missing canonical heads and
the requested G2 configuration, versions and selections in one serialized SQLite
transaction. Any extraction failure rolls back the whole batch, including earlier
activities with existing heads. Original archive bytes are immutable; unpacking can
retain a content-addressed copy of an existing ZIP member, but its database index
and derived evidence remain uncommitted until the request succeeds.

Effort identity hashes extraction version, source checksum and the reported interval
including block and attainment convention. Exact rational crossing coordinates and
original endpoint samples remain separate audit evidence. Annotation does not change identity. Context
records are the G2 effort-level provenance authority for later #30 consumption;
they do not implement activity/workout classification. Categories are VERIFIED_TEST,
RACE, TRAINING_FAST, FATIGUED_BLOCK and EASY_OR_UNKNOWN. Computed provenance,
explicit correction, planned label and inferred pattern remain separate.
Maximal confirmation requires explicit user attestation, VERIFIED_TEST and a
recorded protocol; race/fast labels and development review cannot infer it.

Global inspection forms carry the exact rational start separately from its numeric
display. This prevents a nonterminating rational at a rounding boundary from moving
to a different reported interval when the browser submits JSON. Manual numeric
selection remains supported; malformed or mismatched exact coordinates return 422.

Raw `acquired_at`, canonical/source creation, extraction creation/head selection,
review timestamp and context `known_at` are distinct from activity event times.
GET bests is explicitly `EVENT_TIME_RECONSTRUCTION`, `knowledge_time_frozen=false`,
with current capability `NOT_ESTIMATED`. Immutable prospective snapshots belong to
G6. Later source corrections and labels must not be misrepresented as known earlier.

## 6. Acceptance criteria and proof

The issue's delivery paragraphs are reproduced below, with local AC labels.

**AC-1:** Implement the specified first-arrival A(x) / last-departure L(x) exact continuous-effort algorithm on persisted G1 source-specific traces. Split at confirmed pause/stop boundaries before candidates. Test distinct observed distances and shifted endpoints d_j−D plus domain bounds and relevant one-sided limits. On increasing edges interpolate linearly; on a plateau A is first timestamp and L is last. A(x+D)−L(x) retains ALL interior stationary/jogging elapsed time while allowing boundary plateaus to lie outside the effort. Never stitch work repetitions, timer time or separate running blocks. Preserve record IDs and don't sort/clamp invalid source data to create a false PB.

Pass: independent rational oracle and shifted-endpoint fixture; T02/T03/T16–T18;
malformed-order and complete-trace tests in `tests/test_efforts.py`.

**AC-2:** Use the exact versioned QC defaults, endpoint-bracket limit, minimum original-sample resolution and quality states from G1. Confirmed breaks cannot be crossed. Candidate-interior unresolved gaps/plateaus/jumps withhold VALID. Missing events alone are informational for an otherwise dense clean trace. Return the best VALID result using stable tie-break (elapsed, activity ID, start); if none, expose a flagged candidate or NOT_CALCULABLE, never publish a suspect candidate as a PB.

Pass: endpoint 15/16-second and three/four-record boundaries; local QC versus an
unrelated later gap or missing timestamp; partial timer history; T19/T20/T23;
absolute start ties across timer blocks; rejected decimal-plateau identity;
attained flat-cell representative and non-attained QC infimum;
no-authoritative sparse/malformed/summary-only evidence.

**AC-3:** Configured target distances: 400/800/1000/1500/1609.344/3000/5000/10000/15000/21097.5/30000/42195 metres. Keep target precision. Store best whole-activity and internal-segment evidence, all-time and recent scopes separately; recent default112days and 56/84/168 comparisons are provisional. As-of filtering uses effort end [T−W,T] and never includes future input. No recent winner ⇒ MISSING_RECENT, old PB remains. Store effort provenance (VERIFIED_TEST/RACE/TRAINING_FAST/FATIGUED_BLOCK/EASY_OR_UNKNOWN), source/context/correction separately; no inferred maximality.

Pass: exact mile target, whole versus boundary-plateau timing, T09 inclusive expiry,
whole-activity future exclusion (including future records beyond a bad header),
straddling-window oracle, local/global non-attained limits, explicit context lineage.

**AC-4:** Persist immutable reproducible extraction versions with raw/canonical/config/code lineage. Provide API and human-readable best-effort/verification UI: exact start/end UTC and indices, raw endpoint brackets, interpolation fractions, cumulative boundaries, elapsed and pace, pause/plateau audit, QC flags, coverage and checksums; link the original activity. All-time results remain distinct from current estimated capability.

Pass: real import/bests/inspection/API HTML assertions, >200-record extraction and
pagination, immutable source/config/parser lineage, replay/restart/dedup redirects,
explicit backfill and CLI, ownership/invalid-input checks, actual browser workflow.

**AC-5:** Acceptance tests: complete T01–T05 and T16–T23, including5Kwithin15K,5×1Kwithstoppedrecoveries,jogging,720sinteriorplateauversus300sboundary1K,confirmedbreaks withdistanceincrease,unknown12splateau,31sgap/12.1mpsjump,config-versionreplay,interpolatedbrackets,andcleanmissingevents. Add as-of/recency T09, source-version/restart/dedup, malformed distance, ties and no authoritative result. Explain any sparse normative example separately from the production four-sample/default-gap policy; do not weaken defaults merely to make illustrative arithmetic eligible.

Pass: named T-case tests and independent expected arithmetic. T16's 720-second 2K
remains SUSPECT; T17's legitimate 300-second boundary kilometre remains mathematically
correct but sparse production resolution is insufficient. Dense companion fixtures
exercise real eligibility. T22 explicitly selects 100.5→5100.5 m, crossing two
interpolated brackets; it does not invent a uniquely fastest both-interpolated optimum.

**AC-6:** Delivery: preserve the existing architecture and raw/archive/SQLite invariants. Use synthetic fixtures only; no private training data or credentials in GitHub. Add meaningful tests, inspect real public API/UI behavior, provide a ten-item acceptance report and update the gate audit. Independent review and CI must pass on final code before merge. The user explicitly authorized continued development and merge without per-gate stops; retain honest manual/scientific-validation status and runtime athlete approvals. Parent #15 holds coordinator state.

Pass for implementation/fixtures/report; final CI and independent review are recorded
in the linked PR before the coordinator's serialized merge. Manual/scientific status
remains UNVERIFIED.

## 7. Commands and observed checks

From the isolated worktree root, use `PYTHONPATH=. .venv/bin/python -m pytest tests -q`.
The worktree has no private `.env`; the ordinary checkout is not used for tests.
Development dependencies are isolated in this worktree's `.venv`; only synthetic
temporary stores are used. CLI subprocess tests anchor their script path to the
repository, independent of pytest's working directory.

- G2 numerical/API suite plus the independent oracle: **37 passed**.
- The integrated full local run using the cached standalone Chromium headless
  shell exercised all 416 checks: **414 passed** and two G2 browser harness checks
  failed. The three-file import needed an explicit bounded completion wait instead
  of Playwright's five-second default; the replacement test read a fetch body after
  navigation had discarded it. It now verifies the submitted request and saved
  Verification JSON. Both affected browser replays then passed in **10.81 s**.
  All eight browser flows have therefore passed locally, with no skipped checks.
  Final-head Linux CI reruns the complete suite; its result is recorded in the PR.
- The earlier macOS Chrome app stall was stopped at its 55-second bound. The
  separate installed headless shell supplied the final actual browser replay;
  the old stall is not recorded as a passing check.
- Removing the import extraction hook made the public T01/T05/T22/T23 test fail.
  Replacing last departure with first arrival made the normative plateau test fail.
  Source was restored after each mutation.
- The independent review's four math/QC defects each have new public regressions.
  All four failed on the reviewed implementation before the fixes. An additional
  nonflat open-QC test failed before adding the explicit limiting state; a global
  regression failed before propagating that limit across slower activities.
  The decimal-plateau regression rejects the actual saved optimum and verifies it
  cannot return under another ID. The browser replay uses the real combobox's
  accessible role/name, retaining the saved context and Accept form assertions.
- Round-two regressions reproduced both cross-activity selection errors and loss
  of tied alternatives before the fixes. A separate oracle independently checks
  600 exact-rational QC-cell and recency cases, including jumps, gaps, plateaus,
  record counts and direction-specific limits. Repeated public reject/inspect
  cycles verify 24 distinct attained replacements. Half-even boundary tests check
  both open/closed rejection bins, rational recency before a rounded UTC cutoff,
  exact-coordinate JSON round-trips and invalid-coordinate 422 responses.
- The reviewer identified a finite subnormal target (5e-324 m) whose elapsed value
  underflowed to zero. The new public regression first reproduced ZeroDivisionError.
  Inspection and recompute now return 422 with a byte-identical database; staged
  configuration registration rolls back with the failed extraction.
- Final review found that legacy canonical backfill had committed before a failed
  G2 request. Three public regressions reproduced this on the reviewed head: one
  legacy ZIP, a mixed legacy batch, and a batch with an already processed activity.
  All now retain byte-identical SQLite after 422, then successfully reuse the same
  configuration name on retry. Restored member/lap indexing, summary-only evidence,
  original ZIP bytes and offline behavior are verified. Focused checks: **20 passed**;
  complete canonical/import suites: **105 passed**. A separate loopback HTTP replay
  confirmed unchanged canonical/effort reads and SQLite after failure, followed by
  successful processing of both activities with their original IDs.
- Final mutation checks removed raw-index uncertainty overlap, then replaced the
  rational coordinate conversion with binary64. Each corresponding new public
  regression failed as expected. Both mutations were restored byte-for-byte;
  all 30 G2 tests passed again.
- Real HTTP multipart upload into a separate loopback demo store completed. Its
  winning 5K is exactly 1,440 seconds, 09:15–09:39 UTC on 10 January 2026, original
  records 180–468, cumulative 3000→8000 m. A separate full 5K is 1,500 seconds.
- CUA desktop 1280px/mobile 375px checks had no page overflow. Keyboard result
  navigation/pagination worked; Accept persisted; RACE context persisted with
  maximality NO; the selected interval showed fractions 0.029994001199760045 and
  0.9890552995391934 at brackets [6,7] and [300,301]. No console errors/warnings.
- That browser check found a real `form.action` collision with a named action
  button. The handler now uses `getAttribute('action')`; the replay submits Accept
  through that button and checks the saved verdict. The new browser flow also
  rejects two flat-cell representatives and follows each actual global replacement
  form, verifying its exact coordinate, distinct candidate ID, saved geometry and
  unreviewed state after navigation. Mobile overflow checks pass with all five
  integrated navigation links.
- Final integrated real HTTP proof imported five synthetic traces into a separate
  store. Missing clock at 200 m withholds the 400 m PB. In the 1200 m companion,
  100→500 and 700→1100 m remain VALID; 400→800 m is SUSPECT with original record 30.
  The 15 m flat-cell result is 7.5→22.5 m, 3 s, with four original records.
- Final CUA proof at 1280/375 px used keyboard Reject on the exact decimal plateau
  interval (10.02→5010.02 m, 1500 s). It stayed INVALID; global 5K displayed
  `REVIEW_REJECTION_LIMIT` with no PB and a 1505 s unattained limit. The combobox
  stored RACE with maximality NO. The flat-cell verification explained its attained
  midpoint and lack of an earliest start. All four merged navigation links fit
  mobile; Context opened correctly. Page width equaled viewport width, console
  warnings/errors were empty, and the temporary viewport was reset.
- Round-two real HTTP proof imports into an isolated store: after rejecting the
  1500 s optimum, the jump source contributes only its eligible 1505 s left limit
  and another activity's 1502 s 5K wins. The recency boundary fixture selects the
  other activity's 3.75 s result with no false limit. Subnormal requests return 422
  and leave the database checksum unchanged.
- Bounded synthetic performance check (no performance acceptance threshold):
  12,001 original one-second records, 30 km/12,000 s, all 12 standard targets took
  0.239 s for decode/QC and 37.372 s for extraction at `ce9c99d` before the final
  numeric-representation guard. It evaluated 215,199 attained candidates and
  215,188 limits and found 11 eligible targets (the trace is shorter than 42,195 m).
  On the final guarded code, a separate real multipart import of 2,001 records,
  5 km/2,000 s and all 12 targets completed in 3.512 s. Both completed within a
  120-second bound. The readable reference search remains computation-heavy on
  dense long runs; these timings are measurements, not validated service limits.

## 8. Reproduce the synthetic walkthrough

Generate `python3 demo_efforts.py /tmp/marathon-g2-examples`. Start the local app with
`RUNS_GARMIN_OFFLINE=1`, a new `RUNS_DB_PATH` and separate `RUNS_ARCHIVE_PATH`; choose
an unused loopback port. Upload the three generated FITs. Open activity 1's continuous
efforts and its 5K result. Verify indices 180 and 468, elapsed 1440, pace 288 s/km.
On Best efforts, use cutoff `2026-01-15T00:00:00Z` to compare the internal result with
the separate 1500-second whole 5K. The plateau fixture's 1K is flagged, not a PB.

Select cumulative start 100.5 m and target 5000 m to inspect both interpolated
endpoints. Expand their raw records and reproduce end minus start. Add a review or
context note, reload, and inspect the actor/time/previous-event chain. Changing
settings requires a new version; selecting an old version keeps its original proof.

## 9. Unverified evidence and limitations

No real FITs, private history, credentials or live provider calls were used. The
required manual Garmin comparisons, the 10–20 activity QC-threshold reviews, scientific
validation and prospective accuracy are **UNVERIFIED**. Sparse/contradictory blocks
are withheld without repairs. Missing source end or unavailable archive excludes
global authority. Stored observed PBs are not estimated present capability.

The implementation stores compact extracted winners and immutable supporting source
versions, not every candidate permutation. Read-only boundary/review selection can
re-evaluate those sources under identical code/config, with exact-selection failures
exposed. A saved selected inspection is separate evidence, never automatically a PB.

## 10. Gate status and next work

G2 software is implemented subject to final independent review and green CI before
the authorized coordinator merge. Human/scientific G2 acceptance remains UNVERIFIED.
G3/G4/#30 may consume the stable evidence and context contracts after merge; they
must preserve source quality, event-time versus known-at distinctions, provenance,
missingness and validation status. Coaching decisions still require the applicable
runtime athlete approvals and accepted downstream gates. No release/deployment is
part of this change.
