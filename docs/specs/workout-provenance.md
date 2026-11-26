# Workout labels and effort evidence

Issue #30 implements master Part A §§6.2–6.3, 8.2, 11.1 and 12, and the
planned/actual distinction in APE-WK-01–04 and Part C §6.3. It does not prescribe
workouts, determine physiological zones or alter G2 geometry/QC.

## Separate records

`workout_labels` is append-only. Each activity has independent `planned`,
`inferred` and `corrected` histories. Entries retain canonical version, actor,
explanation, source/reference, event UTC, server-assigned known-at UTC, previous
entry, classifier/config/code version and copied evidence. Effective actual type
uses the latest eligible correction, otherwise inference; planned intent does not
silently become an actual classification. UNKNOWN is an editable classification,
not an inferred rest day. BENCHMARK_TAG and marathon contexts can be secondary tags.

The configured initial mapping recognizes the canonical taxonomy strings in
**completed** FIT step names. A recorded lap must link its `wkt_step_index` to the
step's `message_index` and have positive recorded elapsed time. Both indices must
exist and identify exactly one step. The FIT selected flag is separate from the
12-bit index; missing/invalid links and duplicate step identities remain UNKNOWN
with raw-row diagnostics, including identical duplicate names. A source title or
unexecuted step is only a hint. Mixed mapped categories stay UNKNOWN. This is a
deterministic software-label rule, with no pace/HR/RPE or physiological cutoffs.
The API and UI support immutable custom mappings. Original lap/step/session fields,
message indices and FIT checksums remain inspectable; raw bytes are never changed.

Source changes are visible rather than silently relabeling prior observations.
Inference is explicitly recomputed from the current canonical version. Same
evidence/config/code is idempotent. Corrections require both the current canonical
version and the current layer's `supersedes` ID. Aliases resolve to the current
owner while each entry retains its activity/canonical ID at entry. Note-only edits
prefill and preserve the selected revision, including planned-session and supporting
effort references. Effort edits preserve category, protocol, qualification and the
complete source/document/attestation list. Editable lists and explicit removal
controls change references only when requested. Historical
identity reconstruction is not asserted from a present-day redirect table.

Strength remains a separate context modality with category STRENGTH_CONDITIONING.
Running-label writes cannot turn a recorded run into strength or change its totals.

## API and import

- `GET /api/activities/{id}/workout`: layers, effective classification, history,
  source-change state and input version. Optional `as_of` filters event times only;
  `knowledge_as_of` filters known-at times only. Neither supplies an omitted other
  cutoff. Supply both for both constraints. A future-dated but already-known record
  remains visible with only a knowledge cutoff. Later entries appear as excluded
  IDs/reasons; the two constraints apply identically to workout and effort views.
- `POST /api/activities/{id}/workout`: append planned/corrected label. Required:
  layer, canonical_version_id, primary, actor, explanation, source,
  source_reference. Existing layer updates also require supersedes. Optional:
  tags, event_utc, planned_session_ref, supporting_efforts. Each supporting effort
  is an owned `{version_id,candidate_id}` pair whose immutable geometry and raw
  source reference are copied into the label.
- `POST /api/activities/{id}/workout/infer`: current canonical_version_id and
  optional config_version. This is an explicit write; GET never backfills.
- `POST /api/workouts/import`: multipart `document`, one UTF-8 JSON object with
  `activity_id` and `label` using the same schema. The exact bytes are archived and
  referenced by SHA-256. Duplicate JSON keys/nonfinite values are rejected. A
  rejected import may leave an unreferenced immutable archive object, never a
  partially committed label. The limit is 100 KB per document.
- `GET /api/workouts/configs`, `POST /api/workouts/configs/{version}` with
  `{"completed_step_names":{"My tempo step":"TEMPO_THRESHOLD"}}`.
