"""Exercise the public fitness profile using synthetic physical FIT and context inputs."""
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


async def test_t13_run_available_with_missing_sleep_and_hrv(history):
    async with server() as client:
        await upload(client, ("steady.fit", summary_fit()))
        response = await client.get("/api/fitness", params={"as_of": absolute_time(START + 1501).isoformat()})
        assert response.status_code == 200, response.text
        state = response.json()
        window = state["exposure"]["rolling"][0]
        assert window["run_count"] == 1
        assert window["measures"]["distance_m"]["known_subtotal"] == "5000.0"
        assert window["measures"]["elapsed_seconds"]["known_subtotal"] == "1500.375"
        assert state["recovery"]["wellness"]["hrv"]["state"] == "MISSING"
        assert state["recovery"]["wellness"]["sleep_duration"]["state"] == "MISSING"


async def test_frozen_profile_rebuilt_with_original_raw_observations(history):
    async with server() as client:
        await upload(client, ("source.fit", summary_fit()))
        response = await client.post("/api/fitness/capture", json={"as_of": absolute_time(START + 1501).isoformat()})
        assert response.status_code == 200, response.text
        saved = response.json()
        best = next(r["best"] for r in saved["state"]["pb_all"] if r["distance_m"] == 5000)
        assert best["elapsed_coordinate_s"] == "1500" and best["status"] == "SUPPORTED_OBSERVATION"
        api = "/api/fitness/snapshots/" + saved["snapshot_id"]
        assert (await client.get(api + "/replay")).json()["status"] == "IDENTICAL"
        bundle = (await client.get(api + "/inputs")).json()
        assert bundle["capability_evidence"]["traces"]["1"]["source_sha256"] == best["ref"]["source_sha256"]
        assert (await client.get(api)).json()["state"] == saved["state"]
        fixed = await client.post("/api/fitness/capture", json={"as_of": saved["state"]["event_cutoff"],
            "knowledge_cutoff": saved["state"]["knowledge_cutoff"]})
        assert fixed.json()["snapshot_id"] == saved["snapshot_id"]
