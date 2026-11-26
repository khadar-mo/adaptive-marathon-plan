# Fueling input acceptance — issue #39

## 1. Authority and scope

Master v1.0 L15, Part B §12 APE-FUEL-01–03 and Part C §10.2/M30; SHA-256
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
The implementation began at merged G2 `59faa54`, then incorporated actual merged
workout provenance #41 at `454896ddbd6dd05be3246602cc0d97abec3b714e` before its first
working checkpoint. Final integration also includes merged G4 #40 at
`5dffef9d46fd7bcdd782559bb3d7619a8c2d5d02`; all API registrations and both audit
sections are retained. No unmerged fitness or planning dependency is used.

## 2. Exact issue criteria

> Record optional fueling practice for a completed run, including practice status, a runner-entered strategy, gastrointestinal tolerance or symptoms and timestamped notes. Inspect and correct a record from the activity. Keep the original saved activity reference and provenance through duplicate import, an identity redirect or a correction.

> Preserve the source, actor, observed/reported time, server receipt time and immutable previous revision. Follow the Master practice statuses NOT_PLANNED, OFFERED, PLANNED, COMPLETED and UNKNOWN. An absent entry leaves practice and tolerance unknown.

> Keep the research status RESEARCH_PENDING. User-entered quantities or timing may be retained verbatim as part of their strategy. Generated carbohydrate, sodium, fluid and timing recommendations are outside the approved specification.

> Keep this input separate from running metres, fitness and readiness decisions. Provide normal API/form entry, exact-revision inspection and the read interface for #25 to link to a planned session. #25 retains the planning, taper and forecast acceptance criteria.

> Verify a normal activity-page entry and correction, missing tolerance, duplicate replay and unchanged activity accounting. Use synthetic evidence in GitHub. Include a focused acceptance report with the API and browser results.

## 3. Interfaces and engineering choices

`kind=fueling` uses existing context import/archive/revision/event transactions.
`fueling.py` reads a target and exact revision; `fueling_web.py` provides activity-linked
forms, GET/POST `/api/activities/{id}/fueling` and `/api/fueling/versions/{version_id}`.
Source/actor/time text and all runner content remain as supplied. The actor is a
reported label, not authenticated identity. New entries refer to recorded running
activities; known future ends are rejected, while missing legacy end time remains
unknown. No unresearched long-run threshold limits the optional input. #25 can retain
record/version IDs without rewriting this history. See [the contract](fueling-inputs.md).

## 4. Acceptance mapping

| Criterion | Public evidence |
|---|---|
| Activity entry, correction, exact revision and timestamped notes | `test_activity_fueling_entry_correction_exact_text_missingness_and_keyboard` and correction/cutoff API test |
| All Master statuses and missing practice/tolerance | `test_master_statuses_do_not_invent_tolerance_or_planning_decisions`, optional-unknown test and future-report test |
| Source, actor, observed/reported/receipt time, previous revision | Correction/cutoff API test and archived original-byte assertions |
| Verbatim quantities/timing/content and RESEARCH_PENDING | Exact API/CSV tests and browser leading-newline/CRLF/Unicode correction assertions; override rejection |
| Duplicate import, redirect and reclassification identity | Donor-redirect, reclassification and replay tests; target reference immutability assertions |
| Independent of running metres, fitness/readiness and workout labels | Before/after activity, canonical and effort equality tests; no attention signal generated |
| Supported old format without invented canonical history | Genuine merged-G0 and merged-G2 fixtures through normal app startup/API, and pre-FIT unknown-time test |
| Clear failures / atomic import | Invalid payload matrix, stale corrections, foreign target and integer-bound tests |

## 5. Early representative-input comparison

Checkpoint `596a659` passed a normal public-entry comparison using the genuine
merged-G2 fixture: 30 metres, 6 seconds, same saved effort version before/after a
fueling report and duplicate FIT replay. The coordinator independently exercised
an actual merged-G0-generated synthetic 10000-metre database. That check exposed
an unguarded canonical-table query returning 503 before any entry.

