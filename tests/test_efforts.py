"""G2 tests: independently derived arithmetic and public import/inspection/review paths."""
from datetime import timedelta
from fractions import Fraction
import json
import random
import subprocess
import sys

import pytest

import canonical
import effort_math
import efforts
from canonical_qc import DEFAULT_CONFIG, assess
from demo_efforts import dense, examples
from fit_data import absolute_time
from fit_fixture import START
from recording import inspect
from storage import Archive, Store
from test_canonical import trace_fit
from test_fit_import import upload, resolve
from test_fit_import import zipped
from test_imports import history, server, digest, submit, add_run, LISTED, DETAIL
from runs import ConnectionProblem


@pytest.fixture
def anyio_backend():
    return "asyncio"


def distance(row, target):
    return next(r for r in row["inspected_source"]["result"]["distances"] if r["distance_m"] == target)


def pure(points, targets, *, events=(), config=None, min_end=None):
    trace = inspect(trace_fit(points, events=events), 0, 2**31 - 1)
    config = config or DEFAULT_CONFIG
    return effort_math.extract(trace, assess(trace, "a" * 64, config), config, targets, min_end=min_end)


def oracle(points, target):
    """Independent exact-rational exhaustive polygon oracle, no production helpers.

    Includes all distance values, shifted ends and interval midpoints. A and L
    are found by scanning every positive edge plus every coincident observation.
    """
    points = [(Fraction(t), Fraction(d)) for t, d in points]
    target = Fraction(target)
    lo, hi = points[0][1], points[-1][1] - target
    xs = sorted({lo, hi, *(d for _, d in points if lo <= d <= hi),
                 *(d - target for _, d in points if lo <= d - target <= hi)})
    probes = xs + [(a + b) / 2 for a, b in zip(xs, xs[1:])]
    def times(x):
        values = [t for t, d in points if d == x]
        for (ta, da), (tb, db) in zip(points, points[1:]):
            if da < x < db:
                values.append(ta + (tb - ta) * (x - da) / (db - da))
        return min(values), max(values)
    return min((times(x + target)[0] - times(x)[1], times(x)[1], x) for x in probes)


def test_T16_T17_sparse_geometry_preserves_stationarity_but_does_not_weaken_production_qc():
    points = [(0, 0), (300, 1000), (420, 1000), (720, 2000)]
    result = pure(points, [1000, 2000])
    one, two = result["distances"]
    assert one["best"] is None and two["best"] is None
    assert one["flagged_candidate"]["elapsed_seconds"] == 300
    assert two["flagged_candidate"]["elapsed_seconds"] == 720
    assert two["flagged_candidate"]["audit"]["interior_plateaus"][0]["duration_s"] == 120
    assert not one["flagged_candidate"]["audit"]["interior_plateaus"]
    assert one["flagged_candidate"]["computed_quality_state"] == "INSUFFICIENT"
    assert two["flagged_candidate"]["computed_quality_state"] == "SUSPECT"
    assert one["one_sided_limit_count"] > 0
    trace = inspect(trace_fit(points), 0, 99)
    block = effort_math.prepare(trace, assess(trace, "a" * 64, DEFAULT_CONFIG), DEFAULT_CONFIG)[0][0]
    assert block.crossing(1000, "arrival")["elapsed_seconds"] == 300
    assert block.crossing(1000, "departure")["elapsed_seconds"] == 420
    assert block.crossing(1000, "departure", -1)["elapsed_seconds"] == 300
    assert block.crossing(1000, "arrival", 1)["elapsed_seconds"] == 420


def test_independent_fraction_oracle_and_shifted_endpoint_winner():
    rng = random.Random(17)
    # Integer small traces and <10s plateaus remain within production defaults.
    for _ in range(30):
        points = [(0, 0)]
        for i in range(1, 25):
            points.append((i * 2, points[-1][1] + rng.randrange(0, 17)))
        target = points[-1][1] // 2
        expected = oracle(points, target)
        r = pure(points, [target])["distances"][0]
        c = r["best"] or r["flagged_candidate"]
        assert c["elapsed_seconds"] == pytest.approx(float(expected[0]), abs=1e-9)
    points = [(0, 0), (10, 10), (20, 30), (25, 65), (28, 86), (30, 100), (40, 110), (50, 120)]
    r = pure(points, [75])["distances"][0]["best"]
    assert r["start_distance_m"] == 25 and r["end_distance_m"] == 100
    assert r["start"]["fraction"] == pytest.approx(15 / 20)
    assert r["end"]["fraction"] == 0


