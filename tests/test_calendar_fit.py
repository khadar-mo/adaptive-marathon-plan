"""Fixed-session placement through the availability, template and diagnostic HTTP routes."""
from copy import deepcopy

import httpx
import pytest

from test_availability import available, submission as week_input, window
from test_health import serve_database
from test_workout_templates import block, pace, submission as template_input


WEEK = "2026-11-23"


@pytest.fixture
def client(tmp_path):
    with serve_database(tmp_path / "runs.sqlite3") as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            yield http


def post(client, path, value):
    response = client.post(path, json=value)
    assert response.status_code == 200, response.text
    return response.json()


def save_week(client, doc):
    first = doc["days"][0]["date"]
    return post(client, "/api/availability/" + first, doc)["record"]


def save_session(client, key, blocks):
    doc = template_input(request_id="template-" + key, name=key, blocks=blocks)
    saved = post(client, "/api/workout-templates/" + key, doc)["record"]
    return {"session_id": key, "template_id": key, "template_version_id": saved["version_id"]}


def fixed_block(key, kind, metres, **extra):
    return block(key, kind, metres, pace=pace(low="300", high="300"), **extra)


def request_for(week, sessions, **extra):
    return {"week": week["week"], "availability_version_id": week["version_id"], "sessions": sessions, **extra}


def test_w2_five_to_three_declared_days_check_exact_unchanged_sessions(client):
    three = week_input(WEEK)
    for i, end in [(0, "08:16:16"), (2, "08:05:00"), (4, "08:20:00")]:
        day = three["days"][i]["date"]
        available(three, i, [window(day + "T06:00", day + "T" + end)])
    five = deepcopy(three)
    for i in (1, 3):
        day = five["days"][i]["date"]
        available(five, i, [window(day + "T09:00", day + "T12:00")])
    original = save_week(client, five)
    three.update(request_id="reduce-to-three", expected_revision=original["version_id"])
    revised = save_week(client, three)
    sessions = [
        save_session(client, "tempo", [fixed_block("wu", "WARM_UP", 5000),
                                       fixed_block("tempo", "WORK", 6000, protected=True, rest_after_s="75.125"),
                                       fixed_block("cd", "COOL_DOWN", 16000)]),
        save_session(client, "easy", [fixed_block("easy", "EASY_VOLUME", 25000)]),
        save_session(client, "long", [fixed_block("long", "EASY_VOLUME", 28000)])]
    request = request_for(revised, sessions)
    answer = post(client, "/api/calendar-fit", request)
    assert answer["status"] == "FEASIBLE"
    doses = {s["session_id"]: (s["total_m"], s["work_m"], s["duration_upper_seconds"]) for s in answer["sessions"]}
    assert doses == {"tempo": (27000, 6000, 8176), "easy": (25000, 0, 7500), "long": (28000, 0, 8400)}
    assert [(s["session_id"], s["start_utc"], s["end_utc"]) for s in answer["placements"]] == [
        ("tempo", "2026-11-23T06:00:00+00:00", "2026-11-23T08:16:16+00:00"),
        ("easy", "2026-11-25T06:00:00+00:00", "2026-11-25T08:05:00+00:00"),
        ("long", "2026-11-27T06:00:00+00:00", "2026-11-27T08:20:00+00:00")]
    assert post(client, "/api/calendar-fit", {**request, "sessions": list(reversed(sessions))}) == answer
    for calendar in (original, revised):
        assert client.get(f"/api/availability/{WEEK}/versions/{calendar['version_id']}").json() == calendar
