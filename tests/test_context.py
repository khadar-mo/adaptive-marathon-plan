"""Synthetic public interfaces for #29. Software evidence, not physiology."""
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
import context_data as context
from storage import Store
from test_imports import history, server, submit
from test_fit_import import upload, resolve, zipped
from test_canonical import trace_fit, add_run

pytestmark = pytest.mark.anyio

@pytest.fixture
def anyio_backend():
    return 'asyncio'

def wellness(**kw):
    return {'kind':'wellness','source':'owned-export','source_id':'rhr-1','local_date':'2026-10-25','timezone':'Europe/London','metric':'resting_hr','value':'48.12345678901234567890','unit':'bpm',**kw}

def report(**kw):
    return {'kind':'post_run','source':'self-report','source_id':'run-1','activity_id':1,'reported_at':'2026-10-25T14:00:00Z','ratings':{'rpe':{'value':'4.5','scale_version':'rpe-0-10-v1'}},**kw}

async def enter(client, value, status=200):
    response=await client.post('/api/context/records',content=context.encoded(value),headers={'Content-Type':'application/json'})
    assert response.status_code==status,response.text
    return response.json()

async def view(client, **params):
    response=await client.get('/api/context',params=params)
    assert response.status_code==200,response.text
    return response.json()

async def test_t13_missing_hrv_sleep_does_not_block_running_computations(history):
    async with server() as client:
        await upload(client,('made-up.fit',trace_fit()))
        before=(await client.get('/api/activities/1/canonical')).json()
        metrics=(await client.get('/api/activities/1')).json()
        data=await view(client)
        assert before['canonical_quality_state']=='VALID'
        assert metrics['distance_m']==9.75 and metrics['elapsed_seconds']==3
        assert data['wellness']['hrv']['state']==data['wellness']['sleep_duration']['state']=='MISSING'
        assert data['records']==data['attention_signals']==[]
        assert (await client.get('/api/context/feedback?activity_id=1')).json()['state']=='MISSING'
        assert (await client.get('/api/activities/1/canonical')).json()==before
        assert 'MISSING' in (await client.get('/context')).text

async def test_precision_raw_archive_unknown_time_idempotence_and_read_only(history,tmp_path):
    raw=context.encoded(wellness()).replace('"48.12345678901234567890"','48.12345678901234567890').encode()
    async with server() as client:
        response=await client.post('/api/context/import',files={'file':('owned.json',raw)})
        assert response.status_code==200,response.text
        saved=response.json(); archived=tmp_path/'archive'/saved['raw_sha256']
        assert archived.read_bytes()==raw and archived.stat().st_mode & 0o077==0
        assert (await client.post('/api/context/import',files={'file':('renamed.json',raw)})).json()['replayed']
        data=await view(client); row,=data['records']
        assert row['value']=='48.12345678901234567890' and row['unit']=='bpm'
        assert row['event_utc'] is None and row['observed_at'] is None
        assert row['time_precision']=='local_date_only' and row['event_time_cutoff_status']=='UNKNOWN_INSTANT'
        assert row['first_known_at']==row['known_at']>row['local_date']
        assert len(data['events'])==1 and data['events'][0]['event_utc'] is None
        assert row['sources'][0]['raw_sha256']==hashlib.sha256(raw).hexdigest()
        dbfile=tmp_path/'runs.sqlite3'; before=dbfile.read_bytes()
        for url in ('/context','/api/context',f'/context/records/{row["record_id"]}','/api/context/definitions'):
            assert (await client.get(url)).status_code==200
        assert dbfile.read_bytes()==before

