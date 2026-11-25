"""G0 source/coverage prerequisites for Part A T04/T18/T19/T22/T23.

These are inspection tests, not claims that the later G1/G2 QC and extraction
acceptance tests pass. All recordings and provider responses are synthetic.
"""
import hashlib

import pytest

from fit_data import absolute_time
from fit_fixture import START, make_fit
from test_fit_import import upload, zipped
from test_imports import digest, history, server, submit


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_record_order_plateaus_events_laps_and_provenance_are_read_only(history, tmp_path):
    # Deliberate duplicate/out-of-order times and a distance reversal remain visible.
    points = [(0, 0), (1, 3.25), (1, 3.25), (5, 3.25), (3, 2.75)]
    messages = [(20, {"timestamp": START + t, "distance": d, "heart_rate": 140 + i})
                for i, (t, d) in enumerate(points)]
    messages += [(21, {"timestamp": START + t, "event": 0, "event_type": kind})
                 for t, kind in [(0, 0), (1, 4), (5, 0), (6, 4)]]
    messages += [(19, {"message_index": n, "start_time": START + t, "timestamp": START + t + 1,
                      "total_distance": d}) for n, t, d in [(9, 3, 2.75), (1, 0, 3.25)]]
    data = make_fit(messages=messages, metrics={"timestamp": START + 6, "total_distance": 2.75,
                                              "total_elapsed_time": 6, "total_timer_time": 2})
    async with server() as client:
        report = await upload(client, ("source.fit", data), ("renamed.fit", data))
        assert report["counts"]["inserted"] == 1
        database = tmp_path / "runs.sqlite3"
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        response = await client.get('/api/activities/1/recording')
        assert response.status_code == 200
        row = response.json()
        assert row == (await client.get('/api/activities/1/recording')).json()
        assert row["state"] == "AVAILABLE"
        assert row["selected_fit_sha256"] == digest(data) and len(row["recordings"]) == 1
        assert row["provenance"]["inspection_version"] == "g0-fit-inspection-v1"
        assert row["provenance"]["decoder_version"]
        assert row["provenance"]["decode_options"]["convert_datetimes_to_dates"] is False
        assert row["recordings"][0]["sources"][0]["sha256"] == digest(data)
        assert row["header"]["end_utc"] == absolute_time(START + 6).isoformat()
        assert row["header"]["moving_seconds"] is None
        samples = row["samples"]["rows"]
        assert [s["record_index"] for s in samples] == list(range(5))
        assert [(s["fields"]["timestamp"] - START, s["distance_m"]) for s in samples] == points
        assert [s["heart_rate_bpm"] for s in samples] == [140, 141, 142, 143, 144]
        assert [e["event_type"] for e in row["events"]] == ["start", "stop_all", "start", "stop_all"]
        assert [e["event_index"] for e in row["events"]] == [0, 1, 2, 3]
        assert row["coverage"]["timer_events"] == "RECORDED"
        assert row["coverage"]["samples"]["distance_m"] == {"status": "RECORDED", "present": 5, "total": 5, "missing": 0}
        assert [lap["fields"]["message_index"] for lap in row["laps"]] == [9, 1]
        assert [lap["lap_index"] for lap in row["laps"]] == [0, 1]
        page = (await client.get('/activities/1/recording')).text
        assert "stop all" in page and "Not provided" in page and "No samples are sorted" in page
        assert "Inspect recorded samples and events" in (await client.get('/activities/1')).text
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
        assert (tmp_path / "archive" / digest(data)).read_bytes() == data
        assert not history.calls


async def test_summary_only_missing_channels_are_unknown_not_zero(history):
    async with server() as client:
        await upload(client, ("summary.fit", make_fit(metrics={"total_distance": 5000})))
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["state"] == "AVAILABLE" and row["samples"]["total"] == 0
        assert row["header"]["end_utc"] is None and row["header"]["elapsed_seconds"] is None
        assert all(c["status"] == "NOT_PROVIDED" for c in row["coverage"]["samples"].values())
        assert row["coverage"]["timer_events"] == "NOT_PROVIDED" and row["laps"] == []
        html = (await client.get('/activities/1/recording')).text
        assert "Pause and resume history is unknown" in html and "summary data only" in html
        assert "Lap markers not provided" in html
        assert row["heart_rate_messages"] == [] and row["coverage"]["heart_rate_message_count"] == 0


async def test_separate_heart_rate_messages_are_visible_without_merging_into_records(history):
    # Standard FIT global message 132 can carry HR outside the record stream.
    fit = make_fit(messages=[(20, {"timestamp": START, "distance": 0}),
                             (20, {"timestamp": START + 1, "distance": 3}),
                             (132, {"timestamp": START, "fractional_timestamp": 0, "time256": 0,
                                    "filtered_bpm": [145, 146], "event_timestamp": [100, 101]}),
                             (132, {"filtered_bpm": 147, "event_timestamp": 102})])
    async with server() as client:
        await upload(client, ("separate-hr.fit", fit))
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["samples"]["rows"][0]["heart_rate_bpm"] is None
        assert row["coverage"]["samples"]["heart_rate_bpm"]["status"] == "NOT_PROVIDED"
        assert row["coverage"]["heart_rate_message_count"] == 2
        assert row["coverage"]["heart_rate_message_timestamp_utc"]["status"] == "PARTIAL"
        assert row["coverage"]["heart_rate_message_bpm"]["status"] == "RECORDED"
        assert [h["hr_index"] for h in row["heart_rate_messages"]] == [0, 1]
        assert row["heart_rate_messages"][0]["filtered_bpm"] == [145, 146]
        assert row["heart_rate_messages"][1]["filtered_bpm"] == 147
        assert row["heart_rate_messages"][0]["timestamp_utc"] == absolute_time(START).isoformat()
        assert row["heart_rate_messages"][1]["timestamp_utc"] is None
        assert row["heart_rate_messages"][1]["fields"]["event_timestamp"] == 102
        assert row["provenance"]["heart_rate_indices"] == "zero-based hr_mesgs order"
        html = (await client.get('/activities/1/recording')).text
        assert "Separate heart-rate messages" in html and "2 separate HR messages" in html
        assert "[145, 146]" in html and "147" in html and "not merged" in html


