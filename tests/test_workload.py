"""Independent synthetic G3 sums, clocks, census, provenance and replay contracts."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

import canonical
import fit_store
from storage import Archive, Store
import workload
import workload_inputs as wi


@pytest.fixture
def store(tmp_path, monkeypatch):
    value = Store(tmp_path / "runs.sqlite3", Archive(tmp_path / "archive"))
    with value.connect(write=True, create=True):
        pass
    monkeypatch.setattr(canonical, "knowledge_time", lambda: "2026-11-01T12:00:00.000000+00:00")
    monkeypatch.setattr(wi, "now", lambda: "2026-11-26T12:00:00.000000+00:00")
    return value


def run(store, aid, end, distance, elapsed=3600, *, start=None, timer=3500, moving=None, sport="running"):
    if start is None and end is not None:
        start = (wi.stamp(end) - timedelta(seconds=elapsed if elapsed is not None else 3600)).isoformat()
    local = wi.stamp(start).astimezone(workload.ZONE) if start else None
    with store.connect(write=True, create=True) as db:
        db.execute("""INSERT INTO activities(id,name,sport,start_utc,start_local,local_date,distance_m,
                    elapsed_seconds,timer_seconds,moving_seconds,metric_sources) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (aid, f"Synthetic run {aid}", sport, start, local.isoformat() if local else None,
                    local.date().isoformat() if local else None, distance, elapsed, timer, moving, '{}'))
        return canonical.persist(db, store.archive, aid)


def coverage(**kw):
    return {"kind": "coverage", "source": "synthetic-manifest", "source_id": "end-census", "actor": "fixture athlete",
            "reason": "Explicit synthetic running census", "source_refs": [{"manifest": "synthetic-all-runs-v1"}],
            "basis": "END", "lower_utc": "2026-06-01T00:00:00Z", "upper_utc": "2026-11-26T00:00:00Z",
            "lower_inclusive": True, "upper_inclusive": False, "observed_through": "2026-11-26T00:00:00Z",
            "reported_at": "2026-11-26T01:00:00Z", "status": "COMPLETE", "evidence_kind": "IMPORT_MANIFEST", **kw}


def enter(store, value):
    return wi.ingest(store, wi.encoded(value).encode())["records"][0]


def inspect(store, **kw):
    return workload.read(store, as_of="2026-11-01T12:00:00Z", knowledge_cutoff="2026-11-26T12:00:00Z", **kw)


def test_t10_t11_t12_exact_sums_boundaries_and_l30(store):
    run(store, 1, "2026-10-25T12:00:00Z", 1000)  # exactly excluded lower endpoint
    run(store, 2, "2026-10-25T12:00:00.000001Z", .1, elapsed=0, timer=0, moving=0)
    run(store, 3, "2026-11-01T12:00:00Z", .2, elapsed=0, timer=0, moving=0)
    run(store, 4, "2026-11-01T12:00:00.000001Z", 99000)
    run(store, 5, "2026-10-20T12:00:00Z", 16000)
    run(store, 6, "2026-10-20T14:00:00Z", 90000, sport="cycling")
    observed = inspect(store, proposed_start="2026-11-02T12:00:00Z", proposed_distance_m="18000")
    assert observed['rolling'][0]['activity_ids'] == [2, 3]
    assert observed['rolling'][0]['measures']['distance_m']['known_subtotal'] == '0.3'
    assert observed['rolling'][0]['measures']['distance_m']['value'] is None
    assert observed['l30']['status'] == 'INCOMPLETE_HISTORY' and observed['l30']['ratio'] is None
    enter(store, coverage())
    result = workload.read(store, as_of="2026-11-02T00:00:00Z", knowledge_cutoff="2026-11-26T12:00:00Z",
                           proposed_start="2026-11-02T12:00:00Z", proposed_distance_m="18000")
    # The 99 km run is now completed and must be included; no arbitrary plausible-distance cap.
    assert result['l30']['denominator_m'] == '99000.0'
    result = inspect(store, proposed_start="2026-11-01T15:00:00Z", proposed_distance_m="18000")
    assert result['l30']['denominator_m'] == '16000.0'
    assert result['l30']['ratio'] == '1.125' and Decimal(result['l30']['percentage_change']) == Decimal('12.5')
    assert result['rolling'][0]['measures']['distance_m']['value'] == '0.3'
    assert result['rolling'][1]['measures']['distance_m']['known_subtotal'] == '17000.3'
    assert result['rolling'][2]['run_count'] == 4
    assert {e['reason'] for e in result['excluded']} == {'NONRUNNING', 'NOT_COMPLETED_BY_CUTOFF'}
    assert result['l30']['coverage']['upper_inclusive'] is False
    assert result['l30']['coverage']['lower_inclusive'] is True