def test_T02_T03_T18_no_stitching_even_with_rising_distance_and_include_jogs():
    points, events = [(0, 0)], [(0, 0)]
    t, d = 0, 0
    for _ in range(5):
        for i in range(1, 61):
            points.append((t + i * 5, d + i * 1000 / 60))
        t, d = t + 300, d + 1000
        events.append((t, 4))
        if d < 5000:
            points += [(t + 30, d + 10), (t + 60, d + 20), (t + 90, d + 30)]
            t, d = t + 90, d + 30
            events.append((t, 0))
    result = pure(points, [5000], events=events)["distances"][0]
    assert result["state"] == "NOT_CALCULABLE" and result["candidate_count"] == 0
    jogging = pure(dense([(0, 0), (300, 1200), (600, 1800), (1400, 5000)]), [5000])["distances"][0]["best"]
    assert jogging["elapsed_seconds"] == 1400  # includes the slower 300-second jog


@pytest.mark.parametrize(("points", "flag"), [
    (dense([(0, 0), (150, 500), (162, 500), (312, 1000)]), "stationary_plateau_s"),
    ([(0, 0), (5, 20), (36, 40), (41, 60), (46, 80)], "gap_duration_s"),
    ([(0, 0), (1, 12.1), (2, 15), (3, 18), (4, 21)], "distance_jump"),
])
def test_T19_T20_candidate_anomalies_withhold_authoritative_result(points, flag):
    r = pure(points, [points[-1][1]])["distances"][0]
    assert r["best"] is None and r["flagged_candidate"]["computed_quality_state"] == "SUSPECT"
    assert flag in {f["code"] for f in r["flagged_candidate"]["flags"]}


@pytest.mark.parametrize(("gap", "state"), [(15, "VALID"), (16, "SUSPECT")])
def test_endpoint_bracket_limits_and_four_original_records(gap, state):
    points = [(0, 0), (gap, 30), (gap + 1, 40), (gap + 2, 50), (gap + 3, 60)]
    trace = inspect(trace_fit(points), 0, 99)
    block = effort_math.prepare(trace, assess(trace, "a" * 64, DEFAULT_CONFIG), DEFAULT_CONFIG)[0][0]
    assert block.candidate(15, 40)["computed_quality_state"] == state
    sparse = block.candidate(35, 10)
    assert sparse["computed_quality_state"] == "INSUFFICIENT" and sparse["original_sample_count"] == 3


def test_local_qc_and_malformed_order_never_sorted_into_pb():
    points = dense([(0, 0), (300, 1000)]) + [(331, 1010), (336, 1020)]
    assert pure(points, [1000])["distances"][0]["best"]["elapsed_seconds"] == 300
    for points in ([(0, 0), (5, 20), (4, 40), (10, 60)], [(0, 0), (5, 20), (10, 10), (15, 60)],
                   [(0, 0), (5, None), (10, 40), (15, 60)]):
        r = pure(points, [40])
        assert r["distances"][0]["best"] is None and r["unusable_blocks"]


def test_T04_partial_timer_history_is_flagged_and_full_activity_future_cutoff_is_required():
    points = dense([(0, 0), (300, 1000)])
    r = pure(points, [400], events=[(0, 0), (100, 0)])["distances"][0]
    assert r["best"] is None and r["flagged_candidate"]["timer_event_coverage"] == "PARTIAL"


