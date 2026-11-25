"""G1 software prerequisites for Part A T04/T15/T18–T21/T23, not G2 PB claims."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys

import pytest

import canonical
from canonical_qc import DEFAULT_CONFIG, DEFAULT_CONFIG_VERSION, assess
from fit_data import absolute_time
from fit_fixture import START, make_fit
from recording import inspect
from storage import Archive, Store
from test_fit_import import upload, zipped, resolve, legacy_archive
from test_imports import digest, history, server, submit
from runs import ConnectionProblem


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def trace_fit(points=((0, 0), (1, 3.25), (2, 6.5), (3, 9.75)), *, start=START, events=(), extra=(), total=None, **kwargs):
    records = [(20, {**({"timestamp": start + t} if t is not None else {}),
                     **({"distance": d} if d is not None else {})}) for t, d in points]
    records += [(21, {**({"timestamp": start + t} if t is not None else {}), "event": 0, "event_type": kind}) for t, kind in events]
    elapsed = max((t for t, _ in points if t is not None), default=0)
    metrics = {"timestamp": start + elapsed, "total_distance": total if total is not None else points[-1][1],
               "total_elapsed_time": elapsed, "total_timer_time": max(0, elapsed - 1)}
    return make_fit(start=start, messages=records + list(extra), metrics=metrics, **kwargs)


def flags(row, code):
    return [f for f in row["qc"]["flags"] if f["code"] == code]


async def imported(client, raw):
    report = await upload(client, ("synthetic.fit", raw))
    assert report["activities"], report
    return (await client.get('/api/activities/1/canonical')).json()


async def test_import_persists_canonical_precision_raw_provenance_and_read_only_pages(history, tmp_path):
    raw = trace_fit(extra=[(132, {"filtered_bpm": [140, 141], "event_timestamp": [1, 2]})])
    async with server() as client:
        row = await imported(client, raw)
        assert row["state"] == "PROCESSED" and row["qc"]["state"] == "VALID"
        assert row["canonical_activity_id"] == row["activity_id"] == 1
        assert row["qc_config"] == DEFAULT_CONFIG
        assert row["qc_config_version"] == DEFAULT_CONFIG_VERSION
        assert row["selected_fit_sha256"] == digest(raw)
        assert row["selected_source_version_id"] == row["sources"][0]["source_version_id"]
        assert row["sources"][0]["qc_version_id"]
        assert row["parser"]["decode_options"]["merge_heart_rates"] is False
        assert row["parser"]["decoder_version"]
        header = row["trace"]["header"]
        assert header["end_utc"] == absolute_time(START + 3).isoformat()
        assert (header["distance_m"], header["elapsed_seconds"], header["timer_seconds"], header["moving_seconds"]) == (9.75, 3, 2, None)
        assert header["activity_summaries"][0]["local_timestamp"] - header["activity_summaries"][0]["timestamp"] == 3600
        assert row["trace"]["heart_rate_messages"][0]["filtered_bpm"] == [140, 141]
        assert all(s["heart_rate_bpm"] is None for s in row["trace"]["samples"]["rows"])
        assert row["human_validation"] == row["scientific_validation"] == "UNVERIFIED"
        assert row["qc"]["candidate_rules_applied"] is False
        before = digest((tmp_path / "runs.sqlite3").read_bytes())
        assert row == (await client.get('/api/activities/1/canonical')).json()
        page = await client.get('/activities/1/canonical')
        assert page.status_code == 200 and "VALID" in page.text and "9.75" in page.text
        assert "Inspect canonical trace and quality" in (await client.get('/activities/1')).text
        assert digest((tmp_path / "runs.sqlite3").read_bytes()) == before
        assert (tmp_path / "archive" / digest(raw)).read_bytes() == raw
        assert not history.calls


@pytest.mark.parametrize(("gap", "suspect"), [(29, False), (30, False), (31, True)])
async def test_gap_threshold_through_import(history, gap, suspect):
    async with server() as client:
        row = await imported(client, trace_fit([(0, 0), (1, 3), (1 + gap, 6), (2 + gap, 9)]))
        assert (row["qc"]["state"] == "SUSPECT") == suspect
        found = flags(row, "gap_duration_s")
        assert bool(found) == suspect
        if suspect:
            assert found[0]["sample_indices"] == [1, 2]
            assert found[0]["calculation"]["gap_duration_s"] == gap
            assert found[0]["raw_references"][0] == {"sha256": row["selected_fit_sha256"], "message_group": "record_mesgs", "index": 1}


@pytest.mark.parametrize(("duration", "review", "quarantine"), [(9, False, False), (10, True, False), (29, True, False), (30, True, True)])
async def test_stationary_thresholds_retain_time_without_fabricating_stop(history, duration, review, quarantine):
    raw = trace_fit([(0, 0), (1, 3), (1 + duration, 3), (2 + duration, 6)])
    async with server() as client:
        row = await imported(client, raw)
        found = flags(row, "stationary_plateau_s")
        assert bool(found) == review
        assert len(row["qc"]["blocks"]) == 1
        assert row["trace"]["samples"]["rows"][2]["distance_m"] == 3
        assert row["trace"]["header"]["elapsed_seconds"] == duration + 2
        if review:
            assert found[0]["sample_indices"] == [1, 2]
            assert found[0]["calculation"]["quarantined"] == quarantine
            assert found[0]["calculation"]["reason"] == "AMBIGUOUS_STATIONARY"
            assert found[0]["calculation"]["fabricated_stop"] is False


@pytest.mark.parametrize(("speed", "suspect"), [(12, False), (12.1, True)])
async def test_speed_threshold_retains_distance(history, speed, suspect):
    async with server() as client:
        row = await imported(client, trace_fit([(0, 0), (1, speed), (2, speed + 3), (3, speed + 6)]))
        assert (row["qc"]["state"] == "SUSPECT") == suspect
        assert row["trace"]["samples"]["rows"][1]["distance_m"] == speed
        if suspect:
            assert flags(row, "distance_jump")[0]["calculation"]["speed_mps"] == speed


@pytest.mark.parametrize(("total", "difference", "suspect"), [(1000, 100, False), (1000, 100.01, True), (20000, 200, False), (20000, 200.01, True)])
async def test_summary_tolerance_boundary(history, total, difference, suspect):
    # Short adjacent steps ensure this test isolates the total mismatch rule.
    distance = total - difference
    points = [(i, distance * i / 2000) for i in range(2001)]
    async with server() as client:
        row = await imported(client, trace_fit(points, total=total))
        found = flags(row, "inconsistent_total_distance")
        assert bool(found) == suspect
        if suspect:
            assert found[0]["sample_indices"] == [0, 2000]
            assert found[0]["calculation"]["tolerance_m"] == max(100, total * .01)


@pytest.mark.parametrize(("points", "code", "state"), [
    ([(0, 0), (1, 3), (1, 6), (3, 9)], "timestamp_order_error", "INVALID"),
    ([(0, 0), (2, 3), (1, 6), (3, 9)], "timestamp_order_error", "INVALID"),
    ([(0, 0), (1, 3), (2, 2), (3, 9)], "distance_reversal", "INVALID"),
    ([(0, 0), (None, 3), (2, 6), (3, 9)], "missing_sample_timestamp", "SUSPECT"),
    ([(0, 0), (1, None), (2, 6), (3, 9)], "missing_sample_distance", "SUSPECT"),
    ([(None, 0), (None, 3)], "insufficient_trace", "INSUFFICIENT"),
])
async def test_clock_distance_and_missing_fields_keep_raw_order(history, points, code, state):
    async with server() as client:
        row = await imported(client, trace_fit(points))
        assert row["qc"]["state"] == state and flags(row, code)
        actual = row["trace"]["samples"]["rows"]
        assert [s["record_index"] for s in actual] == list(range(len(points)))
        assert [s["distance_m"] for s in actual] == [p[1] for p in points]


async def test_relative_clock_is_invalid_not_host_timezone(history):
    async with server() as client:
        row = await imported(client, trace_fit(start=42))
        assert row["qc"]["state"] == "INVALID"
        assert flags(row, "invalid_clock")[0]["raw_references"][0]["message_group"] == "session_mesgs"
        assert row["trace"]["header"]["start_utc"] is None
        assert row["trace"]["samples"]["rows"][0]["fields"]["timestamp"] == 42
        assert row["summary"]["start_utc"] is None


async def test_negative_distance_defense_keeps_decoded_value():
    # Standard FIT distance is unsigned. Exercise the defensive normalization check
    # directly for a future decoder's malformed signed value; do not forge FIT bytes.
    trace = inspect(trace_fit(), 0, 200)
    trace["samples"]["rows"][1]["distance_m"] = -1
    trace["samples"]["rows"][1]["fields"]["distance"] = -1
    qc = assess(trace, "a" * 64, DEFAULT_CONFIG)
    assert qc["state"] == "INVALID"
    assert next(f for f in qc["flags"] if f["code"] == "negative_distance")["sample_indices"] == [1]
    assert trace["samples"]["rows"][1]["distance_m"] == -1


@pytest.mark.parametrize("count", [2, 3])
async def test_candidate_only_rules_do_not_reject_activity(history, count):
    async with server() as client:
        row = await imported(client, trace_fit([(i * 16, i * 10) for i in range(count)]))
        assert row["qc"]["state"] == "VALID"
        assert row["qc"]["candidate_rules_applied"] is False
        assert not flags(row, "interpolation_bracket_gap_s")


async def test_confirmed_stop_splits_even_when_distance_rises(history):
    raw = trace_fit([(0, 0), (5, 15), (15, 45), (45, 135), (50, 150), (55, 165)], events=[(0, 0), (10, 4), (40, 0), (55, 4)])
    async with server() as client:
        row = await imported(client, raw)
        assert row["qc"]["timer_event_coverage"] == "RECORDED"
        assert [b["sample_indices"] for b in row["qc"]["blocks"]] == [[0, 1], [3, 4, 5]]
        assert row["qc"]["unassigned_sample_indices"] == [2]
        assert flags(row, "pause_event_inside_window")[0]["sample_indices"] == [1, 2]
        assert row["trace"]["samples"]["rows"][2]["distance_m"] == 45
        assert row["qc"]["blocks"][0]["right_boundary"]["event_index"] == 1


async def test_confirmed_stationary_pause_is_not_an_ambiguous_stop(history):
    async with server() as client:
        row = await imported(client, trace_fit([(0, 0), (1, 3), (31, 3), (32, 6)], events=[(0, 0), (1, 4), (31, 0), (32, 4)]))
        assert row["qc"]["state"] == "VALID"
        assert flags(row, "stationary_plateau_s")[0]["calculation"]["reason"] == "CONFIRMED_STOP"
        assert [b["sample_indices"] for b in row["qc"]["blocks"]] == [[0, 1], [2, 3]]


@pytest.mark.parametrize("events", [[(None, 0), (2, 80)], [(0, 0), (2, 0)], [(2, 4), (1, 0)]])
async def test_partial_or_ambiguous_event_history_does_not_prove_continuity(history, events):
    async with server() as client:
        row = await imported(client, trace_fit(events=events))
        assert row["qc"]["state"] == "SUSPECT"
        assert row["qc"]["timer_event_coverage"] == "PARTIAL"
        assert flags(row, "timer_event_coverage")[0]["calculation"]["proves_continuity"] is False
        if events[-1][0] == 1:
            assert row["qc"]["blocks"] == []


async def test_summary_only_retained_and_garmin_zip_provider_lineage(history):
    history.original = zipped(make_fit(metrics={"timestamp": START + 1800, "total_distance": 5000}))
    async with server() as client:
        await submit(client)
        row = (await client.get('/api/activities/1/canonical')).json()
        assert row["qc"]["state"] == "INSUFFICIENT"
        assert row["trace"]["samples"]["total"] == 0
        assert row["summary"]["distance_m"] == 5001.25
        assert row["provider_ids"] == [{"provider": "garmin", "id": "42"}]
        assert digest(history.original) in [s["sha256"] for s in row["raw_sources"]]


async def test_provider_summary_disagreement_has_explicit_review_and_event_time(history):
    history.original = zipped(trace_fit())
    async with server() as client:
        await submit(client)
        row = (await client.get('/api/activities/1/canonical')).json()
        assert row["source_qc_state"] == "VALID" and row["canonical_quality_state"] == "SUSPECT"
        distance = next(c for c in row["summary_comparisons"] if c["field"] == "distance_m")
        assert distance["requires_distance_review"] and distance["fit_header_ref"]["message_group"] == "session_mesgs"
        assert row["header"]["end_utc"] == absolute_time(START + 3).isoformat()
        assert row["summary"]["end_basis"] == "start_utc_plus_reported_elapsed_seconds"
        assert row["created_at"] > row["header"]["end_utc"]
        assert row["source_created_at"] <= row["created_at"]
        assert row["selection_history"][0]["selected_at"] >= row["created_at"]
        assert all(s["acquired_at"] for s in row["raw_sources"])


async def test_garmin_distance_correction_creates_new_audit_version_without_raw_overwrite(history, tmp_path):
    raw = trace_fit()
    history.original = zipped(raw)
    async with server() as client:
        await submit(client)
        initial = (await client.get('/api/activities/1/canonical')).json()
        history.detail["summaryDTO"]["distance"] = 9.75
        await submit(client)
        current = (await client.get('/api/activities/1/canonical')).json()
        assert current["version_id"] != initial["version_id"]
        assert current["canonical_quality_state"] == "VALID" and initial["canonical_quality_state"] == "SUSPECT"
        assert current["selected_source_version_id"] == initial["selected_source_version_id"]
        old = (await client.get('/api/activities/1/canonical?version=' + initial["version_id"])).json()
        assert old["summary"] == initial["summary"] and old["raw_sources"] == initial["raw_sources"]
        assert (tmp_path / "archive" / digest(raw)).read_bytes() == raw


async def test_explicit_merge_preserves_donor_versions_and_redirects(history):
    history.original_error = ConnectionProblem("Synthetic offline original.")
    async with server() as client:
        await submit(client)
        donor = (await client.get('/api/activities/1/canonical')).json()
        raw = trace_fit(start=START + 300)
        await upload(client, ("manual.fit", raw))
        history.original_error = None
        history.original = zipped(raw)
        pending = await submit(client)
        response = await resolve(client, pending, {"action": "link", "activity_id": 2})
        assert response.status_code == 200
        merged = (await client.get('/api/activities/2/canonical')).json()
        assert merged["merged_activity_ids"] == [1]
        assert merged["provider_ids"] == [{"provider": "garmin", "id": "42"}]
        old = (await client.get('/api/activities/1/canonical?version=' + donor["version_id"])).json()
        assert old["activity_id"] == 2 and old["activity_id_at_creation"] == 1
        assert old["summary"] == donor["summary"] and old["sources"] == donor["sources"]
        assert len(merged["selection_history"]) == 3


async def test_legacy_first_importer_database_preserves_ids_and_bytes_on_backfill(history, tmp_path):
    data, original, directory = legacy_archive(tmp_path)
    async with server() as client:
        before = (await client.get('/api/activities/731')).json()
        assert (await client.get('/api/activities/731/canonical')).json()["state"] == "NOT_PROCESSED"
        result = await client.post('/api/canonical/recompute', json={})
        assert result.status_code == 200 and result.json()["activities"][0]["activity_id"] == 731
        row = (await client.get('/api/activities/731/canonical')).json()
        assert row["selected_fit_sha256"] == digest(data)
        assert (await client.get('/api/activities/731')).json() == before
        assert (directory / digest(original)).read_bytes() == original
        assert not history.calls


async def test_recompute_idempotence_config_and_parser_versions_are_immutable(history, tmp_path, monkeypatch):
    async with server() as client:
        initial = await imported(client, trace_fit([(0, 0), (1, 3), (32, 6), (33, 9)]))
        url = '/api/activities/1/canonical'
        repeated = await client.post(url + '/recompute', json={})
        assert repeated.status_code == 200
        assert repeated.json()["activities"][0]["version_id"] == initial["version_id"]
        await upload(client, ("renamed.fit", trace_fit([(0, 0), (1, 3), (32, 6), (33, 9)])))
        assert len((await client.get(url)).json()["versions"]) == 1
        config = {**DEFAULT_CONFIG, "max_sample_gap_s": 40}
        changed = await client.post(url + '/recompute', json={"config_version": "synthetic-v2", "config": config})
        assert changed.status_code == 200
        current = (await client.get(url)).json()
        assert current["version_id"] != initial["version_id"] and current["qc"]["state"] == "VALID"
        assert current["selected_source_version_id"] == initial["selected_source_version_id"]
        old = (await client.get(url + '?version=' + initial["version_id"])).json()
        assert old["qc"] == initial["qc"] and old["trace"] == initial["trace"]
        conflict = await client.post(url + '/recompute', json={"config_version": "synthetic-v2", "config": DEFAULT_CONFIG})
        assert conflict.status_code == 422 and "different settings" in conflict.text
        monkeypatch.setattr(canonical, "version", lambda name: "synthetic-new-parser")
        await client.post(url + '/recompute', json={})
        latest = (await client.get(url)).json()
        assert latest["selected_source_version_id"] != initial["selected_source_version_id"]
        assert latest["trace"] == initial["trace"] and len(latest["versions"]) == 3
        assert (tmp_path / "archive" / initial["selected_fit_sha256"]).read_bytes() == trace_fit([(0, 0), (1, 3), (32, 6), (33, 9)])


async def test_linked_old_sources_ownership_pagination_and_restart(history, tmp_path):
    first = trace_fit([(i, i * 3) for i in range(205)])
    second = trace_fit(file_number=2)
    other = trace_fit(start=START + 86400)
    async with server() as client:
        initial = await imported(client, first)
        await upload(client, ("second.fit", second), ("other.fit", other))
        current = (await client.get('/api/activities/1/canonical')).json()
        assert len(current["sources"]) == 2 and len(current["versions"]) == 2
        assert (await client.get('/api/activities/1/canonical?source=' + digest(other))).status_code == 404
        other_version = (await client.get('/api/activities/2/canonical')).json()["version_id"]
        assert (await client.get('/api/activities/1/canonical?version=' + other_version)).status_code == 404
        old = (await client.get('/api/activities/1/canonical?version=' + initial["version_id"] + '&offset=200')).json()
        assert old["trace"]["samples"]["rows"][0]["record_index"] == 200
        assert old["selected_fit_sha256"] == digest(first)
        assert "Historical version" in (await client.get('/activities/1/canonical?version=' + initial["version_id"])).text
    async with server() as client:
        restarted = (await client.get('/api/activities/1/canonical')).json()
        assert restarted == current
        assert (await client.get('/api/history?from=2024-02-29&to=2024-03-01')).json()["run_count"] == 2


async def test_existing_imported_database_backfill_is_explicit_and_offline(history, tmp_path):
    async with server() as client:
        await imported(client, trace_fit())
        activity = (await client.get('/api/activities/1')).json()
    store = Store(tmp_path / "runs.sqlite3", Archive(tmp_path / "archive"))
    with store.connect(write=True) as db:
        for table in ("canonical_selections", "canonical_activity_heads", "canonical_activity_versions", "canonical_qc_versions", "canonical_source_versions", "qc_configs"):
            db.execute(f"DROP TABLE {table}")
    before = digest(store.path.read_bytes())
    async with server() as client:
        assert (await client.get('/api/activities/1/canonical')).json()["state"] == "NOT_PROCESSED"
        assert "not processed" in (await client.get('/activities/1/canonical')).text
        assert digest(store.path.read_bytes()) == before
        result = await client.post('/api/canonical/recompute', json={})
        assert result.status_code == 200
        assert (await client.get('/api/activities/1/canonical')).json()["qc"]["state"] == "VALID"
        assert (await client.get('/api/activities/1')).json() == activity
        assert not history.calls


async def test_offline_cli_and_concurrent_recompute_read_keep_one_version(history, tmp_path):
    raw = trace_fit()
    async with server() as client:
        initial = await imported(client, raw)
        responses = await asyncio.gather(*[client.post('/api/activities/1/canonical/recompute', json={}) for _ in range(6)],
                                         *[client.get('/api/activities/1/canonical') for _ in range(6)])
        assert all(r.status_code == 200 for r in responses)
    store = Store(tmp_path / "runs.sqlite3", Archive(tmp_path / "archive"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: canonical.recompute(store), range(8)))
    assert all(r == [{"activity_id": 1, "version_id": initial["version_id"]}] for r in results)
    output = subprocess.run([sys.executable, '../canonical.py', '--database', str(store.path), '--archive', str(store.archive.directory)],
                            capture_output=True, text=True, check=True)
    assert json.loads(output.stdout)["activities"] == results[0]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM canonical_activity_versions").fetchone()[0] == 1
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_archive_damage_does_not_rewrite_historical_proof(history, tmp_path, damage):
    raw = trace_fit()
    async with server() as client:
        initial = await imported(client, raw)
        path = tmp_path / "archive" / digest(raw)
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        saved = (await client.get('/api/activities/1/canonical')).json()
        assert saved["archive_status_now"] == "UNAVAILABLE" and saved["qc"] == initial["qc"]
        assert "restore the original" in (await client.get('/activities/1/canonical')).text
        await client.post('/api/activities/1/canonical/recompute', json={})
        current = (await client.get('/api/activities/1/canonical')).json()
        assert current["qc"]["state"] == "INSUFFICIENT" and current["trace"] is None
        await upload(client, ("restore.fit", raw))
        restored = (await client.get('/api/activities/1/canonical')).json()
        assert restored["version_id"] == initial["version_id"]


@pytest.mark.parametrize("query", ["limit=0", "offset=-1", "version=../secret", "source=bad", "limit=501"])
async def test_bad_query_is_rejected(history, query):
    async with server() as client:
        assert (await client.get('/api/activities/1/canonical?' + query)).status_code == 422


@pytest.mark.parametrize("choice", [{"config": {}}, {"config_version": "bad/name"}, {"extra": True},
                                  {"config": {**DEFAULT_CONFIG, "max_sample_gap_s": 0}},
                                  {"config": {**DEFAULT_CONFIG, "max_sample_gap_s": True}},
                                  {"config": {**DEFAULT_CONFIG, "max_sample_gap_s": 10**400}},
                                  {"config": {**DEFAULT_CONFIG, "min_candidate_samples": 4.5}}])
async def test_bad_config_is_rejected_without_new_versions(history, choice):
    async with server() as client:
        initial = await imported(client, trace_fit())
        assert (await client.post('/api/activities/1/canonical/recompute', json=choice)).status_code == 422
        assert (await client.get('/api/activities/1/canonical')).json() == initial
        assert (await client.post('/api/activities/1/canonical/recompute', json={}, headers={"Origin": "https://elsewhere.invalid"})).status_code == 403
