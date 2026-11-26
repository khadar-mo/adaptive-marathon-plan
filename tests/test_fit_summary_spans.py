"""Synthetic FIT summary ordering, duration precision and immutable upgrades."""
from datetime import timedelta

import pytest
from garmin_fit_sdk import Encoder

import canonical
import canonical_qc
import efforts
import recording
from fit_data import absolute_time
from fit_fixture import START
from storage import Archive, Store
from test_fit_import import upload
from test_imports import digest, history, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def summary_fit(order='first', *, elapsed=1500.375, tail=False, written_at='ordered'):
    encoder = Encoder()
    encoder.on_mesg(0, {'type': 4, 'manufacturer': 1, 'product': 1234,
                        'serial_number': 7654321, 'time_created': START})
    written = START if order == 'first' else START + 1500
    written = written if written_at == 'ordered' else written_at
    span = {'start_time': START, 'timestamp': written, 'total_distance': 5000,
            'total_timer_time': 1500}
    if elapsed is not None:
        span['total_elapsed_time'] = elapsed
    if written is None:
        span.pop('timestamp')
    summaries = [(18, {'sport': 1, **span}), (19, {'message_index': 0, **span})]
    records = [(20, {'timestamp': START + t, 'distance': t * 10 / 3}) for t in range(0, 1501, 5)]
    if tail:
        records.append((20, {'timestamp': START + 1501, 'distance': 5001}))
    for number, fields in (summaries + records if order == 'first' else records + summaries):
        encoder.on_mesg(number, fields)
    encoder.on_mesg(34, {'timestamp': START + 1500, 'local_timestamp': START + 1500, 'num_sessions': 1})
    return bytes(encoder.close())


@pytest.mark.parametrize('order', ['first', 'last'])
async def test_summary_order_uses_fractional_elapsed_span_and_same_effort(order, history):
    raw = summary_fit(order)
    end = (absolute_time(START) + timedelta(seconds=1500.375)).isoformat()
    async with server() as client:
        await upload(client, (f'summary-{order}.fit', raw))
        recorded = (await client.get('/api/activities/1/recording')).json()
        for span in (recorded['header'], recorded['laps'][0]):
            assert span['end_utc'] == end and span['elapsed_seconds'] == 1500.375
            assert span['end_basis'] == 'start_time_plus_total_elapsed_time'
            assert span['message_timestamp_utc'] == absolute_time(START if order == 'first' else START + 1500).isoformat()
        canonical_row = (await client.get('/api/activities/1/canonical')).json()
        assert canonical_row['qc']['state'] == 'VALID'
        effort = (await client.get('/api/activities/1/efforts')).json()
        target = next(r for r in effort['inspected_source']['result']['distances'] if r['distance_m'] == 5000)
        assert target['best']['elapsed_seconds'] == 1500 and target['state'] == 'VALID'
        for cutoff, eligible in ((absolute_time(START + 1500).isoformat(), False), (end, True)):
            bests = (await client.get('/api/efforts', params={'cutoff': cutoff})).json()
            target = next(r for r in bests['distances'] if r['distance_m'] == 5000)
            assert bool(target['all_time']) is eligible
        html = (await client.get('/activities/1/recording')).text
        assert 'Derived end (UTC)' in html and 'Message written (UTC)' in html
        assert end in html


async def test_missing_elapsed_does_not_substitute_summary_written_clock(history):
    async with server() as client:
        await upload(client, ('missing-elapsed.fit', summary_fit('last', elapsed=None)))
        recorded = (await client.get('/api/activities/1/recording')).json()
        for span in (recorded['header'], recorded['laps'][0]):
            assert span['end_utc'] is None and span['end_basis'] == 'NOT_PROVIDED'
            assert span['end_issue'] == 'MISSING_ELAPSED'
            assert span['message_timestamp_utc'] == absolute_time(START + 1500).isoformat()
        assert recorded['coverage']['lap_end_utc']['status'] == 'NOT_PROVIDED'
        qc = (await client.get('/api/activities/1/canonical')).json()['qc']
        assert qc['state'] == 'VALID'
        assert any(f['code'] == 'missing_header_elapsed' and f['effect'] == 'INFO' for f in qc['flags'])
        bests = (await client.get('/api/efforts', params={'cutoff': '2026-01-01T00:00:00Z'})).json()
        assert bests['excluded'][0]['reason'] == 'MISSING_SOURCE_ACTIVITY_END'
        assert all(r['all_time'] is None for r in bests['distances'])


