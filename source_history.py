"""Query archived knowledge of acquisition and ownership without opening a second connection."""
from collections import defaultdict
import json

import workload_inputs as inputs
from source_receipts import group
from workload_processing import clock_evidence


VERSION = "source-identity-history-v1"
LAST_ROW = 2**63 - 1


def ref(kind, key, known_at, **details):
    return {"basis": kind, "id": str(key), "known_at": known_at, **details}


def source_key(s):
    return s["kind"], s["sha256"], s["format"]


def available_by(clock):
    try:
        return clock_evidence(clock)["available_by"]
    except (ValueError, OverflowError):
        return None


def legacy_groups(db, cutoff, epoch):
    """Immutable payloads establish specific earlier facts on an older store."""
    if inputs.table(db, "canonical_activity_versions"):
        cap = epoch["last_legacy_version"] if epoch else LAST_ROW
        rows = db.execute("""SELECT id,activity_id,created_at,payload FROM canonical_activity_versions
            WHERE rowid<=? AND created_at<=? ORDER BY rowid""", (cap, cutoff))
        versions = {}
        for row in rows:
            known = available_by(row["created_at"])
            payload = json.loads(row["payload"])
            value = group(row["activity_id"], payload["merged_activity_ids"], payload["provider_ids"],
                          payload["raw_sources"], payload["summary"]["start_utc"],
                          recordings=[s["fit_sha256"] for s in payload["sources"]])
            versions[row["id"]] = value
            if known and known <= cutoff:
                yield ref("CANONICAL_VERSION", row["id"], known), value
        if inputs.table(db, "canonical_selections"):
            cap = epoch["last_legacy_selection"] if epoch else LAST_ROW
            rows = db.execute("SELECT * FROM canonical_selections WHERE id<=? AND selected_at<=? ORDER BY id", (cap, cutoff))
            for row in rows:
                known = available_by(row["selected_at"])
                if known and known <= cutoff and row["version_id"] in versions:
                    yield ref("CANONICAL_SELECTION", row["id"], known,
                              version_id=row["version_id"]), versions[row["version_id"]]
    if inputs.table(db, "workload_processing_inventories"):
        # After installation each normal identity change already has a complete
        # group receipt. A local G3 inventory omits provider aliases and embedded
        # FIT members and must never replace that stronger observation.
        limit = min(cutoff, epoch["known_at"]) if epoch else cutoff
        for row in db.execute("SELECT * FROM workload_processing_inventories WHERE known_at<=? ORDER BY rowid", (limit,)):
            for activity in json.loads(row["payload"])["activities"]:
                key = ref("PROCESSING_INVENTORY", row["id"], inputs.utc(row["known_at"]))
                yield key, group(activity["activity_id"], [], None, activity["source_refs_at_observation"],
                                 activity["summary_at_observation"]["start_utc"])


def terminal_evidence(db, cutoff, epoch):
    # Both importers bind these raw references before the report reaches its
    # terminal state. Mutable upload owners and reconstructed legacy scan rows
    # cannot prove that an activity owned those bytes at that earlier instant.
    uploads = epoch["last_legacy_upload"] if epoch else LAST_ROW
    scans = epoch.get("last_legacy_scan", 0) if epoch else LAST_ROW
    if not uploads and not scans:
        return
    observed = {}
    if inputs.table(db, "workload_processing_inventories"):
        for entry in db.execute("SELECT * FROM workload_processing_inventories WHERE known_at<=? ORDER BY known_at", (cutoff,)):
            for item in json.loads(entry["payload"])["import_boundary"]:
                if item.get("finished_at"):
                    observed.setdefault((item["id"], item["finished_at"]), entry["known_at"])
    records = []
    if uploads and inputs.table(db, "fit_uploads"):
        for r in db.execute("""SELECT u.sha256,u.ordinal,i.id,i.finished_at FROM fit_uploads u
                JOIN imports i ON i.id=u.import_id WHERE u.rowid<=? AND u.sha256 IS NOT NULL
                AND i.finished_at IS NOT NULL ORDER BY i.id,u.ordinal""", (uploads,)):
            records.append((("fit", r["sha256"], "fit"), r["id"], r["finished_at"], "COMPLETED_FIT_UPLOAD",
                           f'{r["id"]}/{r["ordinal"]}', {"upload_ordinal": r["ordinal"]}))
    if scans and inputs.table(db, "garmin_scan_items"):
        rows = db.execute("""SELECT g.scan_id,g.garmin_id,g.list_sha,g.detail_sha,i.id,i.finished_at
            FROM garmin_scan_items g JOIN garmin_scans s ON s.id=g.scan_id JOIN imports i ON i.id=s.import_id
            WHERE g.scan_id<=? AND s.mode!='legacy' AND i.finished_at IS NOT NULL ORDER BY g.scan_id,g.garmin_id""", (scans,))
        for r in rows:
            for kind in ("list", "detail"):
                if sha := r[kind + "_sha"]:
                    records.append(((kind, sha, "json"), r["id"], r["finished_at"], "COMPLETED_GARMIN_SCAN",
                                   f'{r["scan_id"]}/{r["garmin_id"]}/{kind}',
                                   {"scan_id": r["scan_id"], "provider_id": r["garmin_id"]}))
    for key, iid, finish, basis, rid, data in records:
        try:
            clock = clock_evidence(finish, observed.get((iid, finish)))
        except (ValueError, OverflowError):
            continue
        if clock["available_by"] <= cutoff:
            yield key, ref(basis, rid, clock["available_by"], import_id=iid, clock=clock, **data)


