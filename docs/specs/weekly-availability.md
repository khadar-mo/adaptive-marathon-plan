# Weekly availability — issue #38

## 1. Authority and scope

Master v1.0 Part 3, Part B §§3–4/8.1 and Part C §§1–2/7 define weekly windows,
London boundaries and explicit same-day permission. Source DOCX SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
This independent calendar-input issue began at main `59faa54`. It supplies #19/#23;
it does not implement target approval, allocation or plan activation.

## 2. Runner input

Open **Context → Weekly availability**, choose a Monday and enter an actor.
Each of seven days is `UNKNOWN`, `UNAVAILABLE` or `AVAILABLE`. Available days
require actual windows and a positive session limit; a limit above one requires
explicit doubles permission. Unknown days have a null limit; unavailable days
have a zero limit and no windows.

An unsubmitted week is `NOT_ENTERED`. Submitted all-unavailable weeks are `EMPTY`,
all-unknown weeks `UNKNOWN`, and partly unknown weeks `PARTIAL`. Missing days do
not become rest days. Saving a calendar does not approve kilometres or activate a plan.

## 3. Clock and interval policy

`availability-london-start-day-v1` uses Europe/London Monday 00:00 through the next
Monday 00:00, end excluded. Actual UTC bounds preserve 167/169-hour DST weeks.
Every boundary retains submitted local text, offset/fold and its resolved local
time, UTC instant, offset and fold. Round-trip validation rejects spring gaps;
autumn ambiguity requires an explicit occurrence or matching offset. Two supplied
choices must agree; a nonexistent second occurrence is rejected.

Windows start on their declared day, have positive UTC duration and stay within
the week. Overnight windows are allowed. Daily limits follow the session's London
start date: an overnight window does not grant a new start on a zero/unknown-cap day.
Original windows remain intact; a global union merges overlapping/touching intervals
with contributing IDs and counts each instant once. Duration is available time,
not demonstrated training or distance capacity.

## 4. Immutable storage and retry contract

`availability_revisions` stores immutable payloads with previous revision, actor,
server receipt/knowledge time, original/resolved fields and policy version.
Writes require `expected_revision` (null initially) and `request_id`. An identical
retry returns its original record even after later edits. Changed content under
the same key or a stale edit returns 409. The existing serialized SQLite transaction
makes competing writes atomic. GETs never initialize/migrate storage.

Engineering limits: seven days, 32 windows/day, session limits 0–10, 256 KiB JSON,
bounded actor/note fields. These are input limits, not coaching recommendations.
Duplicate keys, nonfinite numbers, wrong types, unknown fields and spoofed server
timestamps are rejected.

## 5. Public interfaces and planner handoff

| Entry | Contract |
|---|---|
| `GET /api/availability/{Monday}` | Current revision, history index, missingness. |
| `POST /api/availability/{Monday}` | Append or return identical retry. |
| `GET /api/availability/{Monday}/versions/{version_id}` | Exact immutable payload scoped to the week. |
| `/availability?week={Monday}` | Current editor; optional `version` selects read-only history. |

POST fields: `request_id`, `expected_revision`, `actor`, optional `reason`, and seven
`days`. Each day supplies `date`, `state`, `max_sessions`, `doubles_allowed`, `windows`.
Each window has `start` and `end`: `{local: "2026-10-25T01:15", fold: 0, offset: null}`.
The example boundary becomes 00:15 UTC; fold 1 becomes 01:15 UTC. Unknown/unavailable
days have no windows. The API returns original and resolved fields separately.

`read_version(db, week, version_id)` accepts the caller's existing transaction and
resolves no current head or external source. #19 freezes that revision in its event
and checks `known_at` against the decision cutoff. `read_current` is explicitly a
current selector, not a historical-known-at query.

## 6. Acceptance criteria

**AC-1:** “Enter, inspect and revise availability for a Europe/London ISO week.
Record actual date/time windows, the daily limit on sessions, and whether doubles
are permitted. Distinguish a week the runner has never filled in from an explicit
empty week. The form should be reachable from the existing weekly readiness or
context screen.”

Proof: missing/partial/empty API cases; explicit cap/permission tests; actual browser
Context navigation, five-day entry, three-day revision and empty week.

**AC-2:** “Preserve each submitted London date, UTC offset or fold, and resolved
UTC bounds. Handle the spring clock gap and both occurrences of a repeated autumn
time explicitly. Make overlapping windows usable as a union so their time is
counted once.”

Proof: independently specified UTC intervals, 167/169-hour weeks, offset/fold
conflicts, reversed local clocks with positive UTC duration, overnight/week-end
boundaries and overlapping/touching-window union fixtures.

**AC-3:** “Keep immutable weekly revisions with the previous revision, actor and
server receipt time. A stale edit returns a conflict and an identical retry is
idempotent. Provide an exact-version read for the planner and a current-version
read for the UI.”

Proof: old payload equality after edits/restart, actor/time linkage, concurrent
retry and conflicting writes, stale browser tab, cross-week version rejection,
escaped actor and byte-identical storage after GETs.

**AC-4:** “This feature owns the calendar input. #19 attaches the selected revision
to the planner event and #23 accounts for occupied time, completed sessions and
exact weekly metres. Editing the calendar here creates a new calendar revision.”

Proof: payloads declare `CALENDAR_REVISION_ONLY`; no plan/target actions exist.
An existing-format synthetic training database gains the additive table without
changing canonical activity data or its measured distance.

**AC-5:** “Verify a normal browser edit from five to three days, an empty week,
overlapping windows, DST boundaries, prior-version inspection and a stale edit.
Exercise the first working route against an independently calculated set of UTC
intervals. Include ordinary API/browser evidence and a focused acceptance report.
Use synthetic inputs.”

Proof: the independent check and browser replay below; all inputs are synthetic.

## 7. Automated and causal checks

`PYTHONPATH=. .venv/bin/python -m pytest tests/test_availability.py tests/test_availability_browser.py -q`:
**33 passed**, including real Chromium. Chrome/Chromium must be on PATH; Linux CI
requires browser replays. In-memory mutations that counted overlaps separately
and ignored the second clock occurrence each failed their public API regression.
Neither mutation changed a source file. `git diff --check` passed.
Final broad-suite and CI evidence is recorded in the PR.

## 8. First runnable commit

Real Uvicorn/HTTP checks ran at `cef3d912115a40905f388e57935904a8147f0d08`.
The coordinator independently tested that immutable commit: spring 01:30 was
rejected without a revision; autumn 01:15 first occurrence to 01:45 second was
5,400 seconds; overlapping windows were 7,200 seconds; an empty week retained the
old version; stale editing returned 409.

## 9. Actual browser evidence

The replay enters five days with overlapping windows and explicit doubles, revises
to three days, inspects the old read-only version and records an empty week. A stale
tab receives 409 without overwriting it. Spring-gap and ambiguous-autumn errors
remain visible; corrected submissions produce 3,600 and 5,400 seconds.
At 1280px/375px, keyboard submission/navigation and expanded source details work
without page overflow. No JavaScript errors or failed requests occurred; the main
browser's only HTTP errors were the two deliberately asserted 422 clock errors.

## 10. Limits and delivery

Calendar input is implemented subject to final independent review and green CI.
This is not P0/P4 completion, an approved dose, a feasible schedule or a health claim.
No live calendars, private data, credentials, deployment, release or scientific
validation are involved. The coordinator owns the final merge.