@pytest.mark.anyio
async def test_T01_T05_T22_T23_public_import_bests_full_trace_and_selected_interpolation(history, tmp_path):
    async with server() as client:
        for name, raw in examples().items():
            report = await upload(client, (name, raw))
            assert report["state"] == "complete"
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row, 5000)["best"]
        assert c["elapsed_seconds"] == pytest.approx(1440) and c["start_distance_m"] == 3000
        assert c["start"]["sample_indices"][0] > 100 and c["end"]["sample_indices"][0] > 200
        assert c["computed_quality_state"] == "VALID" and c["provenance"]["verified_maximal"] is False
        assert c["timer_event_coverage"] == "NOT_PROVIDED"
        bests = (await client.get('/api/efforts?cutoff=2026-01-15T00:00:00Z')).json()
        five = next(r for r in bests["distances"] if r["distance_m"] == 5000)
        assert five["all_time"]["activity_id"] == 1 and five["best_whole_activity"]["activity_id"] == 2
        assert five["best_whole_activity"]["elapsed_seconds"] == 1500
        assert five["best_whole_activity"]["pace_seconds_per_km"] == 300
        assert five["best_whole_activity"]["mean_speed_mps"] == pytest.approx(10 / 3)
        before = digest((tmp_path / 'runs.sqlite3').read_bytes())
        for url in ('/efforts', '/activities/1/efforts', f'/activities/1/efforts/{row["version_id"]}/{c["id"]}'):
            page = await client.get(url)
            assert page.status_code == 200
        assert digest((tmp_path / 'runs.sqlite3').read_bytes()) == before
        # T22 selects an explicit interval; this does not pretend it is a unique fastest optimum.
        choice = {"version": row["version_id"], "source": row["selected_fit_sha256"], "start_distance_m": 100.5, "distance_m": 5000}
        response = await client.post('/api/activities/1/efforts/inspect', json=choice)
        assert response.status_code == 200, response.text
        inspected = (await client.get('/api' + response.json()["html_url"])).json()
        selected = inspected["candidate"]
        assert 0 < selected["start"]["fraction"] < 1 and 0 < selected["end"]["fraction"] < 1
        assert selected["elapsed_seconds"] == pytest.approx(900 + (5100.5 - 3000) * 1440 / 5000 - 100.5 * 900 / 3000, abs=.002)
        assert inspected["samples"]["total"] > 200 and len(inspected["samples"]["rows"]) == 200
        next_page = (await client.get('/api' + response.json()["html_url"] + '?offset=200')).json()
        assert next_page["samples"]["rows"][0]["record_index"] > inspected["samples"]["rows"][-1]["record_index"]
        assert inspected["source"]["fit_sha256"] == row["selected_fit_sha256"]
        assert not history.calls


@pytest.mark.anyio
async def test_T09_window_boundaries_full_activity_future_exclusion_and_missing_recent(history):
    raw = trace_fit(dense([(0, 0), (1500, 5000)]))
    end = absolute_time(START + 1500)
    async with server() as client:
        await upload(client, ('5k.fit', raw))
        async def fetch(cutoff):
            report = (await client.get('/api/efforts', params={"cutoff": cutoff.isoformat()})).json()
            return next(r for r in report["distances"] if r["distance_m"] == 5000)
        before = await fetch(end - timedelta(microseconds=1))
        assert before["all_time"] is None
        exact = await fetch(end + timedelta(days=112))
        assert next(r for r in exact["recent"] if r["days"] == 112)["state"] == "VALID"
        after = await fetch(end + timedelta(days=112, microseconds=1))
        assert after["all_time"]["elapsed_seconds"] == 1500
        assert next(r for r in after["recent"] if r["days"] == 112)["state"] == "MISSING_RECENT"


@pytest.mark.anyio
async def test_recent_boundary_straddling_activity_uses_later_eligible_interval(history):
    points = dense([(0, 0), (120, 400), (300, 800)])
    async with server() as client:
        await upload(client, ('straddle.fit', trace_fit(points)))
        cutoff = absolute_time(START + 240) + timedelta(days=112)
        report = (await client.get('/api/efforts', params={"cutoff": cutoff.isoformat()})).json()
        r = next(r for r in report["distances"] if r["distance_m"] == 400)
        recent = next(w for w in r["recent"] if w["days"] == 112)["best"]
        assert r["all_time"]["elapsed_seconds"] == 120
        assert recent and recent["elapsed_seconds"] == pytest.approx(160, abs=.002)
        assert effort_math.stamp(recent["end_utc"]) >= absolute_time(START + 240)


