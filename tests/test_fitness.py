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