def test_t12_empty_complete_and_zero_denominator_are_not_zero_risk(store):
    assert inspect(store, proposed_start="2026-11-01T14:00:00Z", proposed_distance_m='0')['l30']['status'] == 'INCOMPLETE_HISTORY'
    enter(store, coverage())
    for aid in (None, 1):
        if aid:
            run(store, aid, '2026-10-28T13:00:00Z', 0, elapsed=0, timer=0)
        result = inspect(store, proposed_start="2026-11-01T14:00:00Z", proposed_distance_m='18000')['l30']
        assert result['status'] == 'NOT_CALCULABLE' and result['ratio'] is None and result['percentage_change'] is None
    future = inspect(store, proposed_start="2026-11-03T14:00:00Z", proposed_distance_m='18000')['l30']
    assert future['status'] == 'INCOMPLETE_HISTORY' and future['future_history_unobserved']


def test_l30_calendar_lower_inclusive_upper_exclusive_across_dst(store):
    for aid, end, distance in [(1, '2026-10-01T22:59:59Z', 99000), (2, '2026-10-01T23:00:00Z', 16000),
                               (3, '2026-10-31T23:59:59Z', 15000), (4, '2026-11-01T00:00:00Z', 99000)]:
        run(store, aid, end, distance)
    enter(store, coverage())
    result = inspect(store, proposed_start='2026-11-01T23:00:00Z', proposed_distance_m='18000')['l30']
    assert result['activity_ids'] == [2, 3]
    assert result['coverage']['lower_utc'] == '2026-10-01T23:00:00.000000+00:00'
    assert result['coverage']['upper_utc'] == '2026-11-01T00:00:00.000000+00:00'
    assert Decimal(result['ratio']) == Decimal('1.125')


def test_end_start_week_dst_open_week_and_measurement_missingness(store):
    run(store, 1, '2026-10-26T00:30:00Z', 5000.25, start='2026-10-25T23:30:00Z')
    run(store, 2, '2026-10-25T00:50:00Z', 1000, elapsed=600, timer=None)
    run(store, 3, '2026-10-25T01:50:00Z', None, elapsed=600, timer=500)
    enter(store, coverage())
    enter(store, coverage(source_id='start-census', basis='START', lower_utc='2026-10-18T23:00:00Z',
                          upper_utc='2026-11-01T12:00:00Z', upper_inclusive=True))
    result = inspect(store)
    prior = next(w for w in result['weeks'] if w['iso_week'] == '2026-W43')
    this = result['weeks'][-1]
    assert prior['full_week_bounds']['lower_utc'] == '2026-10-18T23:00:00.000000+00:00'
    assert prior['full_week_bounds']['upper_utc'] == '2026-10-26T00:00:00.000000+00:00'
    assert prior['fm_end']['activity_ids'] == [2, 3] and prior['planner_start']['activity_ids'] == [2, 3, 1]
    assert this['week_state'] == 'OPEN' and this['planner_start']['coverage']['complete']
    assert this['planner_start']['coverage']['upper_utc'] == '2026-11-01T12:00:00.000000+00:00'
    assert this['planner_start']['coverage']['upper_inclusive'] is True
    assert prior['fm_end']['coverage']['complete']
    assert prior['fm_end']['measures']['distance_m']['missing_count'] == 1
    assert prior['fm_end']['measures']['distance_m']['value'] is None
    assert prior['fm_end']['measures']['distance_m']['known_subtotal'] == '1000.0'
    assert result['spacing'][0]['seconds'] == '3000.0'  # UTC, not repeated local clocks


def test_gap_overlap_and_unknown_timing_keep_subtotals(store):
    run(store, 1, '2026-10-30T11:00:00Z', 5000)
    run(store, 2, '2026-10-30T11:30:00Z', 3000)
    run(store, 3, None, 1234.567, elapsed=None)
    enter(store, coverage())
    result = inspect(store)
    assert result['spacing'][0]['seconds'] == '-1800.0' and result['spacing'][0]['overlap']
    assert result['unassigned'][0]['activity_id'] == 3
    assert result['unassigned_known_subtotals']['distance_m'] == '1234.567'
    assert result['rolling'][0]['unassigned_timing_count'] == 1
    assert result['rolling'][0]['measures']['distance_m']['value'] is None