def test_recent_plateau_never_moves_first_arrival_to_cutoff():
    points = dense([(0, 0), (120, 400), (240, 400), (540, 1000)])
    lower = absolute_time(START + 200).isoformat()
    r = pure(points, [400], min_end=lower)["distances"][0]
    if r["best"]:
        assert effort_math.stamp(r["best"]["end_utc"]) >= absolute_time(START + 240)
        assert r["best"]["end_distance_m"] > 400
    assert not r["best"] or r["best"]["end_utc"] != lower


def test_recent_nonattained_infimum_withholds_exact_winner_and_keeps_attained_evidence():
    # Independent limit calculation: as end distance approaches 100m from above,
    # first arrival approaches 25s and last departure at distance 0+ approaches 0s.
    # 25s is not attained: at exactly 100m, first arrival is the ineligible 20s.
    points = dense([(0, 0), (20, 100), (25, 100), (60, 200)])
    r = pure(points, [100], min_end=absolute_time(START + 22).isoformat())["distances"][0]
    assert r["state"] == "REVIEW_RECENCY_LIMIT" and r["best"] is None
    assert r["unattained_limit"]["elapsed_seconds"] == 25
    assert r["unattained_limit"]["attainment"] == "ONE_SIDED_LIMIT"
    assert r["attained_candidate"]["elapsed_seconds"] > 25


@pytest.mark.anyio
async def test_global_recent_limit_prevents_a_slower_activity_from_being_called_exact_fastest(history):
    async with server() as client:
        await upload(client, ('limit.fit', trace_fit(dense([(0, 0), (20, 100), (25, 100), (60, 200)]))))
        await upload(client, ('slower.fit', trace_fit(dense([(0, 0), (30, 100)]), start=START + 86400)))
        config = {**efforts.DEFAULT_CONFIG, "distances_m": [100]}
        assert (await client.post('/api/efforts/recompute', json={"config_version": "synthetic-100", "config": config})).status_code == 200
        cutoff = absolute_time(START + 22) + timedelta(days=112)
        response = await client.get('/api/efforts', params={"cutoff": cutoff.isoformat()})
        r = response.json()["distances"][0]
        recent = next(w for w in r["recent"] if w["days"] == 112)
        assert recent["state"] == "REVIEW_RECENCY_LIMIT" and recent["best"] is None
        assert recent["unattained_limits"][0]["limit"]["elapsed_seconds"] == 25
        assert 25 < recent["attained_candidate"]["elapsed_seconds"] <= 30


@pytest.mark.anyio
async def test_whole_trace_elapsed_is_distinct_from_header_and_boundary_plateaus(history):
    points = dense([(0, 0), (5, 0), (305, 1000), (310, 1000)])
    async with server() as client:
        await upload(client, ('boundary.fit', trace_fit(points)))
        row = (await client.get('/api/activities/1/efforts')).json()
        r = distance(row, 1000)
        assert r["best"]["elapsed_seconds"] == 300
        assert r["best_whole_activity"]["elapsed_seconds"] == 310
        assert r["best_whole_activity"]["start_utc"] == absolute_time(START).isoformat()
        assert r["best_whole_activity"]["kind"] == "WHOLE_ACTIVITY"
        # A header which ends before future source rows cannot leak them through as-of selection.
        raw = trace_fit(dense([(0, 0), (300, 1000)]), start=START + 86400,
                        extra=[(20, {"timestamp": START + 86400 + 1000, "distance": 1000})])
        await upload(client, ('late-record.fit', raw))
        report = (await client.get('/api/efforts', params={"cutoff": absolute_time(START + 86400 + 400).isoformat()})).json()
        assert {"activity_id": 2, "reason": "FUTURE_ACTIVITY"} in report["excluded"]


