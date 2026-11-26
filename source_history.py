"""As-of provenance for source acquisition and activity ownership."""
import json

import canonical
import workload_inputs as inputs
from workload_processing import clock_evidence


VERSION = "source-identity-history-v1"


def ref(kind, key, known_at, **details):
    return {"basis": kind, "id": str(key), "known_at": known_at, **details}


def group(owner, members, providers, sources, start):
    return {"owner_id": owner, "member_ids": sorted(set([owner, *members])),
            "provider_ids": providers, "claimed_event_start_utc": start,
            "sources": [{"kind": s["kind"], "sha256": s["sha256"], "format": s["format"],
                         "claimed_acquired_at": s.get("acquired_at")} for s in sources]}


def legacy_evidence(db, cutoff):
    """Read affirmative observations, independently of current heads and redirects."""
    if inputs.table(db, "canonical_activity_versions"):
        rows = db.execute("""SELECT id,activity_id,created_at,payload FROM canonical_activity_versions
            WHERE created_at<=? ORDER BY rowid""", (cutoff,))
        versions = {}
        for row in rows:
            payload = json.loads(row["payload"])
            value = group(row["activity_id"], payload["merged_activity_ids"], payload["provider_ids"],
                          payload["raw_sources"], payload["summary"]["start_utc"])
            versions[row["id"]] = value
            yield ref("CANONICAL_VERSION", row["id"], inputs.utc(row["created_at"])), value
        if inputs.table(db, "canonical_selections"):
            rows = db.execute("SELECT * FROM canonical_selections WHERE selected_at<=? ORDER BY id", (cutoff,))
            for row in rows:
                if row["version_id"] in versions:
                    yield ref("CANONICAL_SELECTION", row["id"], inputs.utc(row["selected_at"]),
                              version_id=row["version_id"]), versions[row["version_id"]]
    if inputs.table(db, "workload_processing_inventories"):
        for row in db.execute("SELECT * FROM workload_processing_inventories WHERE known_at<=? ORDER BY rowid", (cutoff,)):
            for activity in json.loads(row["payload"])["activities"]:
                key = ref("PROCESSING_INVENTORY", row["id"], inputs.utc(row["known_at"]))
                yield key, group(activity["activity_id"], [], None, activity["source_refs_at_observation"],
                                 activity["summary_at_observation"]["start_utc"])


def read_in_db(db, knowledge_cutoff, *, activity_id=None):
    """A single-transaction view. Its contents depend only on facts known by K."""
    cutoff = inputs.utc(knowledge_cutoff)
    observations = list(legacy_evidence(db, cutoff))
    observations.sort(key=lambda item: item[0]["known_at"])
    groups, acquisitions, ownership = {}, {}, {}
    for evidence, value in observations:
        owner = value["owner_id"]
        for source in value["sources"]:
            identity = (source["kind"], source["sha256"], source["format"])
            acquisitions.setdefault(identity, evidence)
            ownership.setdefault((owner, identity), evidence)
        previous = groups.get(owner)
        if value["provider_ids"] is None and previous:
            value = {**value, "provider_ids": previous["provider_ids"], "member_ids": previous["member_ids"]}
        for member in value["member_ids"]:
            if member != owner:
                groups.pop(member, None)
        groups[owner] = {**value, "membership_evidence": evidence,
                         "identity_evidence": previous["identity_evidence"] if previous and evidence["basis"]=="PROCESSING_INVENTORY" else evidence if value["provider_ids"] is not None else None}
    selected = []
    for owner, value in sorted(groups.items()):
        if activity_id is not None and activity_id not in value["member_ids"]:
            continue
        sources = []
        for s in value["sources"]:
            identity = (s["kind"], s["sha256"], s["format"])
            sources.append({**s, "first_source_evidence": acquisitions[identity],
                            "first_owner_evidence": ownership[(owner, identity)]})
        selected.append({**value, "sources": sources})
    return {"version": VERSION, "knowledge_cutoff": cutoff, "requested_activity_id": activity_id,
            "state": "OBSERVED" if selected else "UNKNOWN", "groups": selected,
            "missing_reason": None if selected else "No timestamped evidence establishes this ownership at the cutoff."}
