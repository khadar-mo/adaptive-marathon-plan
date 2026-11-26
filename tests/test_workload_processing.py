"""P1: a complete census never certifies a partial canonical backfill."""
from decimal import Decimal

import pytest

import canonical
import app
import fit_store
import storage
import workload
import workload_inputs as wi
from runs import ConnectionProblem
from test_imports import add_run, history, server, submit
from test_workload import coverage, enter, run, store
from test_fit_import import upload
from test_canonical import trace_fit
from garmin_fit_sdk import convert_datetime_to_timestamp


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('processed', [[], [2]])
async def test_real_imported_g0_shape_partial_and_empty_backfill_capture_recovery(history,monkeypatch,tmp_path,processed):
    clock=['2026-11-26T10:00:00.000000+00:00']
    monkeypatch.setattr(wi,'now',lambda:clock[0])
    monkeypatch.setattr(canonical,'knowledge_time',lambda:clock[0])
    monkeypatch.setattr(storage,'now',lambda:clock[0])
    history.listed=[]
    history.original_error=ConnectionProblem('Synthetic summary-only G0 archive')
    for gid,day,distance in [(101,'2026-11-23',10000),(102,'2026-11-24',5000)]:
        item=add_run(history,gid,day,utc=day)
        item.update(startTimeGMT=day+' 10:00:00',startTimeLocal=day+' 10:00:00',distance=distance,elapsedDuration=3600)
        history.extra_details[str(gid)]['summaryDTO'].update(distance=distance,elapsedDuration=3600)
    async with server() as client:
        # Exercise the real importer/raw archive. Suppressing only the later G1
        # hook produces an existing G0-format store without invented selections.
        with monkeypatch.context() as legacy:
            legacy.setattr(canonical,'persist',lambda *args,**kwargs:None)
            imported=await submit(client,{'from':'2026-11-23','to':'2026-11-24'})
        assert len(imported['activities'])==2
        clock[0]='2026-11-26T11:00:00.000000+00:00'
        for aid in processed:
            response=await client.post(f'/api/activities/{aid}/canonical/recompute',json={})
            assert response.status_code==200,response.text
        clock[0]='2026-11-26T12:00:00.000000+00:00'
        assert (await client.post('/api/workload/inputs',json=coverage())).status_code==200
        query={'as_of':'2026-11-25T12:00:00Z','proposed_start':'2026-11-25T14:00:00Z','proposed_distance_m':'12000'}
        before=(tmp_path/'runs.sqlite3').read_bytes()
        current=(await client.get('/api/workload',params=query)).json()
        assert (tmp_path/'runs.sqlite3').read_bytes()==before
        frozen=(await client.post('/api/workload/capture',json=query)).json()
        assert frozen['result']=={k:v for k,v in current.items() if k!='current_diagnostics'}
        report=frozen['result']; rolling=report['rolling'][0]
        assert rolling['coverage']['complete']  # census and processing are independent
        assert Decimal(rolling['measures']['distance_m']['known_subtotal'])==(5000 if processed else 0)
        assert rolling['measures']['distance_m']['value'] is None
        assert rolling['frequency']['known_count']==len(processed) and rolling['frequency']['value'] is None
        assert rolling['processing']['status']=='INCOMPLETE_PROCESSING'
        assert {g['activity_id'] for g in rolling['processing']['gaps']}==({1} if processed else {1,2})
        assert report['l30']['status']=='INCOMPLETE_HISTORY' and report['l30']['ratio'] is None
        assert frozen['inputs']['processing_inventory']['known_at']==clock[0]
        assert frozen['inputs']['processing_gaps'][0]['timing']['end_utc'].endswith('+00:00')
        html=(await client.get('/workload',params=query)).text
        assert 'Recompute canonical activity 1' in html
        snapshot_url='/api/workload/snapshots/'+frozen['snapshot_id']
        clock[0]='2026-11-26T13:00:00.000000+00:00'
        assert (await client.post('/api/canonical/recompute',json={})).status_code==200
        recovered=(await client.get('/api/workload',params=query)).json()
        assert Decimal(recovered['rolling'][0]['measures']['distance_m']['value'])==15000
        assert recovered['rolling'][0]['frequency']['value']==2
        assert Decimal(recovered['l30']['ratio'])==Decimal('1.2')
        assert not recovered['processing_gaps']
        assert (await client.get(snapshot_url)).json()['result']==report
        historical=(await client.get('/api/workload',params={**query,'knowledge_cutoff':frozen['result']['knowledge_cutoff']})).json()
        assert historical['input_hash']==report['input_hash'] and historical['l30']['ratio'] is None
        assert (await client.get(snapshot_url+'/replay')).json()['status']=='IDENTICAL'


def unprocess(store, aid):
    with store.connect(write=True,create=True) as db:
        db.execute('DELETE FROM canonical_selections WHERE activity_id=?',(aid,))
        db.execute('DELETE FROM canonical_activity_heads WHERE activity_id=?',(aid,))