async def test_t14_t15_correction_cutoff_and_old_import_replay(history,tmp_path,monkeypatch):
    clock=['2026-11-01T10:00:00.000000+00:00']; monkeypatch.setattr(context,'now',lambda:clock[0])
    async with server() as client:
        initial=await enter(client,wellness()); version=initial['records'][0]['version_id']
        frozen=await view(client,as_of='2026-11-01T11:00:00Z')
        clock[0]='2026-11-02T10:00:00.000000+00:00'
        await enter(client,wellness(value='50'),409)
        corrected=await enter(client,wellness(value='50',supersedes=version,correction_reason='Transcription correction',source_updated_at='2026-10-25T10:00:00Z'))
        assert corrected['records'][0]['revision']==2
        assert await view(client,as_of='2026-11-01T11:00:00Z')==frozen
        assert (await enter(client,wellness()))['replayed']
        current,=(await view(client))['records']
        assert current['value']=='50' and current['known_at']==clock[0] and current['first_known_at']<current['known_at']
        versions=(await client.get('/api/context/records/'+current['record_id'])).json()['versions']
        assert [r['value'] for r in versions]==['48.12345678901234567890','50']
        assert versions[1]['previous_version_id']==versions[0]['version_id']
        assert all((tmp_path/'archive'/r['sources'][0]['raw_sha256']).is_file() for r in versions)
        await enter(client,wellness(value='51',supersedes=version,correction_reason='Stale write'),409)
        await enter(client,[wellness(source_id='new'),wellness(source_id='wrong',supersedes=version,correction_reason='Wrong record')],409)
        assert len((await view(client))['records'])==1

@pytest.mark.parametrize(('timestamp','local','expected'),[
    ('2026-10-25T01:30:00+01:00','2026-10-25','2026-10-25T00:30:00.000000+00:00'),
    ('2026-10-25T01:30:00+00:00','2026-10-25','2026-10-25T01:30:00.000000+00:00'),
    ('2026-07-10T23:30:00Z','2026-07-11','2026-07-10T23:30:00.000000+00:00')])
async def test_dst_utc_local_boundaries(history,timestamp,local,expected):
    async with server() as client:
        await enter(client,wellness(local_date=local,observed_at=timestamp))
        row,=(await view(client))['records']
        assert row['observed_at']==timestamp and row['event_utc']==expected and row['local_date']==local

@pytest.mark.parametrize('change',[
    {'observed_at':'2026-10-25T01:30:00'},{'observed_at':'2026-10-26T01:30:00Z'},
    {'local_date':'2026-02-30'},{'timezone':'invented/zone'},{'value':'NaN'},{'value':True},
    {'value':'1e1000'},{'value':None},{'unit':''},{'known_at':'2020-01-01T00:00:00Z'},
    {'validity':'MISSING'},{'value':None,'validity':'MISSING'},{'unknown_field':'x'}])
async def test_bad_input_rejected_without_context_commit(history,change):
    async with server() as client:
        await enter(client,wellness(**change),422)
        assert (await view(client))['records']==[]

async def test_missing_observations_device_outputs_and_validity(history):
    async with server() as client:
        await enter(client,[wellness(metric='hrv',value=None,validity='MISSING',missing_reason='Export has no HRV'),
                            wellness(source_id='body',metric='body_battery',value='54',unit='device-score'),
                            wellness(source_id='status',metric='hrv_status',value='balanced',value_type='category',unit='category'),
                            wellness(source_id='suspect',validity='SUSPECT')])
        data=await view(client)
        assert data['wellness']['hrv']['state']=='MISSING' and data['wellness']['hrv']['observed_days']==0
        assert data['wellness']['resting_hr']['state']=='MISSING'
        assert data['attention_signals']==[] and data['clinical_interpretation']=='NOT_PERFORMED'
        assert all(r['interpretation']=='DEVICE_OUTPUT' for r in data['records'] if r['metric'] in ('body_battery','hrv_status'))
        assert any('Export has no HRV' in g['missing_reasons'] for g in data['daily_wellness'])

async def test_multiple_daily_observations_units_methods_and_versioned_aggregation(history):
    async with server() as client:
        await enter(client,[wellness(value='40.00000000000000000001'),wellness(source_id='second',value='50.00000000000000000002'),wellness(source_id='unit',value='0.8',unit='Hz'),wellness(source_id='method',value='60',method='other')])
        groups=(await view(client))['daily_wellness']
        assert len(groups)==3 and sum(g['state']=='MULTIPLE_OBSERVATIONS' for g in groups)==1
        result=await client.post('/api/context/definitions/policy/median-v1',json={'daily_aggregation':'median','expected_wellness_metrics':['resting_hr','sleep_duration']})
        assert result.status_code==200,result.text
        data=await view(client,policy_version='median-v1')
        median,=[g for g in data['daily_wellness'] if g['state']=='AGGREGATED']
        assert median['value']=='45.000000000000000000015'
        assert data['wellness']['resting_hr']['units']==['Hz','bpm']
        assert (await client.post('/api/context/definitions/policy/median-v1',json={'daily_aggregation':'retain_all'})).status_code==409
        assert len((await view(client))['records'])==4

