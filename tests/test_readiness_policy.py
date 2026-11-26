"""Public immutable policy/interpretation storage, replay and invalid input."""
import json

import pytest
import readiness
from test_context import enter
from test_imports import history, server
from test_readiness import WEEK, capture, config, interpret, policy, preweek, scale

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def test_policy_precise_numbers_immutable_artifact_restart_and_old_replay(history,tmp_path,monkeypatch):
    exact='1.123456789012345678901234567890'
    async with server() as client:
        await scale(client)
        cfg=config(); cfg['subjective_rules'][0]['ordinary']['high']=exact
        # Deliberately send an unquoted JSON number, not a float-rounded Python value.
        body=json.dumps({'version':'precise-v1','actor':'Synthetic runner','reason':'Explicit threshold fixture','config':cfg}).replace('"'+exact+'"',exact)
        first=await client.post('/api/readiness/policies',content=body)
        assert first.status_code==200,first.text
        assert first.json()['config']['subjective_rules'][0]['ordinary']['high']==exact
        assert (await client.post('/api/readiness/policies',content=body)).json()==first.json()
        await enter(client,preweek())
        snap=await capture(client)
        choice={'snapshot_id':snap['id'],'policy_version':'precise-v1','scope':{'week':WEEK}}
        saved=(await client.post('/api/readiness/records',json=choice)).json()
        assert saved['result']['state']=='ORDINARY'
        assert (await client.post('/api/readiness/records',json=choice)).json()==saved
        assert (await client.get('/api/readiness/records/'+saved['id']+'/replay')).json()['status']=='IDENTICAL'
        changed={**json.loads(body),'reason':'Different explanation'}
        assert (await client.post('/api/readiness/policies',json=changed)).status_code==409
        await policy(client,config(ordinary_basis='UNSPECIFIED'),'new-unconfigured')
        assert (await interpret(client,snap,policy_version='new-unconfigured'))['state']=='UNKNOWN'
    async with server() as client:
        assert (await client.get('/api/readiness/records/'+saved['id'])).json()==saved
        assert (await client.get('/api/readiness/records/'+saved['id']+'/replay')).json()['status']=='IDENTICAL'
        assert (await client.post('/api/readiness/records',json=choice)).json()==saved
        before=(tmp_path/'runs.sqlite3').read_bytes()
        response=await client.get('/readiness',params={'snapshot_id':snap['id'],'week':WEEK,'activity_id':'','policy_version':'precise-v1'})
        assert response.status_code==200 and 'Readiness: ORDINARY' in response.text
        assert (tmp_path/'runs.sqlite3').read_bytes()==before
        with monkeypatch.context() as changed_code:
            changed_code.setattr(readiness,'code_hash',lambda:'0'*64)
            assert (await client.get('/api/readiness/records/'+saved['id']+'/replay')).json()['status']=='REQUIRES_RECORDED_CODE'
            assert (await client.get('/api/readiness/records/'+saved['id'])).json()==saved


@pytest.mark.parametrize('change',[
    {'ordinary_basis':'Looks fine'}, {'consecutive_report_count':1}, {'consecutive_report_count':True},
    {'post_run_lookback_days':0}, {'report_max_age_days':-1}, {'wellness_max_age_days':367},
    {'subjective_rules':[{'id':'bad','kind':'preweek','source':'self-report','field':'fatigue','scale_version':'not-registered','ordinary':{'low':0,'high':1}}]},
    {'subjective_rules':[{'id':'bad','kind':'preweek','source':'self-report','field':'fatigue','scale_version':'fixture-fatigue-v1','ordinary':{'low':0,'high':8},'concern':{'low':7,'high':10}}]},
    {'subjective_rules':[{'id':'bad','kind':'preweek','source':'self-report','field':'fatigue','scale_version':'fixture-fatigue-v1','ordinary':{'low':0,'high':11}}]},
    {'subjective_rules':[],'arbitrary_threshold':4},
])
async def test_invalid_policy_is_atomic_422(history,change):
    async with server() as client:
        await scale(client)
        before=(await client.get('/api/readiness/policies')).json()
        response=await client.post('/api/readiness/policies',json={'version':'invalid','actor':'Synthetic runner','reason':'Invalid fixture','config':config(**change)})
        assert response.status_code==422,response.text
        assert (await client.get('/api/readiness/policies')).json()==before


async def test_invalid_body_scope_bounds_and_foreign_origin(history):
    async with server() as client:
        assert (await client.get('/readiness')).status_code==200
        snap=await capture(client)
        url='/api/snapshots/'+snap['id']+'/readiness'
        for params in ({},{'week':'2026-10-25'},{'week':'20261026'},{'week':WEEK,'activity_id':1},{'activity_id':0},
                       {'activity_id':-1},{'activity_id':2**63},{'activity_id':2**63-1},{'activity_id':'NaN'},
                       {'week':WEEK,'policy_version':'missing'}, {'week':'9999-12-27'}):
            assert (await client.get(url,params=params)).status_code==422,params
        for value in ('null','[]','{"config":NaN}','{"config":{},"config":{}}'):
            assert (await client.post('/api/readiness/policies',content=value)).status_code==422
            assert (await client.post('/api/readiness/records',content=value)).status_code==422
        assert (await client.post('/api/readiness/policies',json={'version':'blank','actor':' ','reason':'fixture','config':{}})).status_code==422
        assert (await client.post('/api/readiness/records',json={'snapshot_id':snap['id'],'scope':{'week':WEEK}},headers={'Origin':'https://elsewhere.invalid'})).status_code==403
        assert (await client.get('/api/readiness/records/'+'0'*64)).status_code==404
        assert (await client.get('/api/snapshots/'+'0'*64+'/readiness',params={'week':WEEK})).status_code==404
