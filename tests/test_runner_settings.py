"""Race goals and preferences through the existing app and SQLite store."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json

import pytest

import runner_settings as settings
from storage import Store
from test_availability import available, submission, window
from test_fit_import import legacy_archive
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


def prefs(previous, request_id="preferences-1", **changes):
    return {"request_id": request_id, "expected_revision": previous, "actor": "Synthetic runner",
            "reason": "A recorded preference", "action": "SET_PREFERENCES", "max_sessions_per_day": None,
            "strength_exercise_choices": None, "special_constraints": None, **changes}


async def test_preferences_idempotency_history_and_restart(history, tmp_path, monkeypatch):
    times = ["2027-01-10T23:59:59.100001+00:00", "2027-01-10T23:59:59.100002+00:00"]
    clock = iter(times)
    monkeypatch.setattr(settings, "now", lambda: next(clock))
    async with server() as client:
        initial = (await client.get(API + "/preferences")).json()["current"]
        assert initial["special_constraints"] is None and initial["source"] == "NOT_ENTERED"
        constraint = {"scope": "  key run ", "text": " \r\n Keep\tthis <choice> Ω \r\n",
                      "from_date": "2027-01-01", "through_date": "2027-02-07"}
        raw = prefs(initial["version_id"], max_sessions_per_day=0, strength_exercise_choices="  My exercises\r\n",
                    special_constraints=[constraint], actor=" runner ", reason=" Initial choice ")
        one = (await client.post(API + "/preferences", json=raw)).json()["record"]
        assert one["special_constraints"] == [constraint]
        assert one["strength_exercise_choices"] == raw["strength_exercise_choices"] and one["max_sessions_per_day"] == 0
        assert (one["actor"], one["reason"], one["decided_at"]) == (" runner ", " Initial choice ", times[0])
        two = (await client.post(API + "/preferences", json=prefs(one["version_id"], "prefs-2", special_constraints=[]))).json()["record"]
        assert two["special_constraints"] == [] and two["max_sessions_per_day"] is None
        replay = (await client.post(API + "/preferences", json=raw)).json()
        assert replay == {"record": one, "replayed": True, "html_url": "/runner-settings",
                          "version_url": API + "/preferences/versions/" + one["version_id"]}
        for bad in ({**raw, "reason": "Changed"}, {**raw, "request_id": "new-stale"}):
            assert (await client.post(API + "/preferences", json=bad)).status_code == 409
        original_goal = (await client.get(API + "/goal")).json()["current"]
        other_kind = {"request_id": raw["request_id"], "expected_revision": original_goal["version_id"],
                      "actor": "A", "reason": "B", "action": "REVISE_GOAL", "target_duration_s": 9182}
        assert (await client.post(API + "/goal", json=other_kind)).status_code == 409
    async with server() as client:
        for cutoff, expected in [("2027-01-11T00:59:59.100000+01:00", initial), (times[0], one), (times[1], two)]:
            state = (await client.get(API + "/preferences", params={"as_of": cutoff})).json()
            assert state["current"] == expected and len(state["versions"]) == expected["revision"] + 1
        later_path = API + "/preferences/versions/" + two["version_id"]
        assert (await client.get(later_path, params={"as_of": times[0]})).status_code == 404
        assert (await client.get(API + "/goal/versions/" + one["version_id"])).status_code == 404
        assert (await client.get(API + "/preferences", params={"as_of": "2027-01-01"})).status_code == 422
    with Store(tmp_path / "runs.sqlite3").history() as db:
        assert settings.read_record(db, "preferences", version_id=initial["version_id"]) == initial
        assert db.execute("SELECT count(*) FROM runner_setting_revisions").fetchone()[0] == 3


async def test_weekly_windows_and_preferences_are_independently_pinned(history, tmp_path):
    week = "2026-07-06"
    path = API + "/availability/" + week
    async with server() as client:
        initial = (await client.get(API + "/preferences")).json()["current"]
        empty = (await client.get(path)).json()
        assert empty["availability_version"] is None and all(d["effective_max_sessions"] is None for d in empty["days"])
        body = available(submission(week), 0, [window("2026-07-06T10:00", "2026-07-06T11:00")])
        available(body, 1, [window("2026-07-07T10:00", "2026-07-07T14:00")], max_sessions=2, doubles_allowed=True)
        body["days"][2].update(state="UNKNOWN", max_sessions=None)
        calendar = (await client.post("/api/availability/" + week, json=body)).json()["record"]
        saved = (await client.post(API + "/preferences", json=prefs(initial["version_id"], max_sessions_per_day=5))).json()["record"]
        combined = (await client.get(path)).json()
        assert (combined["preferences_version"], combined["availability_version"]) == (saved["version_id"], calendar["version_id"])
        assert [(d["effective_max_sessions"], d["doubles_permitted"]) for d in combined["days"][:4]] == [
            (1, False), (2, True), (None, False), (0, False)]
        assert [d["window_ids"] for d in combined["days"]][:3] == [["2026-07-06:0"], ["2026-07-07:0"], []]
        restrictive = (await client.post(API + "/preferences", json=prefs(saved["version_id"], "restrictive", max_sessions_per_day=1))).json()["record"]
        fresh = (await client.get(path)).json()
        assert fresh["days"][1]["effective_max_sessions"] == 1 and not fresh["days"][1]["doubles_permitted"]
        assert (await client.get("/api/availability/" + week)).json()["current"] == calendar
        key = {k: combined[k] for k in ("as_of", "preferences_version", "availability_version")}
        assert (await client.get(path, params=key)).json() == combined
        assert (await client.get(path, params={"as_of": empty["as_of"]})).json() == empty
        assert (await client.get(path, params={**key, "preferences_version": restrictive["version_id"]})).status_code == 404
        assert (await client.get(API + "/availability/2026-07-13", params=key)).status_code == 404
        assert (await client.get(API + "/goal")).json()["current"]["target_duration_s"] == 9000


@pytest.mark.parametrize("invalid", [
    {"actor": "  "}, {"reason": "\n "}, {"action": "ACTIVATE_PLAN"}, {"expected_revision": None},
    {"max_sessions_per_day": True}, {"max_sessions_per_day": -1}, {"max_sessions_per_day": 1.5},
    {"strength_exercise_choices": " \r\n "}, {"strength_exercise_choices": "bad\0field"},
    {"special_constraints": [{"scope": "running", "text": "   "}]},
    {"special_constraints": [{"scope": "strength", "text": "A", "from_date": "2027-02-30"}]},
    {"special_constraints": [{"scope": "running", "text": "B", "from_date": "2027-03-14", "through_date": "2027-03-13"}]},
    {"decided_at": "2020-01-01T00:00:00Z"}, {"race_local_date": "2028-03-15"},
])
async def test_strict_validation_preserves_unentered_state(history, tmp_path, invalid):
    async with server() as client:
        base = (await client.get(API + "/preferences")).json()["current"]
        r = await client.post(API + "/preferences", json=prefs(base["version_id"], **invalid))
        assert r.status_code == 422, r.text
        assert (await client.get(API + "/preferences")).json()["current"] == base
        assert not (tmp_path / "runs.sqlite3").exists()


async def test_goal_requires_deliberate_action_and_valid_integral_time(history, tmp_path):
    async with server() as client:
        base = (await client.get(API + "/goal")).json()["current"]
        raw = {"request_id": "g", "expected_revision": base["version_id"], "actor": "Runner", "reason": "Fixture decision",
               "action": "REVISE_GOAL", "target_duration_s": 9123}
        for field, value in (("action", "CHECKPOINT_REACHED"), ("target_duration_s", 0), ("target_duration_s", True),
                             ("target_duration_s", 9000), ("race_local_date", "2028-03-15")):
            assert (await client.post(API + "/goal", json={**raw, field: value})).status_code == 422
        duplicate = json.dumps(raw).replace('"REVISE_GOAL"', '"REVISE_GOAL","action":"REVISE_GOAL"')
        assert (await client.post(API + "/goal", content=duplicate)).status_code == 422
        assert (await client.post(API + "/goal", content=b"x" * (256 * 1024 + 1))).status_code == 413
        assert (await client.get(API + "/goal")).json()["current"] == base


async def test_concurrent_expected_revisions_and_stored_baseline(history, tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite3"
    base = settings.original("preferences")
    def submit(i):
        try:
            return settings.save(Store(path), "preferences", prefs(base["version_id"], str(i)))["record"]
        except settings.Conflict:
            return None
    with ThreadPoolExecutor(max_workers=3) as executor:
        saved, = [r for r in executor.map(submit, range(3)) if r is not None]
    assert saved["revision"] == 1
    with Store(path).history() as db:
        exact = settings.read_record(db, "preferences", version_id=base["version_id"])
        assert exact == base
        monkeypatch.setattr(settings, "original", lambda _: pytest.fail("A pinned baseline consulted live code."))
        assert settings.read_record(db, "preferences", version_id=base["version_id"]) == base
        assert settings.read_record(db, "preferences", as_of="2000-01-01T00:00:00Z") == base
        assert settings.read_record(db, "preferences", version_id=saved["version_id"]) == saved


async def test_settings_on_existing_import_leave_actual_and_archive_intact(history, tmp_path):
    _, original_bytes, directory = legacy_archive(tmp_path)
    async with server() as client:
        url = "/api/activities/731"
        original = (await client.get(url)).json()
        assert (await client.get(url + "/canonical")).json()["state"] == "NOT_PROCESSED"
        before = (await client.get("/api/imports")).json()
        assert (await client.get("/runner-settings")).status_code == 200
        base = (await client.get(API + "/preferences")).json()["current"]
        assert (await client.post(API + "/preferences", json=prefs(base["version_id"]))).status_code == 200
        goal = (await client.get(API + "/goal")).json()["current"]
        assert goal["target_duration_s"] == 9000 and goal["decided_at"] is None
        assert (await client.get(url)).json() == original
        assert (await client.get("/api/imports")).json() == before
        assert (await client.get(url + "/canonical")).json()["state"] == "NOT_PROCESSED"
        assert (directory / sha256(original_bytes).hexdigest()).read_bytes() == original_bytes
        assert not history.calls