@pytest.mark.parametrize('written,code', [(None, 'missing_summary_message_clock'), (42, 'invalid_summary_message_clock')])
async def test_summary_written_clock_is_independent_of_valid_span(history, written, code):
    async with server() as client:
        await upload(client, ('written-clock.fit', summary_fit(written_at=written)))
        row = (await client.get('/api/activities/1/canonical')).json()
        assert row['qc']['state'] == 'VALID'
        assert row['trace']['header']['message_timestamp_utc'] is None
        assert row['trace']['header']['end_utc'] == (absolute_time(START) + timedelta(seconds=1500.375)).isoformat()
        assert any(f['code'] == code and f['effect'] == 'INFO' for f in row['qc']['flags'])


async def test_later_record_only_flags_affected_raw_support_and_still_controls_cutoff(history):
    async with server() as client:
        await upload(client, ('late-tail.fit', summary_fit(tail=True)))
        row = (await client.get('/api/activities/1/efforts')).json()
        async def interval(start, target):
            response = await client.post('/api/activities/1/efforts/inspect', json={
                'version': row['version_id'], 'source': row['selected_fit_sha256'],
                'start_distance_m': start, 'distance_m': target})
            assert response.status_code == 200
            return response.json()['candidate']
        early = await interval(100, 400)
        assert early['computed_quality_state'] == 'VALID'
        # Its crossing lies before the derived end, but the interpolation bracket
        # depends on the anomalous tail at t=1501. Endpoint support must count.
        affected = await interval(4600.25, 400)
        assert affected['computed_quality_state'] == 'SUSPECT'
        flag = next(f for f in affected['flags'] if f['code'] == 'sample_outside_recorded_session')
        assert flag['sample_indices'] == [301] and flag['calculation']['difference_from_boundary_seconds'] == .625
        assert flag['sample_times_utc'] == [absolute_time(START + 1501).isoformat()]
        assert flag['raw_references'][0]['index'] == 301
        for seconds, eligible in ((1500.375, False), (1500.999999, False), (1501, True)):
            cutoff = (absolute_time(START) + timedelta(seconds=seconds)).isoformat()
            report = (await client.get('/api/efforts', params={'cutoff': cutoff})).json()
            target = next(r for r in report['distances'] if r['distance_m'] == 400)
            assert bool(target['all_time']) is eligible
            if not eligible:
                assert report['excluded'][0]['reason'] == 'FUTURE_ACTIVITY'


@pytest.mark.parametrize('fields,issue,basis', [
    ({'timestamp': START, 'total_elapsed_time': 1}, 'MISSING_START', 'NOT_PROVIDED'),
    ({'timestamp': START, 'start_time': 42, 'total_elapsed_time': 1}, 'INVALID_START', 'INVALID'),
    ({'timestamp': START, 'start_time': START, 'total_elapsed_time': -1}, 'INVALID_ELAPSED', 'INVALID'),
    ({'timestamp': START, 'start_time': START, 'total_elapsed_time': float('inf')}, 'INVALID_ELAPSED', 'INVALID'),
])
def test_invalid_summary_span_keeps_raw_inputs_and_never_uses_written_clock(fields, issue, basis):
    original = dict(fields)
    span = recording.summary_span(fields)
    assert span['end_utc'] is None and span['end_issue'] == issue and span['end_basis'] == basis
    assert fields == original


