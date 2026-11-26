"""Append-only workout labels and additive G2 context enrichment."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

import canonical
import context_data
import effort_qualification as qualification
from fit_data import read_messages
from recording import json_safe


ALGORITHM_VERSION = "workout-labels-v1"
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
TYPES = ("UNKNOWN", "EASY_AEROBIC", "LONG_RUN", "TEMPO_THRESHOLD", "THRESHOLD_INTERVALS", "FASTER_INTERVALS", "MARATHON_SPECIFIC", "RECOVERY_EASY", "STRENGTH_CONDITIONING")
DEFAULT_CONFIG_VERSION = "workout-labels-v1"
DEFAULT_CONFIG = {"completed_step_names": {name: name for name in TYPES if name not in {"UNKNOWN", "STRENGTH_CONDITIONING"}}}
SCHEMA = """
CREATE TABLE IF NOT EXISTS workout_configs (
 version TEXT PRIMARY KEY, payload TEXT NOT NULL, known_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workout_labels (
 id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL, layer TEXT NOT NULL,
 canonical_version_id TEXT NOT NULL, previous_id TEXT REFERENCES workout_labels(id),
 event_utc TEXT, known_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workout_label_history ON workout_labels(activity_id,layer,known_at);
CREATE TABLE IF NOT EXISTS workout_changes (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, activity_id INTEGER NOT NULL,
 version_id TEXT NOT NULL, event_utc TEXT, known_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class Conflict(ValueError):
    pass


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["planned", "corrected"]
    canonical_version_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    primary: str
    tags: list[str] = Field(default_factory=list, max_length=20)
    actor: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=4000)
    source: str = Field(min_length=1, max_length=120)
    source_reference: str = Field(min_length=1, max_length=2000)
    event_utc: str | None = None
    supersedes: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    planned_session_ref: str | None = Field(None, max_length=200)
    supporting_efforts: list[dict] = Field(default_factory=list, max_length=40)

    @field_validator("primary")
    @classmethod
    def category(cls, value):
        if value not in TYPES:
            raise ValueError("Choose a supported workout type; benchmark is a secondary tag.")
        return value

    @field_validator("tags")
    @classmethod
    def tags_valid(cls, value):
        if any(not tag.strip() or len(tag) > 80 for tag in value):
            raise ValueError("Use nonempty tags of at most 80 characters.")
        return list(dict.fromkeys(tag.strip() for tag in value))

    @field_validator("actor", "explanation", "source", "source_reference")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Required text cannot be blank.")
        return value.strip()


def exists(db):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='workout_labels'").fetchone() is not None


def owner(db, activity_id):
    import efforts
    return efforts.owner(db, activity_id)


def owner_ids(db, aid):
    return [aid, *[r[0] for r in db.execute("SELECT old_id FROM activity_redirects WHERE activity_id=?", (aid,))]]


def head(db, aid):
    row = db.execute("SELECT v.id,v.payload FROM canonical_activity_heads h JOIN canonical_activity_versions v ON v.id=h.version_id WHERE h.activity_id=?", (aid,)).fetchone()
    if row is None:
        raise ValueError("Recompute canonical history before classifying this activity.")
    return row["id"], json.loads(row["payload"])


def register_config(store, version, config):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version) or not isinstance(config, dict) or set(config) != {"completed_step_names"}:
        raise ValueError("Use a version name and a completed_step_names mapping.")
    mappings = config["completed_step_names"]
    if not isinstance(mappings, dict) or len(mappings) > 100 or any(not isinstance(k, str) or not k.strip() or len(k) > 120 or v not in TYPES or v == "STRENGTH_CONDITIONING" for k, v in mappings.items()):
        raise ValueError("Map at most 100 exact completed step names to running categories.")
    with store.connect(write=True, create=True) as db:
        old = db.execute("SELECT payload FROM workout_configs WHERE version=?", (version,)).fetchone()
        if (old and json.loads(old[0]) != config) or (version == DEFAULT_CONFIG_VERSION and config != DEFAULT_CONFIG):
            raise Conflict("Configuration versions are immutable; choose another version.")
        db.execute("INSERT OR IGNORE INTO workout_configs VALUES (?,?,?)", (version, context_data.encoded(config), canonical.knowledge_time()))
    return {"version": version, "config": config}


def configs(store):
    result = [{"version": DEFAULT_CONFIG_VERSION, "config": DEFAULT_CONFIG, "known_at": None}]
    with store.history() as db:
        if exists(db):
            result += [{"version": r["version"], "config": json.loads(r["payload"]), "known_at": r["known_at"]} for r in db.execute("SELECT * FROM workout_configs WHERE version!=? ORDER BY version", (DEFAULT_CONFIG_VERSION,))]
    return result


def record_change(db, kind, aid, version, event_utc, known_at, payload):
    cid = canonical.fingerprint({"kind": kind, "version": version})
    data = {**payload, "affected_activity_id": aid, "action": "RECOMPUTE_CONTEXT_AND_ESTIMATORS_WITH_NEW_INPUT_VERSION", "historical_results": "RETAIN_COPIED_INPUTS"}
    db.execute("INSERT OR IGNORE INTO workout_changes VALUES (?,?,?,?,?,?,?)", (cid, kind, aid, version, event_utc, known_at, context_data.encoded(data)))
    return cid


def latest_id(db, ids, layer):
    row = db.execute("SELECT id FROM workout_labels WHERE activity_id IN (SELECT value FROM json_each(?)) AND layer=? ORDER BY rowid DESC LIMIT 1", (json.dumps(ids), layer)).fetchone()
    return row[0] if row else None


def append(db, aid, canonical_id, layer, payload, event, known, previous):
    payload = {**payload, "algorithm_version": ALGORITHM_VERSION, "code_sha256": CODE_SHA256,
               "activity_id_at_entry": aid, "canonical_version_id": canonical_id}
    identity = {"payload": payload, "event_utc": event, "known_at": known, "previous_id": previous, "layer": layer}
    vid = canonical.fingerprint(identity)
    db.execute("INSERT INTO workout_labels VALUES (?,?,?,?,?,?,?,?)", (vid, aid, layer, canonical_id, previous, event, known, context_data.encoded(payload)))
    record_change(db, "WORKOUT_LABEL", aid, vid, event, known, {"layer": layer, "previous_id": previous})
    return {"id": vid, **identity}


def evidence(store, db, aid, canonical_id, payload):
    activity = canonical.activity_input(db, store.archive, aid)
    result = {"canonical_version_id": canonical_id, "title": activity.get("name"), "raw_sources": payload["raw_sources"], "fit": [],
              "title_policy": "Source title is a hint, not proof of completed structure or effort."}
    for source in payload["sources"]:
        raw = canonical.source_bytes(store.archive, source)
        entry = {"fit_sha256": source["fit_sha256"], "source_version_id": source["source_version_id"], "status": "UNAVAILABLE", "messages": []}
        if raw is not None:
            messages = read_messages(raw)
            entry["status"] = "AVAILABLE"
            for group in ("workout_mesgs", "workout_step_mesgs", "lap_mesgs", "session_mesgs"):
                entry["messages"] += [{"message_group": group, "index": i, "fields": json_safe(fields), "raw_sha256": source["fit_sha256"]} for i, fields in enumerate(messages.get(group, []))]
        result["fit"].append(entry)
    return result


def step_index(value):
    # Both FIT fields use message_index: the low 12 bits identify the row;
    # selected is a flag, while the reserved bits cannot establish an identity.
    if type(value) is not int or not 0 <= value < 65535 or value & 0x7000:
        return None
    return value & 0x0fff


def infer(store, activity_id, *, canonical_version_id, config_version=DEFAULT_CONFIG_VERSION):
    with store.connect(write=True, create=True) as db:
        aid = owner(db, activity_id)
        current, payload = head(db, aid)
        if canonical_version_id != current:
            raise Conflict("The activity source changed; reload before classifying it.")
        config_row = db.execute("SELECT payload FROM workout_configs WHERE version=?", (config_version,)).fetchone()
        config = json.loads(config_row[0]) if config_row else DEFAULT_CONFIG if config_version == DEFAULT_CONFIG_VERSION else None
        if config is None:
            raise ValueError("Unknown classification configuration.")
        source = evidence(store, db, aid, current, payload)
        matches, diagnostics = [], []
        # A planned workout step alone is not evidence it was executed. Match only
        # recorded lap links with positive elapsed time, preserving every raw index.
        selected = next((s for s in source["fit"] if s["fit_sha256"] == payload["selected_fit_sha256"]), None)
        if selected and payload['running_sport']:
            steps = {}
            for row in (r for r in selected['messages'] if r['message_group'] == 'workout_step_mesgs'):
                index = step_index(row['fields'].get('message_index'))
                if index is None:
                    diagnostics.append({'reason':'MISSING_STEP_INDEX' if row['fields'].get('message_index') is None else 'INVALID_STEP_INDEX','raw_references':[row]})
                else:
                    steps.setdefault(index, []).append(row)
            for index, rows in steps.items():
                if len(rows) != 1:
                    diagnostics.append({'reason':'AMBIGUOUS_STEP_INDEX','step_index':index,'raw_references':rows})
            for lap in (r for r in selected["messages"] if r["message_group"] == "lap_mesgs"):
                fields = lap["fields"]
                index = step_index(fields.get('wkt_step_index'))
                if index is None:
                    diagnostics.append({'reason':'MISSING_LAP_STEP_INDEX' if fields.get('wkt_step_index') is None else 'INVALID_LAP_STEP_INDEX','raw_references':[lap]})
                    continue
                linked = steps.get(index, [])
                if not linked:
                    diagnostics.append({'reason':'STEP_INDEX_NOT_FOUND','step_index':index,'raw_references':[lap]})
                    continue
                if len(linked) != 1:
                    continue
                step = linked[0]
                if type(fields.get("total_elapsed_time")) in (int, float) and fields["total_elapsed_time"] > 0:
                    name = step["fields"].get("wkt_step_name")
                    if name in config["completed_step_names"]:
                        matches.append({"primary": config["completed_step_names"][name], "step": step, "lap": lap})
        categories = sorted({r["primary"] for r in matches})
        supported = len(categories) == 1 and not diagnostics
        value = {"primary": categories[0] if supported else "UNKNOWN", "tags": [], "status": "MAPPED_SOURCE_LABEL" if supported else "UNKNOWN",
                 "explanation": "Incomplete or ambiguous executed-step links; see original rows and link diagnostics." if diagnostics else "Exact configured names on steps linked by completed laps; these are software labels, not calibrated intensity." if matches else "No supported completed-step mapping. Titles and pace do not establish a workout category.",
                 "actor": "classifier", "config_version": config_version, "config": config, "evidence": source, "matches": matches,
                 "link_diagnostics": diagnostics,
                 "conflicting_categories": categories if len(categories) > 1 else [], "physiological_validation": "UNVERIFIED"}
        previous = latest_id(db, owner_ids(db, aid), "inferred")
        if previous:
            prior = json.loads(db.execute("SELECT payload FROM workout_labels WHERE id=?", (previous,)).fetchone()[0])
            if all(prior.get(k) == v for k, v in value.items()) and prior["canonical_version_id"] == current and prior["code_sha256"] == CODE_SHA256:
                return read_in_db(db, aid)
        append(db, aid, current, "inferred", value, payload["summary"]["end_utc"], canonical.knowledge_time(), previous)
        return read_in_db(db, aid)


def save(store, activity_id, raw, *, import_sha256=None):
    import efforts
    choice = Label.model_validate(raw)
    with store.connect(write=True, create=True) as db:
        aid = owner(db, activity_id)
        current, canonical_payload = head(db, aid)
        if choice.canonical_version_id != current:
            raise Conflict("The activity source changed; reload before saving this label.")
        previous = latest_id(db, owner_ids(db, aid), choice.layer)
        if choice.supersedes != previous:
            raise Conflict("This label changed; reload and review the latest revision.")
        if choice.primary == "STRENGTH_CONDITIONING" or not canonical_payload['running_sport']:
            raise ValueError("Record strength in Context; a workout label cannot reclassify a recorded run's modality.")
        refs = []
        for ref in choice.supporting_efforts:
            if not isinstance(ref, dict) or set(ref) != {"version_id", "candidate_id"} or not all(isinstance(x, str) and re.fullmatch(r"[a-f0-9]{64}", x) for x in ref.values()):
                raise ValueError("Supporting efforts need an extraction version_id and candidate_id.")
            result = efforts.load(db, aid, ref["version_id"])
            candidate, source = efforts.find_candidate(db, result, ref["candidate_id"])
            refs.append({**ref, "source_sha256": source["fit_sha256"], "canonical_version_id": result["canonical_version_id"], "candidate": deepcopy(candidate)})
        value = choice.model_dump(exclude={"supersedes", "event_utc", "layer"})
        value.update(supporting_efforts=refs, classification_basis="EXPLICIT_RUNNER_DECLARATION", evidence=evidence(store, db, aid, current, canonical_payload), import_sha256=import_sha256)
        event = context_data.moment(choice.event_utc) if choice.event_utc else canonical_payload["summary"]["end_utc"]
        known = canonical.knowledge_time()
        saved = append(db, aid, current, choice.layer, value, event, known, previous)
        return {"saved": saved, "workout": read_in_db(db, aid, knowledge_as_of=known)}


def read_in_db(db, activity_id, *, as_of=None, knowledge_as_of=None):
    aid = owner(db, activity_id)
    event_cutoff = context_data.moment(as_of) if as_of else None
    cutoff = context_data.moment(knowledge_as_of) if knowledge_as_of else None
    current, canonical_payload = head(db, aid)
    rows, withheld = [], []
    if exists(db):
        for row in db.execute("SELECT * FROM workout_labels WHERE activity_id IN (SELECT value FROM json_each(?)) ORDER BY rowid", (json.dumps(owner_ids(db, aid)),)):
            value = {k: row[k] for k in row.keys() if k != "payload"} | {"payload": json.loads(row["payload"])}
            excluded = exclusion(row['id'], row['event_utc'], row['known_at'], event_cutoff, cutoff)
            if excluded:
                withheld.append(excluded)
            else:
                rows.append(value)
    layers = {layer: next((r for r in reversed(rows) if r["layer"] == layer), None) for layer in ("planned", "inferred", "corrected")}
    effective = layers["corrected"] or layers["inferred"]
    selection = {layer: value["id"] if value else None for layer, value in layers.items()}
    return {"activity_id": aid, "canonical_version_id_now": current, "summary_now": canonical_payload["summary"], "layers": layers,
            "history": rows, "withheld": withheld, "effective": effective["payload"]["primary"] if effective else "UNKNOWN",
            "input_version": canonical.fingerprint(selection), "selection": selection, "as_of": event_cutoff, "knowledge_as_of": cutoff,
            "source_changed": any(r and r["canonical_version_id"] != current for r in layers.values()),
            "identity_basis": "Current alias mapping; each entry retains its original activity/canonical IDs. Legacy identity history is not asserted.",
            "physiological_validation": "UNVERIFIED", "running_totals_changed": False}


def read(store, activity_id, **kwargs):
    with store.history() as db:
        return read_in_db(db, activity_id, **kwargs)


def exclusion(identity, event_utc, known_at, event_cutoff, knowledge_cutoff):
    reasons = []
    if knowledge_cutoff and context_data.moment(known_at) > knowledge_cutoff:
        reasons.append('LATER_KNOWLEDGE')
    if event_cutoff and event_utc and context_data.moment(event_utc) > event_cutoff:
        reasons.append('FUTURE_EVENT')
    return {'id':identity,'reason':reasons[0],'reasons':reasons} if reasons else None


def enrich_candidate(db, candidate, version, activity_id, *, as_of=None, knowledge_as_of=None):
    """Enrich in the caller's existing transaction; callers freeze the returned payload."""
    c = deepcopy(candidate)
    cutoff = context_data.moment(knowledge_as_of) if knowledge_as_of else None
    event_cutoff = context_data.moment(as_of) if as_of else None
    contexts, excluded_contexts, reviews, excluded_reviews = [], [], [], []
    for row in db.execute("SELECT * FROM effort_contexts WHERE version_id=? AND candidate_id=? ORDER BY rowid", (version, c['id'])):
        value = {**json.loads(row['payload']), 'id':row['id'], 'known_at':row['known_at'], 'previous_context_id':row['previous_context_id']}
        excluded = exclusion(row['id'], value.get('event_utc') or c['end_utc'], row['known_at'], event_cutoff, cutoff)
        (excluded_contexts if excluded else contexts).append(excluded or value)
    c["context_history"] = contexts
    base = deepcopy(c.get("computed_provenance", c["provenance"]))
    c["computed_provenance"] = deepcopy(base)
    for row in db.execute("SELECT * FROM effort_reviews WHERE version_id=? AND candidate_id=? ORDER BY id", (version, c['id'])):
        # The original review ledger records the decision instant, not a separately
        # reported observation time. That instant is both event and known-at time.
        excluded = exclusion(row['id'], row['created_at'], row['created_at'], event_cutoff, cutoff)
        (excluded_reviews if excluded else reviews).append(excluded or dict(row))
    decision = next((r for r in reversed(reviews) if r["action"] != "annotate"), None)
    c["reviews"] = reviews
    c["review_verdict"] = decision["action"] if decision else "UNREVIEWED"
    c["effective_quality_state"] = "INVALID" if decision and decision["action"] == "reject" else c["computed_quality_state"]
    latest = contexts[-1] if contexts else None
    if latest:
        base.update(category=latest["category"], tags=latest["tags"], user_correction=latest["id"], known_at=latest["known_at"], protocol=latest["protocol"], verified_maximal=latest["maximal_confirmed"], scientific_validation="UNVERIFIED", declaration_basis="EXPLICIT_USER_ATTESTATION")
    base["qualification"] = qualification.read(latest)
    base["preceding_work"] = {"cumulative_start_distance_m": str(c["start_distance_m"]),
                              "exact_start_coordinate_m": c["start"].get("distance_coordinate_m"),
                              "source": "G2 recorded cumulative distance coordinate", "fatigue_inferred": False,
                              "limitation": "Recorded accumulated distance is context, not a rested/fatigued classification."}
    classification = read_in_db(db, activity_id, as_of=event_cutoff, knowledge_as_of=cutoff)
    base["session_classification"] = {k: classification[k] for k in ("layers", "effective", "input_version", "selection", "source_changed", "identity_basis")}
    base["planned_workout"] = classification["layers"]["planned"]
    base["inferred_pattern"] = classification["layers"]["inferred"]
    c["provenance"] = base
    c["context_input_version"] = canonical.fingerprint({"context": latest, "qualification": base['qualification'], "classification": base['session_classification']})
    c["context_knowledge_as_of"] = cutoff
    c['context_event_as_of'] = event_cutoff
    # Exclusions are inspection diagnostics, not selected model inputs/fingerprints.
    c['context_exclusions'] = {'contexts':excluded_contexts,'reviews':excluded_reviews,'labels':classification['withheld']}
    return c


def decorate(store, result, *, as_of=None, knowledge_as_of=None):
    """API metadata overlay; no writes and no extraction identity/code changes."""
    result = deepcopy(result)
    with store.history() as db:
        if db is None:
            return result
        def walk(value, aid=None, version=None):
            if isinstance(value, dict):
                aid = value.get("activity_id", aid)
                version = value.get("version_id", version)
                if aid is not None and version and "computed_quality_state" in value and "id" in value:
                    return enrich_candidate(db, value, version, aid, as_of=as_of, knowledge_as_of=knowledge_as_of)
                return {k: walk(v, aid, version) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, aid, version) for v in value]
            return value
        result = walk(result)
        if isinstance(result, dict):
            result['context_selection'] = {'as_of':as_of, 'knowledge_as_of':knowledge_as_of,
                'scope':'Label/context/review filtering only. Source and extraction availability are separate; this is not a frozen prediction.'}
        return result