- `GET /api/workouts/changes?after=<UTC>`: immutable WORKOUT_LABEL and EFFORT_CONTEXT
  change events with affected activity and new input version. Consumers rebuild
  current contextual/estimator outputs with those versions and retain historical
  copied inputs. This feature does not fabricate an unavailable downstream model.

The browser supplies entry/correction, inference, JSON upload, configuration and
history controls. The existing loopback origin protection applies to every POST.
Unknown IDs return 404, malformed input 422 and stale source/revision writes 409.

## One effort-context ledger

G2 `effort_contexts` remains the only effort-declaration ledger. The new writer
appends the original category/tags/protocol/maximal flag together with explicit
qualification, immutable candidate payload, evidence, event time and code hash.
The POST acknowledgement names the context just committed, inside the same
serialized transaction. A later concurrent writer cannot replace that response.
Existing G2 source files `efforts.py` and `effort_math.py` are byte-identical to the
merged G2 implementation, preserving extraction hashes, IDs and reviewed exclusions.

`provenance.qualification` adds schema_version=effort-qualification-v1,
context_id, known_at, actor, explanation, evidence_refs, evaluation/declaration
code hashes, and these independent fields:

| Field | Values |
| --- | --- |
| maximality | CONFIRMED, NOT_MAXIMAL, UNKNOWN |
| rest_state | RESTED, FATIGUED, UNKNOWN |
| control_state | CONTROLLED, UNCONTROLLED, UNKNOWN |
| race_distance_verification / race_time_verification | ATTESTED, DOCUMENTED, UNKNOWN |
| test_evidence_status / race_evidence_status | SUPPORTED_ATTESTATION, UNVERIFIED, CONFLICTED |

Missing fields stay UNKNOWN. Legacy false maximal confirmation means unconfirmed,
never explicitly NOT_MAXIMAL. No rest/fatigue/control state comes from a title,
prior distance or unstated numerical policy. Recorded cumulative start distance is
exposed separately as preceding-work context with its exact G2 coordinate.

Supported test evidence requires the VERIFIED_TEST declaration, maximal
confirmation, protocol, rested OR controlled context, and evidence references.
Supported race evidence requires the RACE declaration, distance AND elapsed-time
verification and evidence. DOCUMENTED requires a document or owned source-message
reference. Race maximality remains independent; a new explicit race declaration
may confirm it without turning the race into a rested controlled test. Contradictory
rested/fatigued declarations are CONFLICTED. Legacy/basic declarations remain
inspectable with UNVERIFIED qualification. Trace QC/rejection is always separate.

Evidence refs carry kind, method, and a document/attestation reference or an owned
raw FIT checksum/message group/original index. Source refs are checked against
available archived bytes. References are recorded, not fetched or independently
certified. Neither DOCUMENTED nor SUPPORTED_ATTESTATION implies scientific validity.

## Downstream integration

The G2 HTTP presenter enriches candidates without changing G2 extraction identity.
Python consumers use:

```python
enrich_candidate(db, candidate, extraction_version_id, activity_id,
                 as_of=event_cutoff, knowledge_as_of=knowledge_cutoff)
```

from `workout_labels`, within their existing serialized read transaction. It returns
a copy with selected context/history, qualification, session-classification versions,
preceding-work context and `context_input_version`. Copy the entire returned input
into an immutable evaluation; never resolve today's labels when reading that saved
evaluation. G2's selection_id still describes its G2 selection, not the whole new
context snapshot. Changing labels changes context_input_version, not candidate IDs.

Explicit effort-inspection API/pages accept as_of and knowledge_as_of for declarations
and reviewer records independently. Context event time is the effort observation;
the original review ledger records its decision instant as both event and known-at
time. Excluded context, review and label IDs/reasons are inspection diagnostics and
do not enter the selected input fingerprint. These views do not claim to freeze source availability, identity or
extraction knowledge. G6 owns that complete prospective protocol. These metadata
changes cannot establish calibrated prediction errors, injury risk or medical clearance.
