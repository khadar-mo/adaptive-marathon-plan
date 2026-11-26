# G3 training exposure — implementation and acceptance

Issue #18, parent #15. Authority: Developer Master Implementation Specification
v1.0, SHA-256
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
Read Part A §§2, 8–9, 12–14; Part 2 §§2.3–2.4; Part 3 §3.1 and Part 4 §§4.3–4.4.
The coordinator's dated decisions in #18 resolve endpoint, coverage, knowledge
time, FIT summary-message ordering and intensity input interfaces. They are
engineering policies, not physiological validation.

## 1. Requirements and scope

G3 computes accepted, deduplicated completed running exposure: 7/28/56-day
distance, separate elapsed/timer/moving duration, frequency, ISO weeks, longest
30/56/112-day runs, observed spacing/gaps and proposed-session L30 comparisons.
It provides real input, correction, import and inspection interfaces for running
census evidence, dated zone profiles and measured band-time ledgers.

No workout is prescribed, no approved mileage is changed, no injury probability
or capacity ceiling is calculated. Strength remains a separate context modality.
Manual verification and scientific/personal predictive validation are not
established by these software tests.

## 2. Versioned calculation decisions

`g3-exposure-v1` records the following rules in every input bundle:

- Rolling membership is the whole accepted session ending in `(T−W,T]`, using UTC
  elapsed days. G2's inclusive recent segment-end convention remains separate.
- Accepted canonical summary START/END is the workload timing pair. If that pair
  is missing, the selected FIT's own valid START/derived END pair may be used.
  Invalid/inverted pairs remain unassigned. Never combine a summary start with a
  FIT end. The FIT end is session start plus reported elapsed, not the summary's
  message-written timestamp. Both spans, message timestamp, basis and any differing
  window membership remain visible. A timing fallback does not fabricate a missing
  accepted distance or duration.
- FM ISO weeks use END local date in Europe/London; planner weeks use START.
  Both are `[Monday,next Monday)` in London, with DST offsets retained. Whole
  sessions must be complete by T. An open week's observed interval ends at T
  inclusively; its future remainder is OPEN, not an unobserved historical gap.
  The existing History dashboard retains recorded-local-date accounting.
- The report includes 17 calendar weeks, covering the 112-day exposure context.
  This display bound is not a minimum training-history or physiological policy.
- Spacing is previous END to next START in UTC; negative overlaps remain negative.
  Gaps are descriptive intervals with their own census evidence, not inferred rest.
- L30 uses actual END local dates in `[proposed START local date−30 days, proposed
  START local date)`, plus actual end ≤ T. The proposed day is excluded. Future
  proposals cannot turn unobserved future history into coverage.
- L30 precedence is INCOMPLETE_HISTORY when census/timing/distance is incomplete,
  then NOT_CALCULABLE when no positive denominator exists, otherwise CALCULABLE.
  The ratio is proposed metres / longest metres; percentage change is
  `100 × (ratio − 1)`. No warning threshold is applied by G3.
- Decimal-derived sums preserve fractional metres and separate durations; JSON
  quantities are decimal strings. Planner integer rounding does not occur here.
  Source values already represented as floats by the canonical importer are read
  through their decimal string representation; G3 cannot recover precision absent
  from the source contract. The G1 QC configuration and existing mismatch
  comparisons are frozen without introducing another mismatch threshold.

## 3. Census and missing measurements

An empty activity list does not establish complete history. Coverage requires an
explicit immutable running assertion with endpoint basis, exact UTC bounds and
inclusivity, observed-through time, reported/known-at times, evidence kind, actor,
source references, reason and unresolved exclusions. Athlete attestation, import
manifest and provider census are supported. START and END are separate assertions;
selecting BOTH creates two records in one transaction. London date entry converts
midnight boundaries server-side and preserves their offsets.

Coverage cannot exceed observation. Complete assertions with unresolved exclusions
are rejected. Active UNKNOWN/INCOMPLETE assertions overlapping a requested interval
prevent completeness even if another source asserts COMPLETE. Corrections replace
only that logical assertion from their knowledge time; earlier revisions remain.
Point gaps at exclusive touching boundaries are retained exactly.

Legacy Garmin scan capture preserves the complete current scan/item evidence in
the archive, with its actual scan observation time and a new capture knowledge
time. Its basis remains RECORDED_LOCAL_DATE and status UNKNOWN for canonical
START/END use. It does not certify an entire current day or transform a missing
original FIT into missing summary history. Wellness/S&C coverage cannot establish
running census.

Each measure reports `known_subtotal`, `missing_count`, nullable complete `value`
and status. Frequency likewise separates known count and complete value. Census
completeness is independent of measurement completeness. Unknown timing rows retain
their known measures under `unassigned`; they conservatively prevent authoritative
window totals because membership is unknown.

## 4. Stable serializer and immutable lineage