def save_effort_context(store, activity_id, version, candidate_id, *, category, tags, actor, note, protocol=None, maximal_confirmed=False, qualification=None, supersedes=None):
    import efforts
    import effort_qualification as eq
    # Preserve the legacy declaration API; qualification is explicitly separate.
    if category not in efforts.PROVENANCE_CATEGORIES or not actor.strip() or not note.strip() or any(not tag.strip() or len(tag) > 80 for tag in tags):
        raise ValueError("Supply an effort category, nonempty actor/note and short tags.")
    q = eq.validate(qualification, maximal_confirmed)
    if maximal_confirmed:
        legacy_invalid = q is None and (category != "VERIFIED_TEST" or not protocol or not protocol.strip())
        explicit_invalid = q is not None and (category not in {"VERIFIED_TEST", "RACE"} or (category == "VERIFIED_TEST" and (not protocol or not protocol.strip())))
        if legacy_invalid or explicit_invalid:
            raise ValueError("A maximal test needs VERIFIED_TEST and protocol; an explicit race declaration keeps race verification separate.")
    with store.connect(write=True, create=True) as db:
        result = efforts.load(db, activity_id, version)
        c, source = efforts.find_candidate(db, result, candidate_id)
        if q:
            eq.validate_sources(store, result, q["evidence_refs"])
        previous = db.execute("SELECT id FROM effort_contexts WHERE version_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1", (version, candidate_id)).fetchone()
        if supersedes is not None and supersedes != (previous[0] if previous else ""):
            raise Conflict("Effort context changed; reload before correcting it.")
        known = canonical.knowledge_time()
        payload = {"category": category, "tags": list(dict.fromkeys(t.strip() for t in tags)), "actor": actor.strip(), "note": note.strip(),
                   "protocol": protocol, "maximal_confirmed": maximal_confirmed, "qualification": q, "qualification_code_sha256": eq.CODE_SHA256,
                   "event_utc": c["end_utc"], "source_sha256": source["fit_sha256"], "candidate_payload": deepcopy(c)}
        cid = canonical.fingerprint({"version": version, "candidate": candidate_id, "payload": payload, "known_at": known, "previous": previous[0] if previous else None})
        db.execute("INSERT INTO effort_contexts VALUES (?,?,?,?,?,?,?)", (cid, version, candidate_id, source["fit_sha256"], context_data.encoded(payload), known, previous[0] if previous else None))
        record_change(db, "EFFORT_CONTEXT", result["activity_id"], cid, c["end_utc"], known, {"extraction_version_id": version, "candidate_id": candidate_id})
        acknowledged = enrich_candidate(db, c, version, result["activity_id"], knowledge_as_of=known)
        acknowledged["acknowledged_context_id"] = cid
        return acknowledged


def changes(store, after=None):
    cutoff = context_data.moment(after) if after else None
    with store.history() as db:
        if not exists(db):
            return []
        return [{k: r[k] for k in r.keys() if k != "payload"} | {"payload": json.loads(r["payload"])}
                for r in db.execute("SELECT * FROM workout_changes ORDER BY known_at,id") if not cutoff or context_data.moment(r["known_at"]) > cutoff]