async def test_scales_comments_pain_attention_and_missing_reports(history):
    async with server() as client:
        await upload(client,('run.fit',trace_fit()))
        await enter(client,report(attention=['distinct_pain','worsening_symptoms','illness'],pain_discomfort='Sharp discomfort',pain_location='left calf',context='after run',notes='<script>runner note</script>'))
        result=await client.post('/api/context/definitions/scale/exertion-6-20-v1',json={'name':'Alternative exertion','minimum':'6','maximum':'20','unit':'rating','description':'Runner-selected alternative scale'})
        assert result.status_code==200,result.text
        await enter(client,report(kind='preweek',source_id='week-1',activity_id=None,week='2026-10-26',ratings={'rpe':{'value':'13','scale_version':'exertion-6-20-v1'}}))
        data=await view(client)
        assert {r['kind'] for r in data['attention_signals']}=={'distinct_pain','worsening_symptoms','illness'}
        assert all(r['action']=='PLANNER_REVIEW_ONLY' for r in data['attention_signals'])
        assert {r['ratings']['rpe']['scale_version'] for r in data['records']}=={'rpe-0-10-v1','exertion-6-20-v1'}
        row,=[r for r in data['records'] if r['kind']=='post_run']
        assert row['pain_location']=='left calf' and row['context']=='after run'
        html=(await client.get('/context/records/'+row['record_id'])).text
        assert '<script>runner note</script>' not in html and '&lt;script&gt;runner note&lt;/script&gt;' in html
        assert (await client.get('/api/context/feedback?week=2026-11-02')).json()['state']=='MISSING'
        assert (await client.get('/api/context/feedback?activity_id=1')).json()['state']=='REPORTED'
        await enter(client,report(source_id='bad',ratings={'rpe':{'value':'11','scale_version':'rpe-0-10-v1'}}),422)
        await enter(client,report(source_id='bad',ratings={'rpe':{'value':'4','scale_version':'unknown-v1'}}),422)
        await enter(client,report(kind='preweek',activity_id=None,week='2026-10-27'),422)
        assert (await client.post('/api/context/definitions/scale/rpe-0-10-v1',json={'name':'Changed','minimum':'0','maximum':'5','unit':'rating','description':'Changed'})).status_code==409

async def test_sunday_readiness_target_is_not_event_time_and_current_day_not_complete(history,monkeypatch):
    monkeypatch.setattr(context,'now',lambda:'2026-10-25T15:00:00.000000+00:00')
    async with server() as client:
        await enter(client,report(kind='preweek',activity_id=None,week='2026-10-26'))
        await enter(client,wellness(source_id='future',local_date='2026-10-26'))
        await enter(client,wellness(source_id='later-time',observed_at='2026-10-25T17:00:00Z'))
        await enter(client,wellness(source_id='unknown-time'))
        data=await view(client,as_of='2026-10-25T16:00:00Z')
        assert {r['source_id'] for r in data['records']}=={'run-1','unknown-time'}
        assert {r['source_id'] for r in data['withheld']}=={'future','later-time'}
        assert (await view(client,as_of='2026-10-25T14:59:00Z'))['records']==[]
        assert all(g['complete_day_coverage']=='NOT_ESTABLISHED_BY_OBSERVATIONS' for g in data['daily_wellness'])

