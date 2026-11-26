"""Public raw-to-workload routes, input import, and source-consistent timing."""
from datetime import timedelta
import json

import pytest

import app
import canonical
from fit_data import absolute_time
from fit_fixture import START
from runs import ConnectionProblem
import workload
import workload_inputs as wi
from test_workload import coverage, profile, ledger
from test_imports import history, server, submit, DAY
from test_fit_import import upload, zipped
from test_fit_summary_spans import summary_fit

pytestmark=pytest.mark.anyio


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(wi,'now',lambda:'2026-11-26T12:00:00.000000+00:00')


async def get(client, **params):
    response=await client.get('/api/workload',params=params)
    assert response.status_code==200,response.text
    return response.json()


async def enter(client,value,status=200):
    response=await client.post('/api/workload/inputs',content=wi.encoded(value),headers={'Content-Type':'application/json'})
    assert response.status_code==status,response.text
    return response.json()


async def test_empty_read_only_no_credential_lookup_and_validated_requests(history,tmp_path):
    async with server() as client:
        data=await get(client)
        assert data['rolling'][0]['run_count']==0 and data['rolling'][0]['coverage']['status']=='INCOMPLETE_HISTORY'
        assert (await client.get('/workload')).status_code==200
        assert not (tmp_path/'runs.sqlite3').exists() and not history.calls
        for params in ({'as_of':'2026-10-01'}, {'mode':'MAGIC'}, {'proposed_start':'2026-10-01T10:00:00Z'},
                       {'proposed_start':'2026-10-01T10:00:00Z','proposed_distance_m':'NaN'},
                       {'as_of':'2026-10-01T00:00:00Z','knowledge_cutoff':'2026-11-01T00:00:00Z','mode':'AS_KNOWN'}):
            assert (await client.get('/api/workload',params=params)).status_code==422


async def test_summary_only_provider_runs_preserve_calendar_and_summary_completeness(history,tmp_path):
    history.original_error=ConnectionProblem('Synthetic original not supplied')
    async with server() as client:
        await submit(client)
        calendar=(await client.get('/api/history',params=DAY)).json()
        canonical_row=(await client.get('/api/activities/1/canonical')).json()
        assert canonical_row['header'] is None
        before=(tmp_path/'runs.sqlite3').read_bytes()
        data=await get(client,as_of='2024-02-29T12:00:00Z')
        assert data['rolling'][0]['measures']['distance_m']['known_subtotal']=='5001.25'
        row,=data['activities']
        assert row['timing_basis']=='CANONICAL_ACCEPTED_SUMMARY' and row['timing']['end_basis']=='start_utc_plus_reported_elapsed_seconds'
        assert row['measures']['elapsed_seconds']=='1801.1' and row['measures']['timer_seconds']=='1500.0'
        assert not data['rolling'][0]['coverage']['complete']
        assert (await client.get('/api/history',params=DAY)).json()==calendar
        assert (tmp_path/'runs.sqlite3').read_bytes()==before
        assertion=coverage(lower_utc='2024-02-01T00:00:00Z',upper_utc='2024-03-01T00:00:00Z',
                           observed_through='2024-03-01T00:00:00Z',reported_at='2024-03-01T01:00:00Z')
        saved=await enter(client,assertion)
        data=await get(client,as_of='2024-02-29T12:00:00Z')
        assert data['rolling'][0]['coverage']['complete']
        assert data['rolling'][0]['measures']['distance_m']['value']=='5001.25'
        assert (await enter(client,assertion))['replayed']
        assert (tmp_path/'archive'/saved['raw_sha256']).read_bytes()==wi.encoded(assertion).encode()
        assert '/workload' in (await client.get('/history')).text


async def test_t10_renamed_fit_counted_once_own_fractional_span_written_clock_and_snapshot(history):
    raw=summary_fit('first',elapsed=1500.375)
    async with server() as client:
        await upload(client,('first.fit',raw)); await upload(client,('renamed.fit',raw))
        end=(absolute_time(START)+timedelta(seconds=1500.375)).isoformat()
        data=await get(client,as_of=end)
        row,=data['activities']
        assert row['measures']['elapsed_seconds']=='1500.375'
        assert row['fit_message_written_utc']==absolute_time(START).isoformat()
        assert row['selected_fit_span']['end_utc']==wi.utc(end)
        assert data['rolling'][0]['run_count']==1
        before=await get(client,as_of=(absolute_time(START)+timedelta(seconds=1500.374999)).isoformat())
        assert before['rolling'][0]['run_count']==0
        frozen=await client.post('/api/workload/capture',json={'as_of':end,'knowledge_cutoff':data['knowledge_cutoff']})
        assert frozen.status_code==200,frozen.text
        value=frozen.json(); identity=value['snapshot_id']
        assert value['result']['input_hash']==data['input_hash']
        assert (await client.get('/api/workload/snapshots/'+identity+'/replay')).json()['status']=='IDENTICAL'
        assert (await client.get('/workload/snapshots/'+identity)).status_code==200
        assert (await client.get('/api/workload/snapshots/missing')).status_code==404