def test_coverage_endpoint_union_hole_conflict_and_partial_day(store):
    enter(store, coverage(source_id='left', lower_utc='2026-10-25T12:00:00Z', upper_utc='2026-10-29T12:00:00Z', upper_inclusive=False))
    right = enter(store, coverage(source_id='right', lower_utc='2026-10-29T12:00:00Z', lower_inclusive=False,
                                  upper_utc='2026-11-01T12:00:00Z', upper_inclusive=True))
    result = inspect(store)['rolling'][0]['coverage']
    assert not result['complete'] and result['gaps'] == [{
        'lower_utc':'2026-10-29T12:00:00.000000+00:00', 'upper_utc':'2026-10-29T12:00:00.000000+00:00',
        'lower_inclusive':True, 'upper_inclusive':True}]
    enter(store, coverage(source_id='right', lower_utc='2026-10-29T12:00:00Z', lower_inclusive=True,
                          upper_utc='2026-11-01T12:00:00Z', upper_inclusive=True, supersedes=right['version_id'], correction_reason='Endpoint included'))
    assert inspect(store)['rolling'][0]['coverage']['complete']
    enter(store, coverage(source_id='conflicting-census', status='INCOMPLETE', exclusions=['Unresolved missing activity']))
    assert not inspect(store)['rolling'][0]['coverage']['complete']
    with pytest.raises(ValueError, match='Complete coverage'):
        enter(store, coverage(source_id='invalid-future-day', observed_through='2026-11-25T12:00:00Z'))


def test_t14_t15_history_corrections_identity_and_frozen_replay(store, monkeypatch):
    version = run(store, 1, '2026-10-30T10:00:00Z', 16000)
    run(store, 2, '2026-10-30T10:00:00Z', 16000)
    enter(store, coverage())
    args = dict(as_of='2026-11-01T12:00:00Z', knowledge_cutoff='2026-11-01T12:00:00Z', mode='AS_KNOWN')
    old = workload.capture(store, **args)
    assert old['result']['rolling'][0]['run_count'] == 2  # Identity not yet linked at K.
    monkeypatch.setattr(canonical, 'knowledge_time', lambda:'2026-11-26T13:00:00.000000+00:00')
    monkeypatch.setattr(wi, 'now', lambda:'2026-11-27T14:00:00.000000+00:00')
    with store.connect(write=True, create=True) as db:
        fit_store.merge_activity(db, 2, 1)
        db.execute("UPDATE activities SET distance_m=17000,start_utc='2026-11-02T12:00:00+00:00' WHERE id=1")
        corrected = canonical.persist(db, store.archive, 1)
    assert corrected != version
    assert workload.read(store, **args) == old['result']
    current = workload.read(store, as_of='2026-11-03T12:00:00Z')
    assert current['rolling'][0]['run_count'] == 1 and current['rolling'][0]['measures']['distance_m']['known_subtotal'] == '17000.0'
    assert any(e['reason']=='MERGED_CANONICAL_COPY' for e in current['excluded'])
    assert workload.replay(Store(store.path, store.archive), old['snapshot_id'])['status'] == 'IDENTICAL'
    assert workload.snapshot(store, old['snapshot_id']) == old
    # Current mutable redirects and heads are not historical evidence.
    with store.connect(write=True, create=True) as db:
        db.execute('DELETE FROM canonical_activity_heads')
        db.execute('DELETE FROM activity_redirects')
    assert workload.read(store, **args) == old['result']


def test_late_attestation_correction_is_not_backdated_and_raw_preserved(store, monkeypatch):
    first = enter(store, coverage())
    args = dict(as_of='2026-11-01T12:00:00Z', knowledge_cutoff=first['known_at'])
    old = workload.capture(store, **args)
    monkeypatch.setattr(wi, 'now', lambda:'2026-11-27T12:00:00Z')
    second = enter(store, coverage(status='INCOMPLETE', exclusions=['Missing export'], supersedes=first['version_id'], correction_reason='Census correction'))
    assert second['revision']==2 and second['previous_version_id']==first['version_id']
    assert workload.read(store, **args) == old['result']
    assert not workload.read(store, as_of=args['as_of'])['rolling'][0]['coverage']['complete']
    assert len(wi.history(store, first['record_id']))==2
    assert store.archive.read(first['raw_sha256']) and store.archive.read(second['raw_sha256'])
    assert wi.ingest(store, wi.encoded(coverage()).encode())['replayed']
    with pytest.raises(wi.Conflict):
        enter(store, coverage(reason='Changed without an explicit correction'))


