# Completed-run fueling evidence

Issue #39 implements the independent completed-activity portion of Master v1.0
L15, Part B §12 APE-FUEL-01–03 and Part C §10.2/M30. Master SHA-256:
`8562073c94131ff70e6848a507d8655e399bd00d42d6c6910d6f41da20ad8d87`.
Planned-session integration remains #25. This feature makes no intake recommendation,
fitness/readiness decision, running-distance adjustment or physiological claim.

From an activity, choose **Record fueling practice and tolerance**. A saved report
has source/record ID, supplied actor label, report timestamp, optional observation
time, optional strategy and notes, and optional GI tolerance text with its own report
timestamp. Status is exactly `NOT_PLANNED`, `OFFERED`, `PLANNED`, `COMPLETED` or
`UNKNOWN`. No entry means unknown practice and tolerance, not an invented completion
or bad readiness. A supplied tolerance is labelled `REPORTED`, never inferred as good
or poor. Research status is fixed to `RESEARCH_PENDING`.

Text is retained verbatim, including the runner's quantities, timing, whitespace
and Unicode. Report/observation offsets remain as supplied. Notes use the report
timestamp. Observation and tolerance-report times cannot follow that report time.
The actor is a supplied provenance label, not an authenticated identity. Server receipt
and correction availability cannot be supplied/backdated by a client.

## Public interface

| Endpoint | Contract |
|---|---|
| `GET /api/activities/{id}/fueling?as_of=...` | Optional current/known-at view, including missingness and individually retained reports |
| `POST /api/activities/{id}/fueling` | A JSON record or atomic batch, same context-export-v1 shape below |
| `POST /api/context/records` or `/api/context/import?format=json|csv` | Existing entry/import interface also accepts `kind=fueling` |
| `GET /api/fueling/versions/{version_id}?as_of=...` | One exact immutable report revision, if known by the cutoff |
| `GET /api/context/records/{record_id}` | All revisions, reasons, raw receipts and source references |
| `GET /api/context/events` | Existing append-only feed includes `FUELING_PRACTICE` events |

```json
{
  "kind": "fueling",
  "activity_id": 1,
  "source": "self-report",
  "source_id": "synthetic-run-note-1",
  "actor": "Synthetic runner",
  "reported_at": "2026-10-25T14:00:00+00:00",
  "practice_status": "COMPLETED",
  "user_strategy": "My own strategy, unchanged.",
  "tolerance": "Comfortable during this run",
  "tolerance_reported_at": "2026-10-25T13:00:00+00:00",
  "notes": "Synthetic example only."
}
```

`observed_at`, `source_updated_at`, `notes`, `user_strategy` and tolerance are optional.
Missing tolerance and timestamp remain null. Empty supplied text is retained; blank
form inputs are omitted. The existing 2 MiB / 1–1000-record limits and exact parsing
apply. Strategy and notes allow 10000 characters each, GI tolerance 5000. These are
storage limits, not nutrition rules. All fields are validated; unknown fields and
research-status overrides return 422. Activity IDs are positive signed-64-bit integers.

## Identity, corrections and planned-session handoff

The record identity is `(kind, source, source_id)`. Duplicate source bytes replay
the original receipt; normalized unchanged input adds no revision/event. Correct
with the current `supersedes` version and a reason; stale changes return 409. A
correction cannot retarget the record to a different activity. It preserves the first
saved activity reference, canonical version and raw source references. Current donor
redirects group records without rewriting their entry references. Source reclassification
does not delete old evidence or prevent correcting it. New entries require a recorded
running activity and reject a known future end; incomplete legacy timing remains
explicitly UNKNOWN instead of inventing a completion instant. No long-run distance
threshold or classification is invented to limit entry.

`target_now` is a current identity display aid, explicitly not historical evidence.
An as-of read selects versions by server receipt time and report time. Exact-revision
inspection retains a labelled eligibility flag even for a report with later event time.
Multiple reports remain separate; no strategy/tolerance is silently chosen or averaged.

For #25, retain the `record_id` plus exact `version_id` in the future planned-session
link. This input does not assign a planned session or authorize a fueling protocol.
Context tables and archives are reused without altering canonical activities,
effort extraction, workout labels or numerical training history.