def acquisition_evidence(db, cutoff, epoch):
    if inputs.table(db, "canonical_source_versions"):
        cap = epoch["last_legacy_trace"] if epoch else LAST_ROW
        rows = db.execute("""SELECT id,raw_sha256,created_at FROM canonical_source_versions
            WHERE rowid<=? AND created_at<=? ORDER BY rowid""", (cap, cutoff))
        for row in rows:
            known = available_by(row["created_at"])
            if known and known <= cutoff:
                yield ("fit", row["raw_sha256"], "fit"), ref("DECODED_SOURCE_VERSION", row["id"], known)
    yield from terminal_evidence(db, cutoff, epoch)
    if inputs.table(db, "source_acquisitions"):
        for row in db.execute("SELECT * FROM source_acquisitions WHERE known_at<=? ORDER BY known_at,id", (cutoff,)):
            yield source_key(row), {**json.loads(row["evidence"]), "id": row["id"]}


def keep_earliest(evidence, key, receipt):
    # On a tie a direct acquisition receipt, inserted first, is more specific
    # than the presence of a checksum in a group observed at that same instant.
    if key not in evidence or receipt["known_at"] < evidence[key]["known_at"]:
        evidence[key] = receipt


def group_evidence(db, cutoff, epoch):
    observations = list(legacy_groups(db, cutoff, epoch))
    if inputs.table(db, "source_group_history"):
        rows = db.execute("SELECT * FROM source_group_history WHERE known_at<=? ORDER BY sequence", (cutoff,))
        for row in rows:
            receipt = {**json.loads(row["evidence"]), "id": row["id"], "known_at": row["known_at"],
                       "previous_group_id": row["previous_id"]}
            observations.append((receipt, json.loads(row["payload"])))
    # Python's stable sort preserves recorded selection/receipt order on ties.
    return sorted(observations, key=lambda item: item[0]["known_at"])


def read_in_db(db, knowledge_cutoff, *, activity_id=None):
    """Assemble the answer for K solely from committed, dated observations."""
    cutoff = inputs.utc(knowledge_cutoff)
    epoch = None
    if inputs.table(db, "source_history_epoch"):
        row = db.execute("SELECT * FROM source_history_epoch WHERE version=1").fetchone()
        epoch = dict(row) if row else None
    groups, acquisitions, ownership = {}, {}, {}
    for key, evidence in acquisition_evidence(db, cutoff, epoch):
        keep_earliest(acquisitions, key, evidence)
    history = []
    for evidence, value in group_evidence(db, cutoff, epoch):
        owner = value["owner_id"]
        history.append({"evidence": evidence, "group": value})
        for source in value["sources"]:
            key = source_key(source)
            keep_earliest(acquisitions, key, evidence)
            keep_earliest(ownership, (owner, key), evidence)
        previous = groups.get(owner)
        identity_evidence = evidence
        if value["provider_ids"] is None:
            # G3 inventories observe local ownership only. They cannot date a
            # provider alias or disprove an already observed local merge.
            identity_evidence = previous["identity_evidence"] if previous else None
            if previous:
                value = {**value, "provider_ids": previous["provider_ids"], "member_ids": previous["member_ids"]}
        for member in value["member_ids"]:
            if member != owner:
                groups.pop(member, None)
        scope = "LOCAL_SOURCE_REFERENCES" if evidence["basis"] == "PROCESSING_INVENTORY" else "OBSERVED_GROUP_AND_RECORDINGS"
        groups[owner] = {**value, "membership_evidence": evidence, "identity_evidence": identity_evidence,
                         "source_evidence_scope": scope}

    selected, owners_by_source = [], defaultdict(list)
    for owner, value in sorted(groups.items()):
        for s in value["sources"]:
            owners_by_source[source_key(s)].append(owner)
        if activity_id is None or activity_id in value["member_ids"]:
            sources = [{**s, "first_source_evidence": acquisitions[source_key(s)],
                        "first_owner_evidence": ownership[(owner, source_key(s))]} for s in value["sources"]]
            selected.append({**value, "sources": sources})
    members = {m for g in selected for m in g["member_ids"]}
    if activity_id is not None:
        members.add(activity_id)
    decisions = []
    if inputs.table(db, "source_identity_reviews"):
        for row in db.execute("SELECT * FROM source_identity_reviews WHERE known_at<=? ORDER BY known_at,rowid", (cutoff,)):
            item = {**json.loads(row["evidence"]), "id": row["id"]}
            if activity_id is None or item["owner_id"] in members or members.intersection(item["candidates_at_observation"]):
                decisions.append(item)
    result = {"version": VERSION, "knowledge_cutoff": cutoff, "requested_activity_id": activity_id,
              "state": "OBSERVED" if selected else "UNKNOWN", "groups": selected, "review_events": decisions,
              "group_history": [h for h in history if activity_id is None or members.intersection(h["group"]["member_ids"])],
              "missing_reason": None if selected else "No timestamped evidence establishes this ownership at the cutoff."}
    if activity_id is None:
        result["acquisition_state"] = "OBSERVED" if acquisitions else "UNKNOWN"
        result["source_receipts"] = [{"kind": k[0], "sha256": k[1], "format": k[2], "first_source_evidence": receipt,
                                      "observed_owners": owners_by_source[k]} for k, receipt in sorted(acquisitions.items())]
    return result