def profile(**kw):
    return {'kind':'zone_profile','source':'synthetic-lab','source_id':'profile','actor':'fixture athlete',
            'reason':'Supplied test calibration','source_refs':[{'protocol':'synthetic-calibration-v1'}],
            'method':'Synthetic supplied three bands','system':'RESEARCH_THREE','metric':'heart_rate','unit':'bpm',
            'effective_from':'2026-01-01T00:00:00Z','reliability':'CALIBRATED','calibration':'Synthetic explicit method, not physiological verification',
            'bands':[{'label':'low','upper':140},{'label':'middle','lower':140,'upper':170},{'label':'high','lower':170}], **kw}


def ledger(version, p, **kw):
    return {'kind':'band_ledger','source':'synthetic-device','source_id':'bands','actor':'fixture athlete',
            'reason':'Measured synthetic timer bands','source_refs':[{'file':'synthetic-bands.json'}],
            'canonical_version_id':version,'profile_version_id':p['version_id'],'duration_basis':'timer_seconds',
            'seconds':{'low':'2400.125','middle':'900.25','high':'99.5'},'unclassified_seconds':'100.125',
            'measured_at':'2026-10-30T12:00:00Z','measurement_method':'Source supplied elapsed seconds accumulated while timer running', **kw}


def test_positive_calibrated_native_uncalibrated_and_uncovered_intensity(store):
    version = run(store, 1, '2026-10-30T10:00:00Z', 10000, timer=3500)
    run(store, 2, '2026-10-31T10:00:00Z', 5000, timer=1000)
    p = enter(store, profile())
    enter(store, ledger(version,p))
    result = inspect(store)['rolling'][0]['intensity']
    group, = result['groups']
    assert group['seconds'] == {'low':'2400.125','middle':'900.25','high':'99.5'}
    assert group['unknown_seconds']=='1100.125' and group['uncovered_activity_ids']==[2]
    assert group['profile']['unit']=='bpm' and group['profile']['reliability']=='CALIBRATED'
    other = enter(store, profile(source_id='uncalibrated', reliability='UNCALIBRATED'))
    enter(store, ledger(version,other,source_id='uncalibrated-bands'))
    native = enter(store, profile(source_id='native',system='DEVICE_NATIVE_FIVE',reliability='DEVICE_REPORTED',
                                 bands=[{'label':str(i)} for i in range(1,6)]))
    enter(store,ledger(version,native,source_id='native-bands',seconds={str(i):'600' for i in range(1,6)},unclassified_seconds='500'))
    groups=inspect(store)['rolling'][0]['intensity']['groups']
    assert len(groups)==3
    uncalibrated=next(g for g in groups if g['profile']['reliability']=='UNCALIBRATED')
    assert set(uncalibrated['seconds'].values())=={'0'} and uncalibrated['unknown_seconds']=='4500.0'
    assert uncalibrated['activities'][0]['status']=='UNCALIBRATED_METHOD'
    assert next(g for g in groups if g['profile']['system']=='DEVICE_NATIVE_FIVE')['seconds']['1']=='600'


@pytest.mark.parametrize('change, reason', [({'seconds':{'low':'5000','middle':'0','high':'0'}},'BAND_SECONDS_EXCEED_DURATION'),
                                          ({'source_id':'other-source'},'CONFLICTING_LEDGERS')])
def test_intensity_rejects_invalid_measurement_and_conflicting_ledger(store,change,reason):
    version=run(store,1,'2026-10-30T10:00:00Z',10000)
    p=enter(store,profile())
    if reason=='CONFLICTING_LEDGERS': enter(store,ledger(version,p))
    enter(store,ledger(version,p,**change))
    group,=inspect(store)['rolling'][0]['intensity']['groups']
    assert group['activities'][0]['status']==reason
    assert set(group['seconds'].values())=={'0'} and group['unknown_seconds']=='3500.0'


def test_legacy_no_selection_is_unknown_read_only_and_cli(store,tmp_path):
    run(store,1,'2026-10-30T10:00:00Z',10000)
    with store.connect(write=True,create=True) as db:
        db.execute('DELETE FROM canonical_selections')
    before=store.path.read_bytes()
    observed=inspect(store)
    assert observed['current_diagnostics']['unprocessed_running_ids']==[1] and not observed['activities']
    strict=workload.read(store,as_of='2026-11-01T12:00:00Z',mode='AS_KNOWN')
    assert not strict['activities'] and 'current_diagnostics' not in strict
    assert store.path.read_bytes()==before
    script=Path(__file__).resolve().parents[1]/'workload.py'
    result=subprocess.run([sys.executable,str(script),'--db',str(store.path),'--as-of','2026-11-01T12:00:00Z'],capture_output=True,text=True,check=True,cwd=tmp_path)
    assert json.loads(result.stdout)['current_diagnostics']['unprocessed_running_ids']==[1]
    frozen=workload.capture(store,as_of='2026-11-01T12:00:00Z',knowledge_cutoff='2026-11-26T12:00:00Z')
    assert 'current_diagnostics' not in frozen['inputs'] and 'current_diagnostics' not in frozen['result']