Use one existing serialized SQLite transaction:

```python
bundle = workload.collect(db, normalized_T, normalized_K, mode, proposal_or_none)
result = workload.calculate(bundle)
```

Normalize absolute timestamps with `workload_inputs.utc`. `mode` is
`EVENT_TIME_RECONSTRUCTION` or `AS_KNOWN`. A proposal is
`{"start_utc": "...+00:00", "distance_m": "18000"}`. `calculate` reads no database,
archive, provider, current clock or mutable head; frozen inputs can be passed
directly to it.

The bundle contains algorithm/config/code identity, event and knowledge cutoffs,
proposal, exact canonical payloads, creation/selection events, identity groups
recorded in those payloads, accepted measures/timing, raw/source IDs, QC settings,
selected input revisions and referenced profile versions. Provider mode revisions
and terminal attempt results are retained as known at K. A later terminal result
cannot appear in an earlier pending attempt; current runtime ownership is not used.
Mode changes do not erase retained actuals. Strava POLICY_BLOCKED remains explicit
when present in the persisted attempt; no live Strava capability is invented.

The selection timeline is read without resolving today's aliases or heads. A
donor merge becomes effective when the surviving canonical selection records its
merged IDs; the deleted donor's earlier payload remains usable before that time.
An old version reselected later follows selection time, not creation time.
Legacy rows with no timestamped selection cannot be assigned invented historical
knowledge. Current operational backfill guidance is outside the frozen bundle and
input hash. It is not used to alter historically known membership.

`AS_KNOWN` requires K ≤ T and no future knowledge cutoff. Its artifact explicitly
says it is a retrospective calculation with the recorded code, not a prediction
originally made at T. Event-time reconstruction can use later-arriving evidence
but exposes K. G6 still owns full prospective fitness snapshots and outcomes.

`capture` obtains the bundle and persists the immutable result in one write
transaction. Input hash excludes execution time; snapshot creation time is separate.
Repetition with identical inputs returns the same identity. Replay uses only the
stored bundle, checks the calculation-code hash, and returns IDENTICAL or MISMATCH.
Changed calculation code returns REQUIRES_RECORDED_CODE with the preserved artifact
instead of silently claiming exact replay under a different implementation.

## 5. Dated zones and measured intensity

Profiles retain method, system, metric/unit, effective interval, calibration text,
source, reliability and knowledge time. Native five-zone, research three-zone and
other systems are independently named; there are no invented thresholds. A band
ledger references an exact canonical version and exact profile version, supplies
seconds for every band, states elapsed/timer/moving semantics and retains measurement
method, timestamp and raw source.

Supported supplied methods require a known profile covering the complete session,
available matching duration, nonnegative finite seconds and no overcount. Conflicting
ledgers cannot double count a session. Uncalibrated profiles and uncalibrated research
three-zone interpretations retain reported evidence but contribute no validated
band seconds; their available duration remains explicitly unknown. Missing ledgers
also retain unknown duration and missing-duration counts. Methods/profile versions
are never silently pooled. Profile correction does not rewrite an old ledger's
explicit method reference. A corrected canonical version needs its own ledger.

`zone_profiles_at_cutoff` exposes sourced profiles effective at T and known by K;
multiple profiles remain visible rather than silently selecting a physiological
winner. CALIBRATED means supplied calibration evidence, not scientific approval by
the application. Planning-time estimates belong to #22.

## 6. Public API and CLI inspection

| Route | Contract |
|---|---|
| `GET /api/workload` | `as_of`, `knowledge_cutoff`, `mode`, optional `proposed_start` and `proposed_distance_m`; read-only calculation |
| `POST /api/workload/capture` | Same fields as JSON; immutable input bundle/result plus snapshot ID |
| `GET /api/workload/snapshots/{id}` | Original frozen inputs, result and creation time |
| `GET /api/workload/snapshots/{id}/replay` | Replay using the exact saved inputs and compatible code |
| `POST /api/workload/inputs` | One JSON coverage/profile/band ledger or an atomic list |
| `POST /api/workload/import` | Multipart JSON file; exact raw bytes archived, idempotent imports |
| `POST /api/workload/coverage-dates` | London date convenience; DST-aware bounds; BOTH creates START and END assertions |
| `GET /api/workload/inputs/{record_id}` | All immutable correction revisions and source references |
| `POST /api/workload/provider-coverage` | Freeze current legacy scan evidence; no provider request or completeness promotion |

Input examples and all validation fields are represented by `Coverage`, `ZoneProfile`
and `BandLedger` in `workload_inputs.py`. Corrections require `supersedes` and
`correction_reason`; stale edits return HTTP 409. Invalid requests return 422.

The offline CLI requires an explicit database and never loads credentials:

```sh
python workload.py --db /absolute/synthetic.sqlite3 --as-of 2026-11-01T12:00:00Z
python workload.py --db /absolute/synthetic.sqlite3 --import-json /absolute/inputs.json
python workload.py --db /absolute/synthetic.sqlite3 --capture --as-of 2026-11-01T12:00:00Z
python workload.py --db /absolute/synthetic.sqlite3 --replay SNAPSHOT_ID
```

Public synthetic API tests exercise original FIT and summary-only provider imports,
duplicate filenames, accepted-summary corrections, raw input precision, idempotent
imports, revision conflicts, both date bases, immutable capture/replay and read-only
GETs. The CLI is exercised in a subprocess from an unrelated working directory.

## 7. UI inspection

History links to `/workload`. The page exposes window bounds and source contributions,
separate FM/planner week tables, L30, spacing/overlap/missing history, dated zone
profiles and intensity evidence. Real forms enter coverage, calibration profiles and
measured seconds; JSON import and revision correction/history are available. Frozen
calculations have their own URL and exact evidence/replay links.

A real loopback Uvicorn server and Chromium replay cover FIT upload → History →
Workload, BOTH London coverage entry, a calibrated profile and positive measured
ledger, correction/history, L30, keyboard interaction, desktop/mobile overflow and
frozen replay. Temporary synthetic stores and an OS-assigned socket isolate this
flow from user services and private history.

## 8. Fixtures and meaningful test mapping

| Requirements | Independent evidence |
|---|---|
| T10 | Renamed identical FIT counted once; real donor/survivor merge after K preserves earlier grouping even after donor deletion |
| T11 | 16,000 m observed denominator, 18,000 m proposal → 1.125 / +12.5%; proposed day excluded |
| T12 | Empty/zero complete census → NOT_CALCULABLE; missing/partial/future census → INCOMPLETE_HISTORY and null ratio |
| T14 upstream | Appending a future activity, later provider result or mode selection leaves earlier strict result/hash unchanged |
| T15 upstream | Source timing/distance correction creates a new canonical version; old snapshot/replay remain identical after restart |
| Exact arithmetic | 0.1 + 0.2 = decimal 0.3; fractional elapsed; high-precision measured band JSON survives import and aggregation |
| Boundaries | Exclusive rolling lower, inclusive upper, microsecond outside, inclusive L30 calendar lower, exclusive proposed day |
| Calendar | Cross-midnight END versus START assignment, repeated local DST clocks, 169-hour fall-back week, complete-to-cutoff OPEN week |
| Missingness | Missing distance, duration, endpoint, original FIT and intensity; known subtotals retained; no inferred rest |
| Coverage | Point gap between exclusive assertions, conflicting evidence, current-day observation limit, atomic corrections, revision clocks |
| Source consistency | Accepted headline versus FIT span; missing pair uses own FIT pair; inverted pair stays unassigned; written timestamp separate |
| Intensity | Positive supplied calibrated time, independent native five-zone time, uncalibrated and uncovered unknown seconds, conflicting ledgers and overcount |
| Lineage | Version reselection, frozen configuration/QC, alias/history independence, provider unavailable capability, pure replay |

## 9. Verification evidence and limitations

Verification is synthetic. The required independent Garmin/export arithmetic and
original-file manual comparison remain separately recorded evidence. No real FIT,
private training history or credentials are included or read by this worker.
Runtime athlete approvals and scientific-validation requirements are unchanged.

Local verification on 26 November 2026 in the exclusive issue-18 worktree:

- Full regression suite: **457 passed in 149.22 seconds**, with Chromium on PATH;
  no skipped browser checks. This includes 24 G3 core/API checks and its real
  browser flow alongside all existing importer/canonical/G2/context/provider tests.
- The separate G3 browser replay passed in 9.06 seconds. It proved that an unquoted
  `1000.12345678901234567890` band-time value survives production form serialization
  and storage. JavaScript validates JSON syntax but sends original decimal lexemes.
- Desktop and mobile screenshots were visually inspected. The final presentation
  correction gives workload/week tables bounded horizontal scrolling on narrow
  screens; its actual browser replay is recorded in the PR verification summary.
- Coordinator's independent checkpoint HTTP comparison matched 12 rolling windows,
  four prior-30-day maxima and the independently generated 44/48/49/50 km weeks.
  GETs left that database unchanged. This independent synthetic comparison does
  not substitute for manual/scientific validation.
- `git diff --check` passed. Test servers use inherited OS-assigned sockets and
  temporary synthetic databases; reserved user service ports were not used.

## 10. Independent review, CI and release boundary

The coordinator reviews the final committed PR head independently and merges only
after required CI/repository checks pass. Checkpoint commits are not merge authority.
The user authorized continued development and merging without routine per-gate
permission requests; that does not authorize deployment/release or fabricate human
or prospective scientific acceptance. Final review/CI links belong in the PR and
coordinator handoff.