def test_processing_uncertainty_is_scoped_to_supported_end_start_windows(store):
    run(store,1,'2026-10-01T10:00:00Z',10000); unprocess(store,1)
    run(store,2,'2026-11-24T10:00:00Z',5000)
    enter(store,coverage())
    value=workload.read(store,as_of='2026-11-25T12:00:00Z',proposed_start='2026-11-25T14:00:00Z',proposed_distance_m='12000')
    assert value['rolling'][0]['processing']['complete'] and value['rolling'][1]['processing']['complete']
    assert not value['rolling'][2]['processing']['complete']
    assert Decimal(value['l30']['ratio'])==Decimal('2.4')  # known Oct 1 gap is outside L30
    # A cross-week unprocessed run affects START and END weeks independently.
    run(store,3,'2026-11-23T00:30:00Z',4000,start='2026-11-22T23:30:00Z'); unprocess(store,3)
    value=workload.read(store,as_of='2026-11-25T12:00:00Z')
    previous,current=value['weeks'][-2:]
    assert previous['fm_end']['processing']['complete'] and not previous['planner_start']['processing']['complete']
    assert not current['fm_end']['processing']['complete'] and current['planner_start']['processing']['complete']


def test_unknown_instant_stays_explicit_and_cannot_prove_zero(store):
    run(store,1,None,10000,elapsed=None); unprocess(store,1)
    enter(store,coverage())
    value=workload.capture(store,as_of='2026-11-25T12:00:00Z')['result']
    gap,=value['rolling'][0]['processing']['gaps']
    assert gap['timing'] is None and gap['known_start_utc'] is None
    assert value['rolling'][0]['processing']['unknown_instant_count']==1
    assert value['rolling'][0]['frequency']['value'] is None
    assert value['rolling'][0]['measures']['distance_m']['value'] is None


def test_strict_known_view_never_reads_later_legacy_import_or_current_merge(store,monkeypatch):
    clock=['2026-11-25T12:00:00.000000+00:00']
    monkeypatch.setattr(wi,'now',lambda:clock[0])
    monkeypatch.setattr(canonical,'knowledge_time',lambda:clock[0])
    run(store,1,'2026-11-23T10:00:00Z',10000); unprocess(store,1)
    run(store,2,'2026-11-24T10:00:00Z',5000)
    enter(store,coverage(upper_utc='2026-11-25T12:00:00Z',upper_inclusive=True,
                         observed_through=clock[0],reported_at=clock[0]))
    query={'as_of':clock[0],'mode':'AS_KNOWN'}
    old=workload.capture(store,**query)
    assert old['result']['rolling'][0]['processing']['gaps'][0]['activity_id']==1
    clock[0]='2026-11-26T12:00:00.000000+00:00'
    run(store,3,'2026-11-24T15:00:00Z',3000); unprocess(store,3)
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO imports(id,started_at,finished_at,state,source) VALUES (1,?,?,'complete','fit')",(clock[0],clock[0]))
        db.execute("INSERT INTO import_activities VALUES (1,3,'inserted')")
        fit_store.merge_activity(db,1,2)
        canonical.persist(db,store.archive,2)
    assert workload.read(store,**query)==old['result']
    assert workload.replay(store,old['snapshot_id'])['status']=='IDENTICAL'
    later=workload.read(store,as_of='2026-11-25T12:00:00Z')
    assert {g['activity_id'] for g in later['processing_gaps']}=={3}
    assert not any(g.get('activity_id')==3 for g in old['inputs']['processing_gaps'])


def test_old_inventory_does_not_certify_a_later_import_boundary(store,monkeypatch):
    run(store,1,'2026-11-23T10:00:00Z',10000)
    enter(store,coverage())
    old=workload.capture(store,as_of='2026-11-25T12:00:00Z')
    monkeypatch.setattr(wi,'now',lambda:'2026-11-28T12:00:00Z')
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO imports(id,started_at,finished_at,state,source) VALUES (1,'2026-11-27T10:00:00Z','2026-11-27T11:00:00Z','complete','fit')")
    strict=workload.read(store,as_of='2026-11-27T12:00:00Z',mode='AS_KNOWN')
    assert strict['processing_gaps'][0]['reason']=='IMPORT_PROCESSING_NOT_OBSERVED'
    assert strict['processing_gaps'][0]['activity_id'] is None
    assert strict['rolling'][0]['measures']['distance_m']['value'] is None
    # A current read can observe completed processing without an extra user step.
    assert workload.read(store,as_of='2026-11-25T12:00:00Z')['processing_gaps']==[]
    assert workload.replay(store,old['snapshot_id'])['status']=='IDENTICAL'