@pytest.mark.anyio
async def test_context_categories_corrections_known_at_and_maximal_protocol_are_explicit(history):
    async with server() as client:
        await upload(client, ('test.fit', trace_fit(dense([(0, 0), (300, 1000)]))))
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row, 400)["best"]
        target = f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}'
        choice = {"category": "RACE", "tags": ["FATIGUED_BLOCK"], "actor": "Synthetic athlete", "note": "Recorded event context"}
        race = (await client.post(target + '/context', json=choice)).json()["candidate"]
        assert race["provenance"]["category"] == "RACE" and race["provenance"]["verified_maximal"] is False
        assert race["computed_provenance"]["category"] == "TRAINING_FAST"
        assert race["context_history"][0]["known_at"] > race["end_utc"]
        assert (await client.post(target + '/context', json={**choice, "maximal_confirmed": True})).status_code == 422
        test = (await client.post(target + '/context', json={**choice, "category": "VERIFIED_TEST", "maximal_confirmed": True,
                                                           "protocol": "Synthetic controlled rested benchmark protocol"})).json()["candidate"]
        assert test["provenance"]["verified_maximal"] is True
        assert test["provenance"]["scientific_validation"] == "UNVERIFIED"
        assert test["context_history"][1]["previous_context_id"] == race["context_history"][0]["id"]
        assert test["computed_quality_state"] == c["computed_quality_state"]


@pytest.mark.anyio
async def test_source_dedup_redirects_config_recompute_and_read_only_backfill(history, tmp_path):
    history.original_error = ConnectionProblem("Synthetic offline original.")
    async with server() as client:
        await submit(client)
        donor = (await client.get('/api/activities/1/efforts')).json()
        assert donor["inspected_source"] is None  # summary only is not an effort
        raw = trace_fit(dense([(0, 0), (300, 1000)]), start=START + 300)
        await upload(client, ('manual.fit', raw))
        history.original_error = None
        history.original = zipped(raw)
        pending = await submit(client)
        assert (await resolve(client, pending, {"action": "link", "activity_id": 2})).status_code == 200
        merged = (await client.get('/api/activities/2/efforts')).json()
        historical = (await client.get('/api/activities/1/efforts', params={"version": donor["version_id"]})).json()
        assert historical["activity_id"] == 2 and historical["activity_id_at_creation"] == 1
        assert historical["sources"] == donor["sources"]
        await upload(client, ('duplicate.fit', raw))
        assert (await client.get('/api/activities/2/efforts')).json()["version_id"] == merged["version_id"]
        report = (await client.get('/api/efforts?cutoff=2026-12-01T00:00:00Z')).json()
        assert [i["activity_id"] for i in report["inputs"]] == [2]
    store = Store(tmp_path / 'runs.sqlite3', Archive(tmp_path / 'archive'))
    with store.connect(write=True) as db:
        for table in ('effort_contexts', 'effort_reviews', 'effort_inspections', 'effort_selections', 'effort_heads', 'effort_versions', 'effort_configs'):
            db.execute('DROP TABLE ' + table)
    async with server() as client:
        before = digest((tmp_path / 'runs.sqlite3').read_bytes())
        assert (await client.get('/api/activities/2/efforts')).json()["state"] == "NOT_PROCESSED"
        assert digest((tmp_path / 'runs.sqlite3').read_bytes()) == before
        response = await client.post('/api/efforts/recompute', json={})
        assert response.status_code == 200
        assert (await client.get('/api/activities/2/efforts')).json()["state"] == "PROCESSED"


