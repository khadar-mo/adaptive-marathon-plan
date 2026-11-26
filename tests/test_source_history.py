"""Synthetic provenance checks through the existing local app."""
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from test_imports import history, server


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def just_before(value):
    return (datetime.fromisoformat(value) - timedelta(microseconds=1)).isoformat()


async def test_existing_g2_canonical_receipt_proves_owner_only_when_known(history, tmp_path):
    fixture = json.loads(gzip.decompress((Path(__file__).parent / "fixtures" / "workout-g2-59faa54.json.gz").read_bytes()))
    path = tmp_path / "runs.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(fixture["sql"])
        latest = db.execute("SELECT id,created_at FROM canonical_activity_versions ORDER BY rowid LIMIT 1").fetchone()
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    async with server() as client:
        route = "/api/activities/1/source-history"
        earlier = await client.get(route, params={"knowledge_cutoff": just_before(latest[1])})
        assert earlier.status_code == 200
        assert earlier.json()["groups"] == [] and earlier.json()["state"] == "UNKNOWN"
        response = await client.get(route, params={"knowledge_cutoff": latest[1]})
        assert response.status_code == 200, response.text
        group, = response.json()["groups"]
        assert group["owner_id"] == 1 and group["member_ids"] == [1]
        source, = group["sources"]
        assert source["sha256"] == fixture["source_sha256"]
        assert source["first_owner_evidence"]["known_at"] == latest[1]
        loaded = (await client.get("/api/canonical/versions/" + latest[0])).json()
        assert loaded["selected_fit_sha256"] == fixture["source_sha256"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original

async def test_late_upload_retry_and_subsecond_as_of_survive_restart(history, monkeypatch):
    import source_receipts
    from fit_fixture import START
    from test_canonical import trace_fit
    from test_fit_import import upload

    clock = ["2026-11-25T10:00:00.100000+00:00"]
    monkeypatch.setattr(source_receipts.inputs, "now", lambda: clock[0])
    first = trace_fit()
    second = trace_fit(start=START + 5 * 86400)
    early = {"knowledge_cutoff": "2026-11-25T11:00:00.100000+01:00"}
    async with server() as client:
        await upload(client, ("first.fit", first))
        snapshot = (await client.get("/api/source-history", params=early)).json()
        a, = snapshot["groups"]
        source, = a["sources"]
        assert source["sha256"] == hashlib.sha256(first).hexdigest()
        assert a["claimed_event_start_utc"].startswith("2024-")
        assert source["first_source_evidence"]["basis"] == "FIT_UPLOAD_ARCHIVED"
        assert source["first_source_evidence"]["import_id"] == 1
        assert source["first_owner_evidence"]["basis"] == "IMPORT_IDENTITY"
        assert source["first_owner_evidence"]["known_at"] == clock[0]
        assert (await client.get("/api/source-history", params={"knowledge_cutoff": just_before(clock[0])})).json()["groups"] == []
        clock[0] = "2026-11-25T10:00:00.900000+00:00"
        await upload(client, ("earlier-recorded.fit", second))
        item = await upload(client, ("duplicate.fit", first))
        assert item["activities"][0]["outcome"] == "unchanged"
        late = (await client.get("/api/source-history", params={"knowledge_cutoff": clock[0]})).json()
        assert len(late["source_receipts"]) == len(late["groups"]) == len(late["group_history"]) == 2
        assert late["groups"][0] == a
    async with server() as client:
        assert (await client.get("/api/source-history", params=early)).json() == snapshot


async def test_real_review_changes_ownership_after_decision_and_keeps_original_version(history):
    from runs import ConnectionProblem
    from fit_fixture import START
    from test_fit_import import upload, zipped, resolve
    from test_canonical import trace_fit
    from test_imports import submit

    history.original_error = ConnectionProblem("Synthetic offline original.")
    async with server() as client:
        await submit(client)
        canonical = (await client.get("/api/activities/1/canonical")).json()
        exact_version = "/api/canonical/versions/" + canonical["version_id"]
        frozen_version = (await client.get(exact_version)).json()
        raw = trace_fit(start=START+300)
        await upload(client, ("other.fit", raw))
        history.original_error = None
        history.original = zipped(raw)
        pending = await submit(client)
        route = "/api/activities/1/source-history"
        before = (await client.get(route)).json()
        original, = before["groups"]
        assert original["member_ids"] == [1] and original["provider_ids"] == [{"provider":"garmin","id":"42"}]
        assert (await client.get("/api/activities/2/source-history")).json()["groups"][0]["member_ids"] == [2]
        pending_choice, = [r for r in before["review_events"] if r["action"] == "PENDING"]
        assert pending_choice["owner_id"] is None
        assert any(s["sha256"] == hashlib.sha256(raw).hexdigest() for s in pending_choice["sources"])
        assert (await resolve(client, pending, {"action":"link","activity_id":2})).status_code == 200
        after = (await client.get(route)).json()
        joined, = after["groups"]
        assert joined["owner_id"] == 2 and joined["member_ids"] == [1,2]
        assert joined["provider_ids"] == original["provider_ids"]
        receipt = joined["identity_evidence"]
        assert receipt["basis"] == "REVIEW_RESOLUTION" and receipt["import_id"] == pending["id"]
        decision, = [r for r in after["review_events"] if r["action"] == "link"]
        assert decision["id"] in receipt["review_event_ids"] and decision["issue_id"] == receipt["issue_id"]
        assert decision["known_at"] == receipt["known_at"] and decision["owner_id"] == 2
        assert (await client.get(route, params={"knowledge_cutoff":just_before(receipt["known_at"])})).json()["groups"][0]["owner_id"] == 1
        precut = {"knowledge_cutoff": before["knowledge_cutoff"]}
        assert (await client.get(route, params=precut)).json() == before
        assert (await client.get(exact_version)).json() == frozen_version
        assert (await resolve(client, pending, {"action":"link","activity_id":2})).status_code == 200
        repeated = (await client.get(route)).json()
        assert repeated["review_events"] == after["review_events"]
        assert repeated["group_history"] == after["group_history"]
    async with server() as client:
        assert (await client.get(route, params=precut)).json() == before


async def test_failed_derivation_rolls_back_ownership_preserves_staged_raw_and_retry(history, monkeypatch):
    import source_receipts
    from test_canonical import trace_fit
    from test_fit_import import upload

    raw = trace_fit()
    observe = source_receipts.observe_group
    def interrupted(*args, **kwargs):
        observe(*args, **kwargs)
        raise sqlite3.OperationalError("Synthetic cancellation inside the transaction")

    async with server() as client:
        with monkeypatch.context() as patch:
            patch.setattr(source_receipts, "observe_group", interrupted)
            failed = await upload(client, ("rolled-back.fit", raw))
        assert failed["state"] == "failed"
        unowned = (await client.get("/api/source-history")).json()
        assert unowned["groups"] == unowned["group_history"] == unowned["review_events"] == []
        receipt, = unowned["source_receipts"]
        assert receipt["observed_owners"] == [] and receipt["first_source_evidence"]["import_id"] == failed["id"]
        saved = await upload(client, ("retry.fit", raw))
        assert saved["state"] == "complete"
        recovered = (await client.get("/api/source-history")).json()
        current, = recovered["source_receipts"]
        assert current["first_source_evidence"] == receipt["first_source_evidence"]
        assert current["observed_owners"] == [1]
        assert recovered["groups"][0]["membership_evidence"]["import_id"] == saved["id"]
        assert (await client.get("/api/source-history", params={"knowledge_cutoff":unowned["knowledge_cutoff"]})).json() == unowned


async def test_unproven_legacy_claim_is_only_observed_at_upgrade(history, tmp_path):
    from test_fit_import import legacy_archive
    _, original, _ = legacy_archive(tmp_path)
    async with server() as client:
        # This pre-FIT schema needs the repository's ordinary startup migration.
        route = "/api/activities/731/source-history"
        old = (await client.get(route, params={"knowledge_cutoff":"2024-03-01T10:00:00Z"})).json()
        assert old["state"] == "UNKNOWN" and old["groups"] == []
        current = (await client.get(route)).json()
        group, = current["groups"]
        source, = group["sources"]
        assert source["claimed_acquired_at"] == "then"
        assert source["sha256"] == hashlib.sha256(original).hexdigest()
        assert source["first_owner_evidence"]["basis"] == "LEGACY_STATE_OBSERVED"
        assert (await client.get(route, params={"knowledge_cutoff":just_before(source["first_owner_evidence"]["known_at"])})).json()["state"] == "UNKNOWN"


async def test_legacy_terminal_proves_raw_checksum_only_then_inventory_proves_owner(history, tmp_path):
    import workload_inputs
    from test_fit_import import upload
    from test_canonical import trace_fit
    import app

    async with server() as client:
        raw = trace_fit()
        await upload(client, ("original.fit", raw))
        store = app.app.state.store
        with store.connect(write=True) as db:
            for table in ("source_history_epoch", "source_acquisitions", "source_group_history", "source_identity_reviews",
                          "canonical_selections", "canonical_activity_heads", "effort_heads"):
                db.execute("DELETE FROM " + table)
            # Model an older unprocessed store with a coarse terminal clock. Its
            # later immutable G3 observation explicitly contains that terminal.
            db.execute("UPDATE canonical_activity_versions SET created_at='2026-11-25T13:00:00.000000+00:00'")
            db.execute("UPDATE canonical_source_versions SET created_at='2026-11-25T13:00:00.000000+00:00'")
            end = "2026-11-25T10:00:00+00:00"
            db.execute("UPDATE imports SET finished_at=? WHERE id=1", (end,))
            observation = "2026-11-25T10:00:00.250000+00:00"
            payload = {"activities":[{"activity_id":1,"summary_at_observation":{"start_utc":"2024-02-29T00:00:00Z"},
                                      "source_refs_at_observation":[{"kind":"fit","format":"fit","sha256":hashlib.sha256(raw).hexdigest()}]}],
                       "import_boundary":[{"id":1,"finished_at":end}]}
            db.execute("INSERT INTO workload_processing_inventories VALUES(?,?,?)",
                       ("fixture-inventory",observation,workload_inputs.encoded(payload)))
        before = (await client.get("/api/source-history", params={"knowledge_cutoff":just_before(observation)})).json()
        assert before["groups"] == before["source_receipts"] == []
        after = (await client.get("/api/source-history", params={"knowledge_cutoff":observation})).json()
        source, = after["source_receipts"]
        assert source["first_source_evidence"]["clock"]["availability_basis"] == "IMMUTABLE_INVENTORY_OBSERVATION"
        assert source["observed_owners"] == [1]
        group, = after["groups"]
        assert group["identity_evidence"] is None and group["provider_ids"] is None
        assert group["sources"][0]["first_owner_evidence"]["basis"] == "PROCESSING_INVENTORY"

async def test_automatic_device_match_retains_both_sources_and_prior_group(history):
    from fit_fixture import make_fit
    from test_fit_import import upload, zipped
    from test_imports import submit

    manual = make_fit(file_number=3)
    device = make_fit(file_number=7)
    history.original = zipped(device)
    async with server() as client:
        first = await upload(client, ("manual.fit", manual))
        before = (await client.get("/api/activities/1/source-history")).json()
        report = await submit(client)
        assert report["state"] == "complete" and not report["issues"]
        data = (await client.get("/api/activities/1/source-history")).json()
        value, = data["groups"]
        sources = {s["sha256"]:s for s in value["sources"]}
        assert value["provider_ids"] == [{"provider":"garmin","id":"42"}]
        assert value["member_ids"] == [1] and data["review_events"] == []
        assert sources[hashlib.sha256(manual).hexdigest()]["first_owner_evidence"]["import_id"] == first["id"]
        assert sources[hashlib.sha256(device).hexdigest()]["first_owner_evidence"]["import_id"] == report["id"]
        assert sources[hashlib.sha256(history.original).hexdigest()]["kind"] == "original"
        assert value["identity_evidence"]["basis"] == "IMPORT_IDENTITY"
        assert (await client.get("/api/activities/1/source-history", params={"knowledge_cutoff":before["knowledge_cutoff"]})).json() == before


async def test_inspection_rejects_invalid_cutoffs_and_preserves_database_absence(history, tmp_path):
    async with server() as client:
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        for cutoff in ("", future, "2024-01-01", "unparsed"):
            assert (await client.get("/api/source-history", params={"knowledge_cutoff":cutoff})).status_code == 422
        assert (await client.get("/api/activities/0/source-history")).status_code == 422
        assert (await client.get("/api/canonical/versions/absent")).status_code == 404
        assert (await client.get("/api/source-history")).json()["state"] == "UNKNOWN"
    assert not (tmp_path / "runs.sqlite3").exists()

async def test_earlier_claimed_acquisition_cannot_move_proven_knowledge_back(history, monkeypatch):
    import storage
    from test_canonical import trace_fit
    from test_fit_import import upload

    raw = trace_fit()
    async with server() as client:
        await upload(client, ("run.fit", raw))
        route = "/api/activities/1/source-history"
        before = (await client.get(route)).json()
        original = before["groups"][0]["sources"][0]
        claimed = "2000-01-01T09:10:11.012345+00:00"
        with monkeypatch.context() as patch:
            patch.setattr(storage, "now", lambda: claimed)
            await upload(client, ("new-acquisition-claim.fit", raw))
        later = (await client.get(route)).json()
        changed = later["groups"][0]["sources"][0]
        assert changed["claimed_acquired_at"] == claimed
        assert changed["first_source_evidence"] == original["first_source_evidence"]
        assert changed["first_owner_evidence"] == original["first_owner_evidence"]
        assert (await client.get(route, params={"knowledge_cutoff":"2001-01-01T00:00:00Z"})).json()["state"] == "UNKNOWN"
        assert (await client.get(route, params={"knowledge_cutoff":before["knowledge_cutoff"]})).json() == before

@pytest.mark.parametrize("scan_mode,expected", [("range", {"list","detail"}), ("legacy", set())])
async def test_legacy_wire_scan_proves_raw_versions_without_assigning_an_owner(history, scan_mode, expected):
    import app
    from test_imports import submit

    async with server() as client:
        report = await submit(client)
        store = app.app.state.store
        with store.connect(write=True) as db:
            for name in ("source_history_epoch","source_acquisitions","source_group_history","source_identity_reviews"):
                db.execute("DELETE FROM " + name)
            end = "2021-06-05T07:08:09.000000+00:00"
            db.execute("UPDATE imports SET finished_at=? WHERE id=?", (end, report["id"]))
            db.execute("UPDATE garmin_scans SET mode=?", (scan_mode,))
        data = (await client.get("/api/source-history", params={"knowledge_cutoff":end})).json()
        assert data["groups"] == []
        assert {r["kind"] for r in data["source_receipts"]} == expected
        assert all(r["first_source_evidence"]["basis"] == "COMPLETED_GARMIN_SCAN" and
                   r["first_source_evidence"]["import_id"] == report["id"] and not r["observed_owners"]
                   for r in data["source_receipts"])
