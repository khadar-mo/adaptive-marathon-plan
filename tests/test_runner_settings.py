"""Race goals and preferences through the existing app and SQLite store."""
import pytest

from test_imports import history, server


pytestmark = pytest.mark.anyio
API = "/api/runner-settings"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_checkpoint_and_explicit_goal_revision(history, tmp_path):
    async with server() as client:
        goal = (await client.get(API + "/goal")).json()["current"]
        assert (goal["target_duration_s"], goal["race_local_date"], goal["review_date"]) == (9000, "2027-03-14", "2027-02-07")
        assert goal["changed_by_user"] is False and goal["decided_at"] is None
        for instant in ("2027-02-06T23:59:59Z", "2027-02-07T00:00:00Z", "2027-02-08T00:00:00Z"):
            assert (await client.get(API + "/goal", params={"as_of": instant})).json()["current"] == goal
            assert (await client.get("/runner-settings", params={"as_of": instant})).status_code == 200
        assert not (tmp_path / "runs.sqlite3").exists()
        raw = {"request_id": "acknowledged-goal", "expected_revision": goal["version_id"], "actor": "Synthetic runner",
               "reason": "An explicit fixture decision", "action": "REVISE_GOAL", "target_duration_s": 9123}
        saved = await client.post(API + "/goal", json=raw)
        assert saved.status_code == 200, saved.text
        new = saved.json()["record"]
        assert new["previous_version_id"] == goal["version_id"] and new["target_duration_s"] == 9123
        assert new["changed_by_user"] is True and new["decided_at"]
        assert (await client.get(API + "/goal/versions/" + goal["version_id"])).json() == goal
        assert (await client.get(API + "/preferences")).json()["current"]["max_sessions_per_day"] is None