@pytest.mark.anyio
async def test_alternate_sources_and_garmin_corrections_refresh_extraction_without_overwriting(history, tmp_path):
    first = trace_fit(dense([(0, 0), (1500, 5000)]))
    second = trace_fit(dense([(0, 0), (1600, 5000)]), file_number=2)
    async with server() as client:
        await upload(client, ('first.fit', first))
        initial = (await client.get('/api/activities/1/efforts')).json()
        await upload(client, ('second.fit', second))
        current = (await client.get('/api/activities/1/efforts')).json()
        assert len(current["sources"]) == 2 and current["version_id"] != initial["version_id"]
        alternate = (await client.get('/api/activities/1/efforts', params={"source": digest(second)})).json()
        assert distance(alternate, 5000)["best"]["elapsed_seconds"] == 1600
        old = (await client.get('/api/activities/1/efforts', params={"version": initial["version_id"]})).json()
        assert old["sources"] == initial["sources"]
        history.original = zipped(first)
        pending = await submit(client)
        if pending["issues"]:
            relevant = [i for i in pending["issues"] if i.get("choices") or i.get("candidates")]
            if relevant:
                assert (await resolve(client, pending, {"action": "link", "activity_id": 1})).status_code == 200
        before = (await client.get('/api/activities/1/efforts')).json()
        history.detail["summaryDTO"]["distance"] = 8000
        await submit(client)
        corrected = (await client.get('/api/activities/1/efforts')).json()
        assert before["version_id"] != corrected["version_id"]
        assert distance(corrected, 5000)["best"] is None
        assert distance(corrected, 5000)["flagged_candidate"]["computed_quality_state"] == "SUSPECT"
        assert corrected["selected_source_version_id"] == before["selected_source_version_id"]
        assert (tmp_path / 'archive' / digest(first)).read_bytes() == first


@pytest.mark.anyio
async def test_config_replay_precision_audit_reject_no_qc_override_and_restart(history, tmp_path):
    async with server() as client:
        raw = trace_fit(dense([(0, 0), (1200, 4000)]))
        await upload(client, ('test.fit', raw))
        first = (await client.get('/api/activities/1/efforts')).json()
        mile = distance(first, 1609.344)["best"]
        assert mile["distance_m"] == 1609.344
        assert mile["elapsed_seconds"] == pytest.approx(482.8032, abs=.002)
        target = f'/api/activities/1/efforts/{first["version_id"]}/{mile["id"]}'
        for action in ('accept', 'annotate', 'reject'):
            response = await client.post(target + '/review', json={"action": action, "actor": "Synthetic reviewer", "note": '<script>kept as text</script>'})
            assert response.status_code == 200
        audited = (await client.get(target)).json()["candidate"]
        assert audited["computed_quality_state"] == "VALID" and audited["effective_quality_state"] == "INVALID"
        assert [r["action"] for r in audited["reviews"]] == ['accept', 'annotate', 'reject']
        assert audited["reviews"][2]["previous_review_id"] == audited["reviews"][1]["id"]
        assert '&lt;script&gt;' in (await client.get(target.removeprefix('/api'))).text
        selected = (await client.get('/api/efforts?cutoff=2026-01-01T00:00:00Z')).json()
        next_mile = next(r for r in selected["distances"] if r["distance_m"] == 1609.344)["all_time"]
        assert next_mile is None or next_mile["id"] != mile["id"]
        changed = {**efforts.DEFAULT_CONFIG, "whole_activity_tolerance_m": 0.5}
        response = await client.post('/api/activities/1/efforts/recompute', json={"config_version": "synthetic-g2-v2", "config": changed})
        assert response.status_code == 200
        second = (await client.get('/api/activities/1/efforts')).json()
        assert second["version_id"] != first["version_id"] and len(second["versions"]) == 2
        assert (await client.post('/api/activities/1/efforts/recompute', json={"config_version": "synthetic-g2-v2", "config": changed})).status_code == 200
        assert len((await client.get('/api/activities/1/efforts')).json()["versions"]) == 2
        assert (tmp_path / 'archive' / digest(raw)).read_bytes() == raw
    async with server() as client:
        historical = (await client.get('/api/activities/1/efforts', params={"version": first["version_id"]})).json()
        assert distance(historical, 1609.344)["best"]["review_verdict"] == "reject"


