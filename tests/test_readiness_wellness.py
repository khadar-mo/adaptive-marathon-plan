"""G5 frozen compatible daily baselines, missingness and explicit directions."""
from datetime import date, timedelta

import pytest
from test_context import enter, wellness
from test_fitness import configure
from test_imports import history, server
from test_readiness import capture, config, interpret, policy, preweek, scale

pytestmark = pytest.mark.anyio
STREAM = {'metric':'resting_hr','unit':'bpm','source':'watch','method':'waking','calibration':'seated'}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def fixture(client, values, *, days=14, direction='HIGHER', epsilon='0.125', missing=()):
    await scale(client)
    await configure(client,{'robust_deviations':[{'stream':STREAM,'epsilon':epsilon,'direction':direction}]})
    first=date(2026,10,25)-timedelta(days=len(values)-1)
    records=[wellness(**STREAM,source_id=f'baseline-{d}',local_date=str(first-timedelta(days=d)),value='48') for d in range(1,days+1)]
    records += [wellness(**STREAM,source_id=f'observed-{d}',local_date=str(first+timedelta(days=d)),
                        observed_at=str(first+timedelta(days=d))+'T12:00:00Z',value=value) for d,value in enumerate(values) if d not in missing]
    await enter(client,records)
    await enter(client,preweek())
    snap=await capture(client,config_version='synthetic-v1')
    full=(await client.get('/api/snapshots/'+snap['id'])).json()
    stream=full['fitness']['recovery']['wellness']['resting_hr']['streams'][0]['stream']
    return snap,stream


async def wellness_policy(client,stream,*,direction='HIGHER',version='fixture-readiness-v1',**kw):
    return await policy(client,config(**{'wellness_max_age_days':1,'consecutive_wellness_days':2,
        'wellness_rules':[{'id':'rhr','stream':stream,'direction':direction,'adverse_at_z':'4','ordinary_abs_z_max':'1'}],**kw}),version)


@pytest.mark.parametrize(('values','missing','expected'),[
    (['49','49'],(),'PERSISTENT_CONCERN'),
    (['49','48'],(),'ORDINARY'),
    (['49','48','49'],(1,),'MIXED'),
])
async def test_consecutive_dated_deviation_uses_robust_G5_and_missing_days_break(history,values,missing,expected):
    async with server() as client:
        snap,stream=await fixture(client,values,missing=missing)
        await wellness_policy(client,stream)
        result=await interpret(client,snap,policy_version='fixture-readiness-v1')
        assert result['state']==expected
        evidence=result['wellness_evidence'][0]
        assert evidence['days'][-1]['g5_descriptor']==evidence['frozen_g5_stream']
        assert evidence['days'][-1]['g5_descriptor']['robust_deviation']['rule']['epsilon']=='0.125'
        if missing:
            assert evidence['days'][0]['level']=='UNKNOWN'
            assert evidence['days'][0]['reason']=='CURRENT_MISSING'
            assert not result['persistence']


@pytest.mark.parametrize(('direction','value','configured','expected'),[
    ('LOWER','47','LOWER','MIXED'),
    ('BIDIRECTIONAL','47','BIDIRECTIONAL','MIXED'),
    ('HIGHER','47','HIGHER','UNKNOWN'),
    ('HIGHER','49','LOWER','UNKNOWN'),
])
async def test_directions_are_explicit_and_compatible(history,direction,value,configured,expected):
    async with server() as client:
        snap,stream=await fixture(client,[value],direction=direction)
        await wellness_policy(client,stream,direction=configured,ordinary_basis='SUBJECTIVE_AND_WELLNESS')
        result=await interpret(client,snap,policy_version='fixture-readiness-v1')
        assert result['state']==expected
        if direction!=configured:
            assert result['wellness_evidence'][0]['days'][-1]['reason']=='INCOMPATIBLE_G5_DIRECTION'


async def test_minimum_days_units_freshness_and_target_relevance(history):
    async with server() as client:
        snap,stream=await fixture(client,['48'],days=13)
        await wellness_policy(client,stream,ordinary_basis='SUBJECTIVE_AND_WELLNESS')
        result=await interpret(client,snap,policy_version='fixture-readiness-v1')
        assert result['state']=='UNKNOWN'
        assert result['wellness_evidence'][0]['days'][-1]['reason']=='BASELINE_UNKNOWN'
        await enter(client,wellness(**STREAM,source_id='fourteenth',local_date='2026-10-11',value='48'))
        newer=await capture(client,config_version='synthetic-v1')
        result=await interpret(client,newer,policy_version='fixture-readiness-v1')
        assert result['state']=='ORDINARY'
        assert result['wellness_evidence'][0]['days'][-1]['g5_descriptor']['observed_days']==14
        for target in ('2027-03-08','2026-10-05'):
            # An explicitly targeted ordinary report is not enough under this
            # policy if current wellness evidence does not reach its target.
            await enter(client,preweek(source_id='target-'+target,week=target))
            targeted=await capture(client,config_version='synthetic-v1')
            out=await interpret(client,targeted,week=target,policy_version='fixture-readiness-v1')
            assert out['state']=='UNKNOWN'
            assert out['wellness_evidence'][0]['reasons']==['OUTSIDE_TARGET_FRESHNESS_WINDOW']
            assert out['wellness_evidence'][0]['frozen_g5_stream']['current']=='48'
        # Matching numeric values in a different unit are not silently combined.
        await wellness_policy(client,{**stream,'unit':'beats/min'},version='different-unit',ordinary_basis='SUBJECTIVE_AND_WELLNESS')
        out=await interpret(client,newer,policy_version='different-unit')
        assert out['state']=='UNKNOWN'
        assert out['wellness_evidence'][0]['reasons']==['COMPATIBLE_STREAM_MISSING']
        later=await capture(client,as_of='2026-11-02T18:00:00Z',config_version='synthetic-v1')
        out=await interpret(client,later,week='2026-11-02',policy_version='fixture-readiness-v1')
        assert out['state']=='UNKNOWN'
        assert out['wellness_evidence'][0]['reasons']==['CURRENT_MISSING_OR_STALE']


async def test_date_only_current_day_remains_uncertain_and_future_observation_not_used(history):
    async with server() as client:
        await enter(client,wellness())  # Received after the historical partial day at T.
        await enter(client,wellness(source_id='future',local_date='2039-01-01',observed_at='2039-01-01T12:00:00Z'))
        snap=await capture(client)
        result=await interpret(client,snap)
        assert result['state']=='UNKNOWN'
        assert result['uncertain_context']
        assert not result['unconfigured_wellness_streams']
        assert 'future' not in str(result['wellness_evidence'])
