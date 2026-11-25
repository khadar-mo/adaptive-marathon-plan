# Provider modes — issue #28 acceptance report

Authority: master specification v1.0, source DOCX SHA-256
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
Read Part 1 L16; Part 2 §§2.1–2.3; Part 3 §3.1; Part A §§3.1–3.2, 4.1.
The dated 25 November 2026 preflight in
[issue #28](https://github.com/khadar-mo/adaptive-marathon-plan/issues/28)
controls the live Strava branch. This is software evidence, not scientific or
manual Garmin acceptance. The full live four-mode requirement remains partly
unfulfilled under [external blocker #33](https://github.com/khadar-mo/adaptive-marathon-plan/issues/33).

1. **Requirements.** The acceptance criteria below retain the issue's wording.

   | Criterion | Result and evidence |
   |---|---|
   | “synthetic REST/export adapters exercise all four modes” | Pass for supported runtime and synthetic mode contracts: `test_all_runtime_modes_and_fit_independence`, `test_synthetic_eligible_secondary_mode_contracts_use_g1_identity_without_double_count`. Existing synthetic REST transport tests cover Strava listing; the runtime analytical adapter is blocked. |
   | “an ordinary empty day versus an outage, unauthorized/unsupported responses, fallback reason and source precedence” | Pass: parametrized `test_empty_is_not_outage_and_failures_keep_blocked_fallback_explicit`; pure secondary contract covers eligible fallback and limited scope without fetching or storing Strava activity data. |
   | “summary-only downgrade, duplicate and ambiguous copies” | Pass: `test_actual_capability_tiers_no_invented_samples_or_events`, `test_ambiguous_source_stays_reviewable_through_modes`; identical Garmin original/FIT reuse counts once through `/api/history`. |
   | “correction/reimport/idempotency, as-of timestamps and decimal precision” | Pass for ingress: `test_config_versions_stale_writes_and_attempt_freeze_survive_restart` proves 5001.25 → 5002.125 m correction, immutable prior version and attempt, distinct activity/knowledge times, stable reimport and restart. |
   | “Cover T10 duplicate accounting, relevant T15 correction lineage, and integration acceptance A23/M08 when the planner adapter exists; do not claim those downstream tests complete prematurely.” | T10/T15 ingress evidence passes as above. A23/M08 planner integration is not claimed; no planner adapter exists on the implementation base. |
   | “Use meaningful CLI/API integration tests and inspect the actual mode/capability UI.” | Pass: local Uvicorn subprocess/CLI, ASGI public API, CUA Chrome desktop/mobile/keyboard inspections below. Five automated browser replays remain required in final CI. |
   | “No real credentials or personal activities belong in fixtures or GitHub.” | Pass: generated FITs, scripted REST responses, temporary databases, isolated own server. No live private access. |
   | “Live Strava analytical/persistent ingestion must return explicit POLICY_BLOCKED before fetching or persisting Strava activity data.” | Pass: direct endpoint and Strava-only orchestrator denial spies on settings, request, archive and canonical save; no import row/raw archive from the denied branch. |
   | “provide working editable/importable UI and API paths for the inputs and configuration this issue owns, with validation, persisted changes and an inspected end-to-end update” | Pass: immutable mode revisions, optimistic stale-write check, all mode options, selected-source import form, date validation, existing explicit Garmin and FIT paths. CUA saved, reloaded and submitted two modes. |

2. **Scope.** `GARMIN_ONLY` imports via the existing Garmin path; exported FIT
   remains sufficient and independent. `STRAVA_ONLY` analytical imports are
   unavailable. `BOTH` retains usable Garmin results and shows the Strava block.
   `GARMIN_PRIMARY_FALLBACK` records the exact empty/offline/auth/quota/unsupported
   cause and blocked fallback. A pure, non-ingesting decision function exercises
   hypothetical eligible secondary observations with synthetic canonical IDs;
   it is not an enabled live or user-supplied Strava payload adapter. No option,
   environment variable or API body removes the policy guard. Existing explicit
   Garmin/FIT imports remain available in every mode; source choices do not
   filter or erase prior actuals.

3. **Changes.** `provider_modes.py` stores mode revisions and attempt results,
   reuses the import lock and G1 identities, and reads local capability/coverage.
   `provider_views.py` adds the Sources page and diagnostics. `app.py` exposes
   configuration/status/import/attempt endpoints. `provider_cli.py` uses only the
   loopback API. `garmin_import.py` optionally returns typed observations and
   captures canonical heads before releasing import ownership. `runs.py` retains
   existing Strava commands, auth/refresh/pagination and provides optional safe
   rate metadata plus accepted scope/authenticated-athlete-ID retention. Storage
   adds only new tables; raw archive permissions and SQLite serialization stay
   in the existing Store. README documents runtime configuration and limitations.

4. **Fixtures.** Tests use `FixtureGarmin`, `make_fit`, `trace_fit`, and scripted
   loopback HTTP. Demonstration evidence uses only `demo_recordings.examples()`:
   rich trace, trace without timer events, and summary-only FIT. Source and
   identity ambiguity, reimport, correction, empty response and outage fixtures
   are generated locally. No credential files, private histories or real FITs
   were read. The UI server used an isolated temporary database on port 8028.

5. **Tests.** `PYTHONPATH=. .venv/bin/python -m pytest tests -q` passed with
   **319 passed, 5 skipped** on the initial implementation base. The five skipped
   tests are browser replays requiring Chrome on PATH. After merging main
   `e7b05cd834914daa9226750b605a867444d60948` (context ingestion), the suite passed
   **368 tests with 6 browser skips**. The focused mode/provider
   suites passed **98 tests**. `git diff --check` passed. Regression sensitivity
   was proved by temporarily making the denied adapter load settings: the
   before-fetch spy test failed. Temporarily removing the stale revision guard
   made the persisted configuration/correction test fail at HTTP 200 versus 409.
   Both mutations were restored before the final suite. No new lint/type-check
   command is configured by the repository.

6. **Actual API and CLI.** `test_actual_cli_config_status_and_import_with_loopback_server`
   starts the real Uvicorn entry point, runs `provider_cli.py` as a subprocess,
   saves Strava-only, submits a date range, reads the blocked persisted attempt,
   and rejects stale/invalid requests. The separate real port-8028 server returned
   HTTP 202 for three synthetic FIT uploads; `provider_cli.py --port 8028 status`
   showed persisted revisions, `GARMIN_OFFLINE`, `POLICY_BLOCKED`, and tiers C/B/A.
   Public API integration tests execute the actual Garmin request/parsing/import
   path against scripted bytes and cover all modes, conflicts, origin protection,
   invalid dates, unknown attempt IDs and read-only status. No live private calls.

7. **Actual UI.** Chrome CUA inspected the own port-8028 tab at 1280×850 and
   375×850. Saved Strava-only with Enter, reloaded to verify persistence, submitted
   dates and saw an unavailable result. Saved Garmin-primary fallback on mobile,
   submitted with Enter, saw explicit offline cause and blocked secondary branch,
   then opened Source import JSON. Mobile `scrollWidth=375`, `innerWidth=375`;
   console warn/error log was empty. Screenshots captured desktop source controls
   and mobile fallback/coverage. After synthetic API FIT upload, the Sources page
   visibly listed C/B/A and followed Activity 3 by keyboard to `INSUFFICIENT`,
   `NOT_PROVIDED`, zero original records. Viewport override was reset. A native
   Playwright attempt was bounded at 25 seconds because the local Chrome page
   creation stalled; it did not supply passing browser evidence. The committed
   browser replay must pass on the final CI head.

8. **Lineage and replay.** Each attempt freezes its configuration revision,
   observation states, range/coverage and canonical version IDs before another
   import can correct the heads. G1 raw source checksums, parser versions, provider
   IDs, creation/selection times and source corrections remain immutable. Reimport
   reuses the same canonical version; selection A→B→A creates new mode history
   without editing prior choices. The mode API is read-only on GET. Interrupted
   attempts expose incomplete evidence; they never turn into fabricated complete
   empty history. This supplies G6 inputs; it does not implement snapshot replay
   or retroactively claim prospective predictions.

9. **External/manual/scientific limits.** Official [API Policy](https://www.strava.com/legal/api_policy)
   and [API Agreement](https://www.strava.com/legal/api) were rechecked on
   25 November 2026; the intended persistent combined analytics remains blocked
   in #33. [OAuth scope rules](https://developers.strava.com/docs/authentication/),
   [pagination](https://developers.strava.com/docs/),
   [REST reference](https://developers.strava.com/docs/reference/) and
   [rate-limit headers](https://developers.strava.com/docs/rate-limits/) informed
   compatibility checks. Successful authentication is not an analytics exception.
   No Strava/MCP/alternate-route access, request for credentials or provider
   contact occurred. Live Garmin authorization/reliability was not newly tested;
   functional export and synthetic REST evidence is recorded separately. Manual
   watch comparisons, physiological validation, Tier D contextual evaluation,
   planner A23/M08 and G6 prospective evidence are not claimed. No runtime athlete
   approvals, mileage decisions, deployment or release are changed.

10. **Review and CI.** Independent review and passing final-head CI are merge
    requirements; this report records author evidence and does not self-approve.
    Initial PR #37 CI passed 323 tests but failed the new browser replay because
    the test reloaded/navigated before asynchronous mode saving completed. The
    replay now waits for the save response and navigation, retaining strict
    request-failure and console checks. The merged-main replay also follows the
    shared Context/Sources navigation. A fresh CUA integration check saved Both,
    navigated to Context, saved a synthetic wellness observation with exact
    `47.12345678901234567890` precision, and returned to Sources with the saved
    mode and all C/B/A evidence intact; console warn/error logs remained empty.
    No browser viewport override was needed for this integration check.
    The linked PR conversation/checks carry the final SHA, independent verdict,
    CI run and any base-integration fixes. The coordinator owns the merge and
    records final proof in parent #15. External #33 remains open regardless of
    supported-source software completion.
