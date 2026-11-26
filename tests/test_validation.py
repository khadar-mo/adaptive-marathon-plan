"""Synthetic G6 raw-to-snapshot and heldout validation through the normal app."""
import pytest
from test_imports import history, server
from test_fit_import import upload
from test_fit_summary_spans import summary_fit
from fit_data import absolute_time
from fit_fixture import START

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_immutable_fractional_run(history):
    async with server() as client:
        await upload(client, ("summary.fit", summary_fit()))
        t = absolute_time(START + 1501).isoformat()
        response = await client.post("/api/snapshots", json={"as_of": t, "mode": "EVENT_TIME_RECONSTRUCTION"})
        assert response.status_code == 200, response.text
        frozen = response.json()
        url = "/api/snapshots/" + frozen["id"]
        planner = (await client.get(url + "/planner")).json()
        run, = planner["actuals"]
        assert run["distance_m"] == "5000.0" and run["measures"]["elapsed_seconds"] == "1500.375"
        assert run["selected_fit_span"]["end_utc"] == run["end_utc"]
        assert run["fit_message_written_utc"] != run["end_utc"]
        assert run["identity"]["sources"][0]["first_owner_evidence"]["known_at"] <= frozen["knowledge_cutoff"]
        assert (await client.get(url + "/replay")).json()["status"] == "IDENTICAL"
        fixed = await client.post("/api/snapshots", json={"as_of": t, "knowledge_cutoff": frozen["knowledge_cutoff"],
                                                          "mode": "EVENT_TIME_RECONSTRUCTION"})
        assert fixed.json()["id"] == frozen["id"]