`tests/generate_fueling_legacy_fixture.py` now reconstructs that generation using
`git archive 4a0630f65885b1515e59302075e37c5632bcb2da` and its ordinary upload API.
The checked-in 1505-byte compressed fixture has no canonical tables. The regression
first failed with the same 503; after the read-only fallback it verifies missingness,
entry, correction, exact revision, old replay, restart and unchanged 10000 m / 3600 s.
No synthetic fixture was made by deleting tables from current output.
The coordinator's independent recheck of the corrected checkpoint confirmed the
10000-metre run stayed unchanged, verbatim note/retry behavior, historical cutoff
selection and HTTP 409 for a stale correction.

## 6. API, persistence and CLI boundaries

Public routes verify JSON and CSV, exact raw archive bytes, validation, stale-write
conflicts, history, event feed, known-at cutoffs and source identity. Read-only views
leave SQLite bytes unchanged. An invalid batch leaves no report/event. Fresh app
startup retains prior reports. The existing context CLI automatically accepts the
same kind; no new CLI command, credential or provider fetch is needed.

## 7. Actual browser evidence

The repository's Playwright flow runs real loopback HTTP with synthetic FIT upload,
then follows the normal activity link and keyboard-saves a report. It inspects the
exact revision, corrects at mobile width, verifies missing tolerance and preserves
leading newlines, whitespace, Unicode and an existing CRLF note during unrelated
edits. A cutoff restores revision 1 and hides later tolerance. Desktop 1280 px and
mobile 375 px have no horizontal overflow; no console errors/warnings or failed
requests occur. The existing context browser regression also passes.

## 8. Check commands and regression strength

From the worktree root, focused API/context checks:
`PYTHONPATH=. .venv/bin/python -m pytest tests/test_fueling.py tests/test_context.py tests/test_context_form_serialization.py -q`
— **81 passed**. The new fueling plus existing context browser tests passed **2 tests**
with the coordinator-provided headless Chromium on PATH. The G0 regression failed
against the first runnable implementation, then passed with the fallback restored.
Full-suite and final-head CI results will be recorded after code freeze.

On checkpoint `70e88a6`, the full root suite with headless Chromium passed **496
tests, no skips**, 203.12 s. [CI run 45736077535](https://github.com/khadar-mo/adaptive-marathon-plan/actions/runs/45736077535)
also passed. A temporary mutation disabled preserving original text in the production
form script: the real browser test failed its stored-note equality assertion because
CRLF/CR became LF (one failure, 5.42 s). Production code was restored before integrated
checks. The G4-integrated API regression additionally asserts unchanged estimator
preview output before/after fueling entry. Final integrated checks are tracked on PR #44.

Independent #44 review then found three edge cases: a known future start with no
end could accept a completed-history entry; the historical JSON link lost its cutoff;
and whitespace-only actor/tolerance counted as supplied text. New direct regressions
produced **9 failures and 1 already-correct case** before the fixes. New entries now
reject a known future start or end, historical links fetch the same revision, and
blank-only actor/tolerance is rejected while meaningful multiline text is unchanged.
A known past start with unknown end remains supported. The browser replay follows
the actual historical JSON link as well as the direct HTTP regression.

## 9. Boundaries and unverified work

All test data are synthetic. No real FIT, credentials, private history or nutritional
science validation was used. M30/APE-FUEL forbid generated doses; the feature records
the runner's own content only. It does not assign planned sessions, score readiness,
change fitness estimates, create approvals or complete #25's taper/forecast work.

## 10. Independent review and final delivery

[PR #44](https://github.com/khadar-mo/adaptive-marathon-plan/pull/44) records the
final head, CI run and independent-review verdict. Latest-head CI and independent
approval are required for the coordinator's serialized merge. The root owns merge
sequencing. Software conformance does not complete fueling research, runner-specific
validation or athlete approval of a future protocol.