async def test_partial_fields_relative_time_and_unknown_events_remain_inspectable(history):
    raw = make_fit(messages=[(20, {"timestamp": START, "distance": 0, "heart_rate": 145}),
                             (20, {"timestamp": 42, "distance": 3}), (20, {"timestamp": START + 2}),
                             (21, {"event": 0, "event_type": 0}),
                             (21, {"timestamp": START + 1, "event": 0, "event_type": 80})])
    async with server() as client:
        await upload(client, ("partial.fit", raw))
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["samples"]["rows"][1]["timestamp_utc"] is None
        assert row["samples"]["rows"][1]["fields"]["timestamp"] == 42
        for field, count in (("timestamp_utc", 2), ("distance_m", 2), ("heart_rate_bpm", 1)):
            assert row["coverage"]["samples"][field] == {"status": "PARTIAL", "present": count, "total": 3, "missing": 3 - count}
        assert row["coverage"]["timer_events"] == "PARTIAL"
        assert row["events"][1]["event_type"] == "unknown" and row["events"][1]["fields"]["event_type"] == 80


async def test_record_pagination_keeps_global_indices_and_full_coverage(history):
    data = make_fit(messages=[(20, {"timestamp": START + i, "distance": 3 * i}) for i in range(205)])
    async with server() as client:
        await upload(client, ("pages.fit", data))
        first = (await client.get('/api/activities/1/recording')).json()
        last = (await client.get('/api/activities/1/recording?offset=200')).json()
        assert len(first["samples"]["rows"]) == 200 and len(last["samples"]["rows"]) == 5
        assert [s["record_index"] for s in last["samples"]["rows"]] == list(range(200, 205))
        assert first["coverage"] == last["coverage"] and first["samples"]["total"] == last["samples"]["total"] == 205
        assert "Next samples" in (await client.get('/activities/1/recording')).text
        last_html = (await client.get('/activities/1/recording?offset=200')).text
        assert "Previous samples" in last_html and "Next samples" not in last_html
        assert "Records 200–204" in last_html and "source=" + digest(data) in last_html
        empty = (await client.get('/activities/1/recording?offset=9999')).text
        assert "No records on this page" in empty and "offset=200" in empty
        assert first["coverage"]["timer_events"] == "NOT_PROVIDED"


@pytest.mark.parametrize("query", ["offset=-1", "offset=2147483648", "limit=0", "limit=501", "source=../../secret", "offset=bad"])
async def test_invalid_recording_queries_are_rejected(history, query):
    async with server() as client:
        assert (await client.get('/api/activities/1/recording?' + query)).status_code == 422


async def test_owned_sources_stay_distinct_and_other_activity_is_not_selectable(history):
    first = make_fit(messages=[(20, {"timestamp": START, "distance": 1})])
    second = make_fit(file_number=2, messages=[(20, {"timestamp": START, "distance": 2})])
    unrelated = make_fit(start=START + 86400)
    async with server() as client:
        await upload(client, ("first.fit", first), ("second.fit", second), ("other.fit", unrelated))
        for data, distance in ((first, 1), (second, 2)):
            row = (await client.get('/api/activities/1/recording?source=' + digest(data))).json()
            assert len(row["recordings"]) == 2
            assert row["selected_fit_sha256"] == digest(data)
            assert row["samples"]["rows"][0]["distance_m"] == distance
        assert (await client.get('/api/activities/1/recording?source=' + digest(unrelated))).status_code == 404
        assert (await client.get('/api/activities/999/recording')).status_code == 404


async def test_garmin_zip_provenance_and_read_only_member_fallback(history, tmp_path):
    fit = make_fit(messages=[(20, {"timestamp": START, "distance": 12.34})])
    history.original = zipped(fit)
    async with server() as client:
        await submit(client)
        member = tmp_path / "archive" / digest(fit)
        member.unlink()
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["state"] == "AVAILABLE" and row["selected_fit_sha256"] == digest(fit)
        assert row["samples"]["rows"][0]["distance_m"] == 12.34
        assert digest(history.original) in [s["sha256"] for s in row["recordings"][0]["sources"]]
        assert not member.exists()  # Inspection must not repair or reindex archives.


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_unavailable_archive_is_explicit_and_never_shows_invented_samples(history, tmp_path, damage):
    fit = make_fit(messages=[(20, {"timestamp": START, "distance": 12.34})])
    async with server() as client:
        await upload(client, ("source.fit", fit))
        path = tmp_path / "archive" / digest(fit)
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["state"] == "UNAVAILABLE" and row["samples"]["rows"] == []
        assert row["selected_fit_sha256"] == digest(fit)
        assert "checksum" in row["limitations"][0]
        assert "restore an original file" in (await client.get('/activities/1/recording')).text


async def test_original_not_supplied_keeps_activity_available(history):
    history.original = None
    async with server() as client:
        await submit(client)
        row = (await client.get('/api/activities/1/recording')).json()
        assert row["state"] == "UNAVAILABLE" and row["recordings"] == []
        assert "No original FIT" in row["limitations"][0]
        assert (await client.get('/activities/1')).status_code == 200