@pytest.mark.anyio
async def test_T21_qc_change_only_new_version_and_accept_does_not_clear_suspect(history):
    async with server() as client:
        await upload(client, ('plateau.fit', trace_fit(dense([(0, 0), (150, 500), (162, 500), (312, 1000)]))))
        first = (await client.get('/api/activities/1/efforts')).json()
        c = distance(first, 1000)["flagged_candidate"]
        assert c["computed_quality_state"] == "SUSPECT"
        target = f'/api/activities/1/efforts/{first["version_id"]}/{c["id"]}/review'
        accepted = await client.post(target, json={"action": "accept", "actor": "Tester", "note": "Arithmetic checked; unresolved plateau remains."})
        assert accepted.json()["candidate"]["effective_quality_state"] == "SUSPECT"
        config = {**DEFAULT_CONFIG, "stationary_review_s": 13}
        assert (await client.post('/api/activities/1/canonical/recompute', json={"config_version": "synthetic-13s", "config": config})).status_code == 200
        second = (await client.get('/api/activities/1/efforts')).json()
        assert distance(second, 1000)["best"]["computed_quality_state"] == "VALID"
        assert first["selected_source_version_id"] == second["selected_source_version_id"]
        assert first["version_id"] != second["version_id"]


@pytest.mark.anyio
async def test_invalid_inputs_ownership_summary_only_and_offline_cli(history, tmp_path):
    async with server() as client:
        await upload(client, ('one.fit', trace_fit(dense([(0, 0), (300, 1000)]))))
        await upload(client, ('two.fit', trace_fit(dense([(0, 0), (300, 1000)]), start=START + 86400)))
        a = (await client.get('/api/activities/1/efforts')).json()
        b = (await client.get('/api/activities/2/efforts')).json()
        c = distance(a, 400)["best"]
        assert (await client.get('/api/activities/2/efforts?version=' + a["version_id"])).status_code == 404
        assert (await client.get('/api/activities/1/efforts?source=' + b["selected_fit_sha256"])).status_code == 404
        assert (await client.post(f'/api/activities/2/efforts/{a["version_id"]}/{c["id"]}/review', json={"action": "accept", "actor": "A", "note": "B"})).status_code == 404
        for payload in ({"config_version": "g2-engineering-v1", "config": {**efforts.DEFAULT_CONFIG, "recent_days": 84}},
                        {"config_version": "bad", "config": {**efforts.DEFAULT_CONFIG, "distances_m": [True]}}):
            assert (await client.post('/api/efforts/recompute', json=payload)).status_code == 422
        for cutoff in ('yesterday', '2026-01-01T00:00:00'):
            assert (await client.get('/api/efforts', params={"cutoff": cutoff})).status_code == 422
        assert (await client.post('/api/activities/1/efforts/inspect', json={"version": a["version_id"], "source": a["selected_fit_sha256"], "start_distance_m": -1, "distance_m": 400})).status_code == 422
        assert (await client.post('/api/efforts/recompute', json={}, headers={"Origin": "https://elsewhere.invalid"})).status_code == 403
        ties = (await client.get('/api/efforts?cutoff=2026-01-01T00:00:00Z')).json()
        assert next(r for r in ties["distances"] if r["distance_m"] == 400)["all_time"]["activity_id"] == 1
    output = subprocess.run([sys.executable, '../efforts.py', '--database', str(tmp_path / 'runs.sqlite3'), '--archive', str(tmp_path / 'archive')], capture_output=True, text=True)
    assert output.returncode == 0, output.stderr
    assert len(json.loads(output.stdout)["activities"]) == 2


@pytest.mark.anyio
async def test_rejected_tied_interval_selects_next_attained_valid_interval(history):
    async with server() as client:
        await upload(client, ('constant.fit', trace_fit([(i, i * 4) for i in range(401)])))
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row, 400)["best"]
        assert c["start_distance_m"] == 0 and c["elapsed_seconds"] == 100
        await client.post(f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}/review', json={"action": "reject", "actor": "Synthetic reviewer", "note": "Reject the first interval only"})
        report = (await client.get('/api/efforts?cutoff=2026-01-01T00:00:00Z')).json()
        replacement = next(r for r in report["distances"] if r["distance_m"] == 400)["all_time"]
        assert replacement["elapsed_seconds"] == 100 and replacement["start_distance_m"] == 4
        assert replacement["id"] != c["id"]