def test_reselection_uses_selection_clock_not_creation_and_replay_after_restart(store,monkeypatch):
    old=run(store,1,'2026-10-30T10:00:00Z',10000)
    monkeypatch.setattr(canonical,'knowledge_time',lambda:'2026-11-02T12:00:00Z')
    with store.connect(write=True,create=True) as db:
        db.execute('UPDATE activities SET distance_m=12000 WHERE id=1')
        new=canonical.persist(db,store.archive,1)
        db.execute("INSERT INTO canonical_selections(activity_id,version_id,selected_at) VALUES (1,?,'2026-11-03T12:00:00Z')",(old,))
    for cutoff,version in [('2026-11-02T13:00:00Z',new),('2026-11-03T13:00:00Z',old)]:
        data=workload.capture(store,as_of=cutoff,mode='AS_KNOWN')
        assert data['result']['activities'][0]['canonical_version_id']==version
        assert workload.replay(Store(store.path,store.archive),data['snapshot_id'])['status']=='IDENTICAL'


def test_provider_mode_and_unavailable_terminal_result_are_frozen_at_knowledge_time(store):
    run(store,1,'2026-10-30T10:00:00Z',10000)
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO provider_config_revisions VALUES (1,'STRAVA_ONLY','2026-11-01T00:00:00Z','provider-modes-v1')")
        db.execute("INSERT INTO provider_attempts(id,config_revision,mode,contract_version,started_at,finished_at,result) VALUES (1,1,'STRAVA_ONLY','provider-modes-v1','2026-11-01T12:00:00Z','2026-11-02T12:00:00Z',?)",
                   (json.dumps({'state':'POLICY_BLOCKED','policy_checked_at':'2026-11-01','activity_ids':[]}),))
    args=dict(as_of='2026-11-01T13:00:00Z',mode='AS_KNOWN')
    old=workload.capture(store,**args)
    pending=old['inputs']['provider_lineage']['attempts'][0]
    assert pending['result'] is None and pending['finished_at'] is None
    assert old['result']['rolling'][0]['run_count']==1  # mode choice does not erase retained actuals
    with store.connect(write=True,create=True) as db:
        db.execute("INSERT INTO provider_config_revisions VALUES (2,'GARMIN_ONLY','2026-11-03T00:00:00Z','provider-modes-v1')")
    assert workload.read(store,**args)==old['result']
    later=workload.read(store,as_of='2026-11-04T00:00:00Z',mode='AS_KNOWN')['provider_lineage']
    assert later['selected_config']['mode']=='GARMIN_ONLY'
    assert later['attempts'][0]['result']['state']=='POLICY_BLOCKED'


def test_t14_new_future_activity_does_not_change_strict_historical_hash_or_values(store,monkeypatch):
    run(store,1,'2026-10-30T10:00:00Z',10000)
    args=dict(as_of='2026-11-01T13:00:00Z',mode='AS_KNOWN')
    old=workload.capture(store,**args)
    monkeypatch.setattr(canonical,'knowledge_time',lambda:'2026-11-02T12:00:00Z')
    run(store,2,'2026-11-02T10:00:00Z',12000)
    assert workload.read(store,**args)==old['result']
    assert workload.capture(store,**args)['snapshot_id']==old['snapshot_id']
    assert workload.replay(store,old['snapshot_id'])['status']=='IDENTICAL'


def test_batch_correction_failure_is_atomic_and_no_intensity_ledger_keeps_unknown_time(store):
    run(store,1,'2026-10-30T10:00:00Z',10000)
    result=inspect(store)['rolling'][0]['intensity']
    assert result['status']=='UNKNOWN'
    assert result['uncovered_time']['elapsed_seconds']=={'known_unknown_seconds':'3600.0','missing_duration_count':0}
    assert result['uncovered_time']['moving_seconds']=={'known_unknown_seconds':'0','missing_duration_count':1}
    row=enter(store,coverage())
    with pytest.raises(wi.Conflict):
        wi.ingest(store,wi.encoded([coverage(source_id='must-rollback'),coverage(reason='Illegal implicit replacement')]).encode())
    with store.history() as db:
        assert [r['record_id'] for r in wi.selected(db,'2026-11-26T12:00:00Z')]==[row['record_id']]
