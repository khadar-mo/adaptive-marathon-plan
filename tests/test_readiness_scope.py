"""Targeted reports, completed-run chronology and frozen evidence applicability."""
from datetime import datetime

import pytest
from garmin_fit_sdk import convert_datetime_to_timestamp

from test_context import enter
from test_fit_import import resolve, upload, zipped
from test_fitness import steady
from test_imports import history, server, submit
from test_readiness import T, WEEK, capture, config, interpret, policy, preweek, scale

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def run(client, day):
    stamp = convert_datetime_to_timestamp(datetime.fromisoformat(day + 'T09:00:00+00:00'))
    imported = await upload(client, (day + '.fit', steady(0, start=stamp)))
    assert imported['state'] == 'complete', imported
    return imported['activities'][0]['id']


def postrun(activity, day='2026-10-25', **kw):
    return {'kind':'post_run', 'source':'self-report', 'source_id':f'run-{activity}',
            'activity_id':activity, 'reported_at':day+'T12:00:00Z',
            'ratings':{'fatigue':{'value':'8','scale_version':'fixture-fatigue-v1'}}, **kw}


def run_policy(**kw):
    rule = {**config()['subjective_rules'][0], 'id':'run-fatigue', 'kind':'post_run'}
    return config(**{'subjective_rules':[rule], 'post_run_lookback_days':7, **kw})


async def test_consecutive_fatigue_missing_run_breaks_and_pain_priority(history):
    async with server() as client:
        await scale(client); await policy(client, run_policy())
        a = await run(client, '2026-10-23'); b = await run(client, '2026-10-24')
        await enter(client, postrun(a,'2026-10-23'))
        one = await capture(client)
        assert (await interpret(client,one,policy_version='fixture-readiness-v1'))['state']=='MIXED'
        first = (await enter(client,postrun(b,'2026-10-24')))['records'][0]
        pair = await capture(client)
        repeated = await interpret(client,pair,policy_version='fixture-readiness-v1')
        assert repeated['state']=='PERSISTENT_CONCERN'
        assert repeated['persistence'][0]['count']==2
        assert len(set(repeated['persistence'][0]['version_ids']))==2
        by_run = await client.get('/api/snapshots/'+pair['id']+'/readiness',params={'activity_id':b,'policy_version':'fixture-readiness-v1'})
        assert by_run.json()['state']=='PERSISTENT_CONCERN'
        await enter(client,postrun(b,'2026-10-24',attention=['distinct_pain'],supersedes=first['version_id'],correction_reason='Reported distinct pain'))
        pain = await capture(client)
        assert (await interpret(client,pain,policy_version='fixture-readiness-v1'))['state']=='PAIN_OR_ILLNESS_REVIEW'
        assert (await interpret(client,pair,policy_version='fixture-readiness-v1'))==repeated
        # A third completed run with no check-in breaks the consecutive proof.
        latest = (await client.get('/api/context/records/'+first['record_id'])).json()['versions'][-1]
        await enter(client,postrun(b,'2026-10-24',supersedes=latest['version_id'],correction_reason='Resolved the distinct symptom; fatigue remains'))
        await run(client, '2026-10-25')
        gap = await capture(client)
        gap_result = await interpret(client,gap,policy_version='fixture-readiness-v1')
        assert gap_result['state']=='MIXED' and not gap_result['persistence']


async def test_current_postrun_illness_does_not_classify_distant_or_historical_week(history):
    async with server() as client:
        await scale(client); await policy(client,run_policy())
        activity = await run(client,'2026-11-25')
        await enter(client,postrun(activity,'2026-11-25',attention=['illness']))
        snap = await capture(client,as_of='2026-11-26T00:00:00Z')
        current = await interpret(client,snap,week='2026-11-23',policy_version='fixture-readiness-v1')
        assert current['state']=='PAIN_OR_ILLNESS_REVIEW'
        for week in ('2027-03-08','2026-10-19'):
            result = await interpret(client,snap,week=week,policy_version='fixture-readiness-v1')
            assert result['state']=='UNKNOWN'
            assert result['runner_reports'][0]['reasons']==['OUTSIDE_COMPLETED_RUN_SCOPE']
        # A runner can explicitly address a future week; target date is not report time.
        await enter(client,preweek(week='2027-03-08',reported_at='2026-11-25T15:00:00Z',attention=['illness']))
        explicit = await capture(client,as_of='2026-11-26T00:00:00Z')
        assert (await interpret(client,explicit,week='2027-03-08',policy_version='fixture-readiness-v1'))['state']=='PAIN_OR_ILLNESS_REVIEW'