@pytest.mark.anyio
async def test_same_second_fit_import_after_frozen_knowledge_does_not_change_history(history,monkeypatch):
    clock=['2026-11-26T12:00:00.050000+00:00']
    class ImportClock:
        @staticmethod
        def now(zone): return wi.stamp(clock[0]).astimezone(zone)
    # Exercise production storage.now formatting, not a replacement of now().
    monkeypatch.setattr(storage,'datetime',ImportClock)
    monkeypatch.setattr(wi,'now',lambda:clock[0])
    monkeypatch.setattr(canonical,'knowledge_time',lambda:clock[0])
    async with server() as client:
        earlier=trace_fit(start=convert_datetime_to_timestamp(wi.stamp('2026-11-23T10:00:00Z')))
        first=await upload(client,('earlier.fit',earlier))
        assert first['started_at']==first['finished_at']==clock[0]
        cutoff='2026-11-26T12:00:00.100000+00:00'; clock[0]=cutoff
        assert (await client.post('/api/workload/capture',json={'as_of':cutoff})).status_code==200
        query={'as_of':cutoff,'mode':'AS_KNOWN'}
        saved=(await client.post('/api/workload/capture',json=query)).json()
        before=saved['result']
        assert before['rolling'][0]['run_count']==1 and not before['processing_gaps']
        clock[0]='2026-11-26T12:00:00.900000+00:00'
        later=trace_fit(start=convert_datetime_to_timestamp(wi.stamp('2026-11-24T10:00:00Z')))
        second=await upload(client,('later.fit',later))
        assert second['started_at']==second['finished_at']==clock[0]
        after=(await client.get('/api/workload',params=query)).json()
        assert after==before and after['input_hash']==before['input_hash']
        clock[0]='2026-11-26T12:00:00.950000+00:00'
        later_known=(await client.get('/api/workload',params={'as_of':clock[0],'mode':'AS_KNOWN'})).json()
        assert later_known['rolling'][0]['run_count']==2
        gap,=later_known['processing_gaps']
        assert gap['known_at']=='2026-11-26T12:00:00.900000+00:00'
        assert gap['source_availability']['precision']=='MICROSECOND'
        # Terminal timestamp is immutable after the running-to-terminal transition.
        app.app.state.store.finish(second['id'],'complete')
        assert (await client.get('/api/imports/'+str(second['id']))).json()['finished_at']==second['finished_at']
        assert (await client.get('/api/workload/snapshots/'+saved['snapshot_id']+'/replay')).json()['status']=='IDENTICAL'


def test_unobserved_legacy_second_is_not_backdated_into_earlier_cutoff(store,monkeypatch):
    clock=['2026-11-26T12:00:00.100000+00:00']
    monkeypatch.setattr(wi,'now',lambda:clock[0])
    workload.capture(store,as_of=clock[0])
    query={'as_of':clock[0],'mode':'AS_KNOWN'}
    before=workload.read(store,**query)
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO imports(id,started_at,finished_at,state,source) VALUES (1,'2026-11-26T12:00:00+00:00','2026-11-26T12:00:00+00:00','complete','fit')")
    clock[0]='2026-11-26T12:00:01.100000+00:00'
    assert workload.read(store,**query)==before
    later=workload.read(store,as_of='2026-11-26T12:00:01.000000+00:00',mode='AS_KNOWN')
    gap,=later['processing_gaps']
    assert gap['known_at']=='2026-11-26T12:00:01.000000+00:00'
    assert gap['source_availability']['precision']=='SECOND_TRUNCATED'
    assert gap['source_availability']['availability_basis']=='TRUNCATED_INTERVAL_ELAPSED'
    assert gap['source_availability']['upper_exclusive']


@pytest.mark.parametrize('finished', [None,'2026-11-26T12:00:00+00:00'])
def test_inventory_retains_genuinely_earlier_known_legacy_state_within_same_second(store,monkeypatch,finished):
    clock=['2026-11-26T12:00:00.100000+00:00']
    monkeypatch.setattr(wi,'now',lambda:clock[0])
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO imports(id,started_at,finished_at,state,source) VALUES (1,'2026-11-26T12:00:00+00:00',?,'complete','fit')",(finished,))
    workload.capture(store,as_of=clock[0])
    query={'as_of':clock[0],'mode':'AS_KNOWN'}
    before=workload.read(store,**query)
    assert before['processing_inventory']['import_boundary'][0]['finished_at']==finished
    if finished is None:
        gap,=before['processing_gaps']
        assert gap['known_at']==clock[0]
        assert gap['source_availability']['availability_basis']=='IMMUTABLE_INVENTORY_OBSERVATION'
        assert gap['source_availability']['precision']=='SECOND_TRUNCATED'
    else:
        assert not before['processing_gaps']
    clock[0]='2026-11-26T12:00:00.900000+00:00'
    with store.connect(write=True,create=True) as db:
        db.execute("UPDATE imports SET finished_at='2026-11-26T12:00:00+00:00' WHERE id=1")
        db.execute("INSERT INTO imports(id,started_at,finished_at,state,source) VALUES (2,'2026-11-26T12:00:00+00:00','2026-11-26T12:00:00+00:00','complete','fit')")
    assert workload.read(store,**query)==before