async def test_strength_isolation_and_later_soreness_availability(history,monkeypatch):
    clock=['2026-10-25T20:00:00.000000+00:00']; monkeypatch.setattr(context,'now',lambda:clock[0])
    strength={'kind':'strength','source':'self-report','source_id':'gym-1','local_date':'2026-10-25','observed_at':'2026-10-25T12:00:00Z','duration_s':'3600.25','status':'completed','lower_body_emphasis':'yes','exercises':[{'name':'Squat','sets':3,'reps':'5','load':'42.50','load_unit':'kg'}]}
    async with server() as client:
        await upload(client,('run.fit',trace_fit()))
        before=(await client.get('/api/history?from=2024-02-01&to=2026-11-01')).json()
        initial=await enter(client,strength); old=await view(client,as_of=clock[0])
        assert (await client.get('/api/history?from=2024-02-01&to=2026-11-01')).json()==before
        clock[0]='2026-10-26T20:00:00.000000+00:00'
        change={**strength,'supersedes':initial['records'][0]['version_id'],'correction_reason':'Added next-day soreness','ratings':{'soreness':{'value':'3','scale_version':'rpe-0-10-v1'}},'soreness_reported_at':'2026-10-26T10:00:00Z'}
        await enter(client,change)
        assert await view(client,as_of='2026-10-25T20:00:00Z')==old
        data=await view(client)
        assert data['strength_running_distance_m']=='0' and data['report_counts']['strength']==1
        assert data['records'][0]['exercises'][0]['load']=='42.50' and data['events'][0]['kind']=='STRENGTH'
        await enter(client,{**strength,'source_id':'future-soreness','ratings':change['ratings'],'soreness_reported_at':'2026-10-27T10:00:00Z'})
        assert len((await view(client))['withheld'])==1
        await enter(client,{**strength,'source_id':'invalid','exercises':[{'name':'Squat','load':'12.2'}]},422)
        await enter(client,{**strength,'source_id':'negative','duration_s':'-1'},422)

async def test_source_coverage_requires_explicit_evidence_and_complete_days(history,monkeypatch):
    monkeypatch.setattr(context,'now',lambda:'2026-10-25T13:00:00.000000+00:00')
    coverage={'kind':'coverage','source':'self-report','source_id':'coverage-1','domain':'wellness','metric':'sleep_duration','first':'2026-10-20','last':'2026-10-24','status':'COMPLETE','basis':'athlete_attestation','reported_at':'2026-10-25T10:00:00Z','reason':'Athlete attests export completeness.'}
    async with server() as client:
        await enter(client,wellness()); await enter(client,coverage)
        await enter(client,{**coverage,'source_id':'current-day','last':'2026-10-25'},422)
        await enter(client,{**coverage,'source_id':'unavailable','metric':'hrv','status':'UNAVAILABLE','last':'2026-10-25','reason':'Export lacks HRV.'})
        data=await view(client); rows=[r for r in data['records'] if r['kind']=='coverage']
        assert len(rows)==2 and {r['status'] for r in rows}=={'COMPLETE','UNAVAILABLE'}
        assert data['wellness']['sleep_duration']['state']=='MISSING'
        assert data['wellness']['resting_hr']['coverage']=='UNKNOWN_UNLESS_EXPLICITLY_REPORTED'

async def test_csv_source_precision_and_request_security(history,tmp_path):
    output=io.StringIO(); row=wellness(); writer=csv.DictWriter(output,fieldnames=row.keys()); writer.writeheader(); writer.writerow(row); raw=output.getvalue().encode()
    async with server() as client:
        assert (await client.post('/api/context/records',json=wellness(),headers={'Origin':'https://example.invalid'})).status_code==403
        response=await client.post('/api/context/import?format=csv',files={'file':('owned.csv',raw)})
        assert response.status_code==200,response.text
        row,=(await view(client))['records']; assert row['value']=='48.12345678901234567890'
        assert (tmp_path/'archive'/response.json()['raw_sha256']).read_bytes()==raw
        assert (await client.post('/api/context/import?format=csv',files={'file':('bad.csv',b'kind,kind\nwellness,wellness\n')})).status_code==422
        html=(await client.get('/context?edit='+row['record_id'])).text
        assert 'Save correction' in html and row['version_id'] in html and 'Correction reason' in html
        assert (await client.get('/context/records/'+'0'*64)).status_code==404
        assert (await client.get('/api/context?as_of=2026-01-01')).status_code==422
        assert (await client.post('/api/context/records',content='[')).status_code==422
        assert (await client.post('/api/context/records',content='x'*(2*1024*1024+1))).status_code==413