@pytest.mark.parametrize(('changes','reason'),[
    ({'ratings':{},'notes':'Easy week planned; no fatigue number supplied'},'MISSING_REQUIRED_RATING:preweek-fatigue'),
    ({'ratings':{},'missing_reason':'Runner did not submit a fatigue rating'},'EXPLICIT_MISSING_REPORT'),
    ({'ratings':{'fatigue':{'value':'5','scale_version':'fixture-fatigue-v1'}}},'OUTSIDE_DEFINED_RANGES:preweek-fatigue'),
    ({'ratings':{'fatigue':{'value':'1','scale_version':'rpe-0-10-v1'}}},'UNCONFIGURED_OR_INCOMPATIBLE_RATING:fatigue'),
    ({'reported_at':'2026-10-01T12:00:00Z'},'STALE_REPORT'),
    ({'pain_discomfort':'Persistent new tightness, exact runner text'},'UNCLASSIFIED_SYMPTOM_TEXT'),
])
async def test_missing_unknown_scale_stale_or_unclassified_is_not_ordinary(history, changes, reason):
    async with server() as client:
        await scale(client); await policy(client)
        await enter(client,preweek(**changes))
        result = await interpret(client,await capture(client),policy_version='fixture-readiness-v1')
        assert result['state']=='UNKNOWN'
        assert reason in result['runner_reports'][0]['reasons']


async def test_future_report_and_late_past_report_follow_T_and_K(history):
    async with server() as client:
        await scale(client); await policy(client)
        future = (await enter(client,preweek(reported_at='2026-10-26T12:00:00Z',attention=['illness'])))['records'][0]
        old = await capture(client)
        assert (await interpret(client,old,policy_version='fixture-readiness-v1'))['state']=='UNKNOWN'
        await enter(client,preweek(supersedes=future['version_id'],correction_reason='Correct the report time; recorded later'))
        fixed = await capture(client)
        assert (await interpret(client,fixed,policy_version='fixture-readiness-v1'))['state']=='ORDINARY'
        prior_k = await capture(client,knowledge_cutoff=old['knowledge_cutoff'])
        assert prior_k['id']==old['id']
        assert (await interpret(client,prior_k,policy_version='fixture-readiness-v1'))['state']=='UNKNOWN'


async def test_frozen_identity_donor_reference_is_not_reassigned_by_live_state(history):
    from runs import ConnectionProblem
    from test_canonical import trace_fit
    from fit_fixture import START
    history.original_error=ConnectionProblem('Synthetic unavailable original.')
    async with server() as client:
        await scale(client); await policy(client,run_policy(report_max_age_days=366,post_run_lookback_days=366))
        await submit(client)
        await enter(client,postrun(1,attention=['illness']))
        before = await capture(client)
        # The Garmin summary can be timed even before a raw FIT is available.
        first = await client.get('/api/snapshots/'+before['id']+'/readiness',params={'activity_id':1,'policy_version':'fixture-readiness-v1'})
        assert first.status_code==200,first.text
        raw=trace_fit(start=START+300)
        await upload(client,('manual.fit',raw))
        history.original_error=None; history.original=zipped(raw)
        pending=await submit(client)
        assert (await resolve(client,pending,{'action':'link','activity_id':2})).status_code==200
        after = await capture(client)
        for identity in (1,2):
            changed=await client.get('/api/snapshots/'+after['id']+'/readiness',params={'activity_id':identity,'policy_version':'fixture-readiness-v1'})
            assert changed.status_code==200,changed.text
            assert changed.json()['state']=='PAIN_OR_ILLNESS_REVIEW'
            assert changed.json()['runner_reports'][0]['record']['activity_id']==1
            assert changed.json()['runner_reports'][0]['resolved_activity_id']==2
        assert (await client.get('/api/snapshots/'+before['id']+'/readiness',params={'activity_id':1,'policy_version':'fixture-readiness-v1'})).json()==first.json()