@pytest.mark.parametrize('endpoint', ['efforts', 'canonical'])
async def test_explicit_upgrade_keeps_legacy_versions_and_selected_settings(history, tmp_path, monkeypatch, endpoint):
    current_inspect = recording.inspect
    def legacy_inspect(*args, **kwargs):
        trace = current_inspect(*args, **kwargs)
        for span, fields in [(trace['header'], trace['header']['session']), *[(lap, lap['fields']) for lap in trace['laps']]]:
            span['end_utc'] = recording.utc(fields.get('timestamp'))
            for key in ('message_timestamp_utc', 'end_basis', 'end_issue'):
                span.pop(key)
        return trace
    custom_qc = {**canonical_qc.DEFAULT_CONFIG, 'max_sample_gap_s': 25}
    custom_efforts = {**efforts.DEFAULT_CONFIG, 'distances_m': [400, 5000], 'recent_days': 84}
    raw = summary_fit()
    store = Store(tmp_path/'runs.sqlite3', Archive(tmp_path/'archive'))
    async with server() as client:
        with monkeypatch.context() as old:
            old.setattr(recording, 'inspect', legacy_inspect)
            old.setattr(recording, 'INSPECTION_VERSION', 'g0-fit-inspection-v1')
            old.setattr(canonical, 'CANONICAL_VERSION', 'g1-canonical-v1')
            old.setattr(canonical, 'ALGORITHM_VERSION', 'g1-qc-v1')
            old.setattr(canonical_qc, 'ALGORITHM_VERSION', 'g1-qc-v1')
            await upload(client, ('legacy-summary-first.fit', raw))
            assert (await client.post('/api/activities/1/canonical/recompute', json={
                'config_version': 'custom-qc', 'config': custom_qc})).status_code == 200
            assert (await client.post('/api/activities/1/efforts/recompute', json={
                'config_version': 'custom-efforts', 'config': custom_efforts})).status_code == 200
            legacy_g1 = (await client.get('/api/activities/1/canonical')).json()
            legacy_g2 = (await client.get('/api/activities/1/efforts')).json()
        with store.history() as db:
            preserved = {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table}')]
                         for table in ('canonical_source_versions', 'canonical_activity_versions', 'canonical_selections',
                                       'canonical_qc_versions', 'effort_versions', 'effort_selections')}
        checksum = digest(store.path.read_bytes())
        for kind in ('canonical', 'efforts'):
            assert (await client.get(f'/api/activities/1/{kind}')).json()['recompute_required'] is True
            assert 'Recompute required:' in (await client.get(f'/activities/1/{kind}')).text
        assert 'Recompute required:' in (await client.get('/efforts')).text
        target = legacy_g2['inspected_source']['result']['distances'][0]
        candidate = target['best'] or target['flagged_candidate']
        bookmark = f'/activities/1/efforts/{legacy_g2["version_id"]}/{candidate["id"]}'
        inspected = (await client.get('/api' + bookmark)).json()
        assert inspected['recompute_required'] is True and inspected['candidate'] == candidate
        assert 'Recompute required:' in (await client.get(bookmark)).text
        bests = (await client.get('/api/efforts', params={'cutoff': '2026-01-01T00:00:00Z'})).json()
        assert bests['excluded'][0]['reason'] == 'SOURCE_NORMALIZATION_REBUILD_REQUIRED'
        assert all(r['all_time'] is None for r in bests['distances'])
        assert digest(store.path.read_bytes()) == checksum
        # A failed requested upgrade must not publish the corrected G1 head first.
        bad = await client.post('/api/activities/1/efforts/recompute', json={
            'config_version': 'bad-upgrade', 'config': {**custom_efforts, 'distances_m': [5e-324]}})
        assert bad.status_code == 422 and digest(store.path.read_bytes()) == checksum
        upgraded = await client.post(f'/api/activities/1/{endpoint}/recompute', json={})
        assert upgraded.status_code == 200
        g1 = (await client.get('/api/activities/1/canonical')).json()
        g2 = (await client.get('/api/activities/1/efforts')).json()
        assert not g1['recompute_required'] and not g2['recompute_required']
        assert g1['qc_config_version'] == 'custom-qc' and g1['qc_config'] == custom_qc
        assert g2['config_version'] == 'custom-efforts' and g2['config'] == custom_efforts
        assert g1['selected_fit_sha256'] == g2['selected_fit_sha256'] == digest(raw)
        assert g1['selected_source_version_id'] != legacy_g1['selected_source_version_id']
        assert g1['raw_sources'] == legacy_g1['raw_sources'] and g1['summary'] == legacy_g1['summary']
        assert g1['trace']['header']['end_utc'] == (absolute_time(START) + timedelta(seconds=1500.375)).isoformat()
        with store.history() as db:
            for table, previous in preserved.items():
                assert [tuple(r) for r in db.execute(f'SELECT * FROM {table}')][:len(previous)] == previous
        old = (await client.get('/api/activities/1/canonical', params={'version': legacy_g1['version_id']})).json()
        assert old['trace'] == legacy_g1['trace'] and old['parser'] == legacy_g1['parser']
        old = (await client.get('/api/activities/1/efforts', params={'version': legacy_g2['version_id']})).json()
        assert old['sources'] == legacy_g2['sources']
        assert (await client.get('/api' + bookmark)).json()['candidate'] == candidate
        assert 'Recompute required:' in (await client.get(bookmark)).text
        # Explicitly asking for the named default still changes the selection.
        assert (await client.post('/api/activities/1/canonical/recompute', json={
            'config_version': canonical.DEFAULT_CONFIG_VERSION})).status_code == 200
        assert (await client.get('/api/activities/1/canonical')).json()['qc_config'] == canonical_qc.DEFAULT_CONFIG
        assert (await client.post('/api/activities/1/efforts/recompute', json={
            'config_version': efforts.DEFAULT_CONFIG_VERSION})).status_code == 200
        assert (await client.get('/api/activities/1/efforts')).json()['config'] == efforts.DEFAULT_CONFIG
        assert (tmp_path/'archive'/digest(raw)).read_bytes() == raw and not history.calls