async def test_feedback_survives_explicit_donor_merge(history):
    from runs import ConnectionProblem
    from fit_fixture import START
    history.original_error=ConnectionProblem("Synthetic unavailable original.")
    async with server() as client:
        await submit(client)
        initial=await enter(client,report(source_id='donor'))
        raw=trace_fit(start=START+300)
        await upload(client,('manual.fit',raw))
        history.original_error=None; history.original=zipped(raw)
        pending=await submit(client)
        assert (await resolve(client,pending,{'action':'link','activity_id':2})).status_code==200
        row=(await client.get('/api/context/records/'+initial['records'][0]['record_id'])).json()['versions'][0]
        assert row['activity_id']==1 and row['target_at_entry']['activity_id']==1 and row['target_now']['activity_id']==2
        assert len((await client.get('/api/context/feedback?activity_id=2')).json()['reports'])==1

@pytest.mark.parametrize('edit',['sport','date'])
async def test_feedback_stays_on_session_when_alias_reclassified_or_moved(history,edit):
    week={'from':'2026-10-19','to':'2026-10-25'}
    history.listed=[]; add_run(history,91,'2026-10-20'); history.original=zipped(trace_fit())
    async with server() as client:
        await submit(client,week)
        history.listed=[]; add_run(history,92,'2026-10-21'); history.original=zipped(trace_fit(file_number=2))
        pending=await submit(client,week); assert (await resolve(client,pending,{'action':'link','activity_id':1})).status_code==200
        saved=await enter(client,report())
        history.listed=[]; add_run(history,91,'2026-10-20',sport='cycling'); await submit(client,week)
        history.listed=[]; add_run(history,92,'2026-10-21',sport='cycling' if edit=='sport' else 'running')
        if edit=='date': history.extra_details['92']['summaryDTO']['startTimeLocal']='2026-10-29 00:15:00'
        await submit(client,week)
        row=(await client.get('/api/context/records/'+saved['records'][0]['record_id'])).json()['versions'][0]
        assert row['activity_id']==1 and row['target_at_entry']['sport']=='running'
        assert row['target_now']['sport']==('cycling' if edit=='sport' else 'running') and row['version_id']==saved['records'][0]['version_id']
        assert len((await client.get('/api/context/feedback?activity_id=1')).json()['reports'])==1

def test_concurrent_duplicates_and_corrections_are_serialized(tmp_path):
    store=Store(tmp_path/'context.sqlite3'); raw=context.encoded(wellness()).encode()
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda _:context.ingest(store,raw),range(4)))
    assert len({r['records'][0]['version_id'] for r in results})==1
    version=results[0]['records'][0]['version_id']; record_id=results[0]['records'][0]['record_id']
    def update(value):
        try: return context.ingest(store,context.encoded(wellness(value=value,supersedes=version,correction_reason='Concurrent correction')).encode())
        except context.Conflict: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(update,('50','51')))
    assert results.count('conflict')==1 and len(context.history(store,record_id)['versions'])==2

def test_cli_import_inspect_restart_without_provider(tmp_path):
    source=tmp_path/'synthetic.json'; source.write_text(context.encoded(wellness())); db=tmp_path/'context.sqlite3'
    script=Path(__file__).resolve().parents[1]/'context_data.py'; command=[sys.executable,str(script),'--database',str(db)]
    env={**os.environ,'RUNS_GARMIN_OFFLINE':'1'}
    imported=subprocess.run(command+['import',str(source)],cwd=tmp_path,env=env,text=True,capture_output=True,timeout=10)
    assert imported.returncode==0,imported.stderr
    shown=subprocess.run(command+['show'],cwd=tmp_path,env=env,text=True,capture_output=True,timeout=10)
    assert shown.returncode==0,shown.stderr
    assert json.loads(shown.stdout)['records'][0]['value']=='48.12345678901234567890'
    assert json.loads(imported.stdout)['records'][0]['revision']==1

