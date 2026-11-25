from demo_recordings import examples
from fit_data import decode_fit
from recording import inspect


def test_demo_files_have_the_documented_independent_expected_values():
    files = examples()
    for data in files.values():
        assert decode_fit(data)["fields"]["start_utc"] is not None
    rich = inspect(files["sample-rich.fit"], 175, 40)
    assert rich["header"]["distance_m"] == 1110
    assert rich["header"]["elapsed_seconds"] == 400 and rich["header"]["timer_seconds"] == 370
    assert rich["samples"]["total"] == 401
    assert [(s["record_index"], s["distance_m"]) for s in rich["samples"]["rows"][5:36]] == [(i, 540) for i in range(180, 211)]
    assert rich["coverage"]["timer_event_count"] == 4 and rich["coverage"]["lap_count"] == 2
    sparse = inspect(files["no-timer-events.fit"], 0, 200)
    assert sparse["samples"]["total"] == 205 and sparse["coverage"]["timer_events"] == "NOT_PROVIDED"
    assert sparse["coverage"]["samples"]["heart_rate_bpm"]["status"] == "NOT_PROVIDED"
    summary = inspect(files["summary-only.fit"], 0, 200)
    assert summary["samples"]["total"] == 0 and summary["header"]["distance_m"] == 2000
