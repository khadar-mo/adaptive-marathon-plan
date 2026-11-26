"""Synthetic provenance checks through the existing local app."""
from datetime import datetime, timedelta
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