async def test_event_log_has_every_correction_and_stable_cursor(history,monkeypatch):
    clock=['2026-11-01T10:00:00.000000+00:00']; monkeypatch.setattr(context,'now',lambda:clock[0])
    async with server() as client:
        initial=await enter(client,wellness())
        first=(await client.get('/api/context/events?limit=1')).json()
        assert first['next_after']==1 and first['events'][0]['version_id']==initial['records'][0]['version_id']
        clock[0]='2026-11-02T10:00:00.000000+00:00'
        correction=await enter(client,wellness(value='50',supersedes=initial['records'][0]['version_id'],correction_reason='Corrected'))
        events=(await client.get('/api/context/events?after=1')).json()
        assert len(events['events'])==1 and events['events'][0]['previous_version_id']==initial['records'][0]['version_id']
        assert events['events'][0]['version_id']==correction['records'][0]['version_id']
        assert events['events'][0]['sources'][0]['raw_sha256']==correction['raw_sha256']
        assert (await client.get('/api/context/events',params={'as_of':first['as_of']})).json()['events']==first['events']
        await enter(client,wellness())
        assert len((await client.get('/api/context/events')).json()['events'])==2
        assert (await client.get('/api/context/events?after=-1')).status_code==422

async def test_one_checkin_per_source_and_explicit_missing_report(history):
    async with server() as client:
        await upload(client,('run.fit',trace_fit()))
        await enter(client,report(ratings={},missing_reason='Runner did not supply a check-in'))
        response=(await client.get('/api/context/feedback?activity_id=1')).json()
        assert response['state']=='MISSING' and response['reports'][0]['missing_reason']=='Runner did not supply a check-in'
        await enter(client,report(source_id='another-checkin'),409)
        assert 'MISSING' in (await client.get('/context/feedback?activity_id=1')).text
        await enter(client,report(kind='preweek',source_id='empty-week',activity_id=None,week='2026-10-26',ratings={}),422)

async def test_future_definition_unavailable_at_cutoff(history,monkeypatch):
    monkeypatch.setattr(context,'now',lambda:'2026-11-02T10:00:00.000000+00:00')
    async with server() as client:
        result=await client.post('/api/context/definitions/policy/custom-v1',json={'daily_aggregation':'median'})
        assert result.status_code==200
        assert (await client.get('/api/context',params={'as_of':'2026-11-01T10:00:00Z','policy_version':'custom-v1'})).status_code==422

async def test_blank_browser_filters_and_duplicate_json_fields(history):
    async with server() as client:
        assert (await client.get('/context?as_of=&first=&last=&policy_version=context-policy-v1')).status_code==200
        raw=context.encoded(wellness()).replace('"value":','"value":"1","value":')
        response=await client.post('/api/context/records',content=raw)
        assert response.status_code==422 and 'Duplicate JSON fields' in response.text

async def test_coverage_intersects_selected_observation_days_and_csv_nested_fields(history):
    coverage={'kind':'coverage','source':'owned-export','source_id':'manifest','domain':'wellness','first':'2026-10-20','last':'2026-10-24','status':'COMPLETE','basis':'import_manifest','reported_at':'2026-10-25T10:00:00Z','reason':'Synthetic manifest after exported interval'}
    strength={'kind':'strength','source':'self-report','source_id':'csv-gym','local_date':'2026-10-25','status':'completed','duration_s':'3600.1234567890123456789','exercises':json.dumps([{'name':'Squat','sets':3,'load':'40.25','load_unit':'kg'}]),'ratings':json.dumps({'rpe':{'value':'4.5','scale_version':'rpe-0-10-v1'}})}
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=strength.keys()); writer.writeheader(); writer.writerow(strength)
    async with server() as client:
        await enter(client,coverage)
        data=await view(client,first='2026-10-21',last='2026-10-22')
        assert data['wellness']['hrv']['coverage_reports'][0]['status']=='COMPLETE'
        assert data['wellness']['hrv']['state']=='MISSING'
        result=await client.post('/api/context/import?format=csv',files={'file':('strength.csv',out.getvalue().encode())})
        assert result.status_code==200,result.text
        row,=[r for r in (await view(client))['records'] if r['kind']=='strength']
        assert row['duration_s']=='3600.1234567890123456789' and row['exercises'][0]['sets']==3
        assert row['ratings']['rpe']['value']=='4.5'
