"""Readiness interpretation through existing context entry and G6 snapshot APIs."""
from datetime import date, timedelta

import pytest

import context_data
from test_context import enter, wellness
from test_fitness import configure
from test_imports import history, server

pytestmark = pytest.mark.anyio
T = '2026-10-25T18:00:00Z'
WEEK = '2026-10-26'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def capture(client, **kw):
    response=await client.post('/api/snapshots',json={'as_of':T,'mode':'EVENT_TIME_RECONSTRUCTION',**kw})
    assert response.status_code==200,response.text
    return response.json()


async def scale(client):
    value={'name':'Synthetic fatigue scale','minimum':'0','maximum':'10','unit':'rating','description':'Synthetic runner-defined fatigue, not a clinical scale'}
    response=await client.post('/api/context/definitions/scale/fixture-fatigue-v1',json=value)
    assert response.status_code==200,response.text


def config(**kw):
    return {'ordinary_basis':'SUBJECTIVE_WITH_LIMITATIONS','report_max_age_days':7,
            'subjective_rules':[{'id':'preweek-fatigue','kind':'preweek','source':'self-report','field':'fatigue','scale_version':'fixture-fatigue-v1',
                                 'ordinary':{'low':'0','high':'2'},'concern':{'low':'7','high':'10'}}],**kw}


async def policy(client, value=None, version='fixture-readiness-v1'):
    response=await client.post('/api/readiness/policies',content=context_data.encoded({'version':version,'actor':'Synthetic runner','reason':'Synthetic explicit range fixture; no clinical threshold','config':value or config()}))
    assert response.status_code==200,response.text
    return response.json()


def preweek(**kw):
    return {'kind':'preweek','source':'self-report','source_id':'check-in','week':WEEK,'reported_at':'2026-10-25T12:00:00Z',
            'ratings':{'fatigue':{'value':'1','scale_version':'fixture-fatigue-v1'}},**kw}


async def interpret(client, snapshot, **kw):
    response=await client.get('/api/snapshots/'+snapshot['id']+'/readiness',params={'week':WEEK,**kw})
    assert response.status_code==200,response.text
    return response.json()


async def test_empty_and_explicit_ordinary_report_replay_survives_late_correction(history,tmp_path):
    async with server() as client:
        empty=await capture(client)
        assert (await interpret(client,empty))['state']=='UNKNOWN'
        await scale(client); await policy(client)
        first=(await enter(client,preweek()))['records'][0]
        saved=await capture(client)
        ordinary=await interpret(client,saved,policy_version='fixture-readiness-v1')
        assert ordinary['state']=='ORDINARY' and ordinary['policy_time_basis']=='POLICY_KNOWN_BY_SNAPSHOT_K'
        assert ordinary['runner_reports'][0]['record']['week']==WEEK
        assert (await interpret(client,saved,week='2026-11-02',policy_version='fixture-readiness-v1'))['state']=='UNKNOWN'
        before=(tmp_path/'runs.sqlite3').read_bytes()
        page=await client.get('/readiness',params={'snapshot_id':saved['id'],'week':WEEK,'policy_version':'fixture-readiness-v1'})
        assert page.status_code==200 and 'Readiness: ORDINARY' in page.text
        assert (tmp_path/'runs.sqlite3').read_bytes()==before
        await enter(client,preweek(attention=['illness'],supersedes=first['version_id'],correction_reason='Later symptom report'))
        newer=await capture(client)
        assert (await interpret(client,newer,policy_version='fixture-readiness-v1'))['state']=='PAIN_OR_ILLNESS_REVIEW'
        assert await interpret(client,saved,policy_version='fixture-readiness-v1')==ordinary
        assert (await client.get('/api/snapshots/'+saved['id']+'/replay')).json()['status']=='IDENTICAL'


async def test_frozen_g5_baseline_and_contradictory_device_report(history):
    stream={'metric':'resting_hr','unit':'bpm','source':'watch','method':'waking','calibration':'seated'}
    async with server() as client:
        await scale(client)
        await configure(client,{'robust_deviations':[{'stream':stream,'epsilon':'0.125','direction':'HIGHER'}]})
        rows=[wellness(**stream,source_id=f'day-{day}',local_date=str(date(2026,10,25)-timedelta(days=day)),value='48') for day in range(1,15)]
        rows.append(wellness(**stream,source_id='today',local_date='2026-10-25',observed_at='2026-10-25T12:00:00Z',value='49.000000000000000000001'))
        await enter(client,rows); await enter(client,preweek())
        snap=await capture(client,config_version='synthetic-v1')
        original=(await client.get('/api/snapshots/'+snap['id'])).json()
        source=original['fitness']['recovery']['wellness']['resting_hr']['streams'][0]
        assert source['baseline']=='48' and source['observed_days']==14
        assert source['robust_deviation']['value']=='8.000000000000000000008'
        await policy(client,config(wellness_max_age_days=0,wellness_rules=[{'id':'rhr','stream':source['stream'],'direction':'HIGHER','adverse_at_z':'4','ordinary_abs_z_max':'1'}]))
        result=await interpret(client,snap,policy_version='fixture-readiness-v1')
        assert result['state']=='MIXED' and result['policy_time_basis']=='LATER_POLICY_REANALYSIS'
        assert result['wellness_evidence'][0]['frozen_g5_stream']==source
        assert result['wellness_evidence'][0]['days'][0]['g5_descriptor']['robust_deviation']['mad']=='0'
        assert result['runner_reports'][0]['state']=='ORDINARY'
        assert (await client.get('/api/snapshots/'+snap['id'])).json()==original