async def test_headline_vs_fit_span_membership_correction_uses_accepted_pair(history,monkeypatch):
    monkeypatch.setattr(canonical,'knowledge_time',lambda:'2026-11-01T12:00:00Z')
    history.original=zipped(summary_fit())
    async with server() as client:
        await submit(client)
        data=await get(client,as_of='2024-02-28T23:50:00Z')
        row,=data['activities']
        assert row['timing']['start_utc']=='2024-02-28T23:15:38.000000+00:00'
        assert row['timing']['end_utc']=='2024-02-28T23:45:39.100000+00:00'
        assert row['timing_disagreement']
        fit=row['selected_fit_span']
        assert fit['end_utc']!=row['timing']['end_utc']  # provider headline and original are distinct
        old=row['canonical_version_id']
        history.detail['summaryDTO'].update(elapsedDuration=7200,distance=6000.125)
        monkeypatch.setattr(canonical,'knowledge_time',lambda:'2026-11-02T12:00:00Z')
        await submit(client)
        result=await get(client,as_of='2024-02-28T23:50:00Z')
        assert not result['activities'] and result['excluded'][0]['reason']=='NOT_COMPLETED_BY_CUTOFF'
        old_view=await get(client,as_of='2024-02-28T23:50:00Z',knowledge_cutoff='2026-11-01T13:00:00Z')
        assert old_view['activities'][0]['canonical_version_id']==old
        assert old_view['activities'][0]['measures']['distance_m']=='5001.25'


async def test_public_coverage_dates_both_bases_dst_and_revision_conflict(history):
    async with server() as client:
        value=coverage(basis='BOTH')
        value.pop('lower_utc');value.pop('upper_utc')
        value.update(first='2026-10-19',to_exclusive='2026-10-26')
        response=await client.post('/api/workload/coverage-dates',json=value)
        assert response.status_code==200,response.text
        rows=response.json()['records']
        assert {r['basis'] for r in rows}=={'START','END'}
        assert all(r['lower_utc']=='2026-10-18T23:00:00.000000+00:00' and r['upper_utc']=='2026-10-26T00:00:00.000000+00:00' for r in rows)
        row=rows[0]
        changed={k:v for k,v in row.items() if k in wi.Coverage.model_fields}
        changed['status']='UNKNOWN'
        await enter(client,changed,409)
        changed.update(supersedes=row['version_id'],correction_reason='Synthetic revised census')
        revised=(await enter(client,changed))['records'][0]
        assert revised['revision']==2
        versions=(await client.get('/api/workload/inputs/'+row['record_id'])).json()['versions']
        assert len(versions)==2
        assert 'Revision 2' in (await client.get('/workload/inputs/'+row['record_id'])).text


async def test_public_calibration_and_band_import_precision_missing_method_and_known_time(history):
    async with server() as client:
        await upload(client,('sample.fit',summary_fit()))
        canonical_row=(await client.get('/api/activities/1/canonical')).json()
        p=(await enter(client,profile(effective_from='2020-01-01T00:00:00Z')))['records'][0]
        value=ledger(canonical_row['version_id'],p,seconds={'low':'1000.12345678901234567890123456789','middle':'300','high':'100'},unclassified_seconds='0')
        raw=wi.encoded(value).replace('"1000.12345678901234567890123456789"','1000.12345678901234567890123456789').encode()
        response=await client.post('/api/workload/import',files={'file':('synthetic.json',raw)})
        assert response.status_code==200,response.text
        assert response.json()['records'][0]['seconds']['low']=='1000.12345678901234567890123456789'
        data=await get(client,as_of=(absolute_time(START)+timedelta(days=1)).isoformat())
        group,=data['rolling'][0]['intensity']['groups']
        assert group['seconds']['low']=='1000.12345678901234567890123456789'
        assert group['unknown_seconds']=='99.87654321098765432109876543211'
        assert not (await get(client,as_of='2026-11-01T00:00:00Z',mode='AS_KNOWN'))['input_records']
        bad=profile(source_id='invalid',bands=[{'label':'one'}])
        await enter(client,bad,422)
        bad=ledger(canonical_row['version_id'],p,source_id='bad',seconds={'missing':'10'})
        await enter(client,bad,422)


async def test_legacy_provider_scan_capture_never_certifies_end_census(history):
    async with server() as client:
        await submit(client)
        response=await client.post('/api/workload/provider-coverage',json={'scan_id':1,'actor':'fixture','reason':'Freeze current synthetic scan'})
        assert response.status_code==200,response.text
        row,=response.json()['records']
        assert row['basis']=='RECORDED_LOCAL_DATE' and row['status']=='UNKNOWN'
        assert row['known_at']>=row['reported_at']
        assert not (await get(client,as_of='2024-02-29T12:00:00Z'))['rolling'][0]['coverage']['complete']
        assert app.app.state.archive.read(row['source_refs'][0]['raw_sha256'])


def test_fallback_timing_is_one_source_pair_and_invalid_pair_never_falls_back():
    payload={'activity_id_at_creation':1,'summary':{'start_utc':None,'end_utc':None,'distance_m':1000,'elapsed_seconds':None},
             'header':{'start_utc':'2026-10-25T00:30:00Z','end_utc':'2026-10-25T01:30:00Z',
                       'message_timestamp_utc':'2026-10-25T00:30:00Z','end_basis':'start_time_plus_total_elapsed_time'},
             'running_sport':True,'qc_config_version':'fixture'}
    def row(): return {'id':'fixture','payload':json.dumps(payload),'created_at':'2026-11-01T00:00:00Z'}
    selection={'selected_at':'2026-11-01T00:00:00Z'}
    observed=workload.activity(row(),selection)
    assert observed['timing_basis']=='SELECTED_FIT_OWN_SPAN_FALLBACK'
    assert observed['timing']['end_utc']=='2026-10-25T01:30:00.000000+00:00'
    assert observed['measures']['elapsed_seconds'] is None  # timing fallback does not invent a missing accepted measure
    payload['summary'].update(start_utc='2026-10-25T02:00:00Z',end_utc='2026-10-25T01:00:00Z')
    observed=workload.activity(row(),selection)
    assert observed['timing'] is None and observed['timing_issue']=='INVALID_TIMING'
