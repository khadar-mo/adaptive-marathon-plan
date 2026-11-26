"""Public classification/provenance contracts with synthetic recorded sources."""
from copy import deepcopy
from pathlib import Path
import asyncio
import gzip
import hashlib
import json
import os
import sqlite3

import pytest

import app
import canonical
import efforts
import workout_labels as labels
from demo_efforts import dense, examples
from test_canonical import trace_fit
from fit_fixture import START
from test_efforts import distance
from test_fit_import import upload, resolve, zipped
from test_imports import history, server, submit
from runs import ConnectionProblem
from storage import Archive, Store


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def structured_fit(*, linked=True, name="THRESHOLD_INTERVALS", points=None, start=START):
    extra = [(27, {"message_index": 0, "wkt_step_name": name, "intensity": 0})]
    if linked:
        extra.append((19, {"message_index": 0, "wkt_step_index": 0, "total_elapsed_time": 300, "total_distance": 1000}))
    return trace_fit(points or dense([(0, 0), (300, 1000)]), extra=extra, start=start)


def choice(row, layer="corrected", primary="EASY_AEROBIC", **extra):
    old = row["layers"][layer]
    return {"layer": layer, "canonical_version_id": row["canonical_version_id_now"], "primary": primary,
            "actor": "Synthetic athlete", "explanation": "Recorded intent or correction, not physiology.",
            "source": "athlete", "source_reference": "Synthetic training note", "supersedes": old["id"] if old else None, **extra}


async def setup(client, raw=None):
    await upload(client, ("synthetic.fit", raw or structured_fit()))
    response = await client.get('/api/activities/1/workout')
    assert response.status_code == 200, response.text
    return response.json()


async def test_independent_three_layers_linked_raw_structure_and_immutable_corrections(history, tmp_path):
    raw = structured_fit()
    async with server() as client:
        row = await setup(client, raw)
        before = (await client.get('/api/activities/1/efforts')).json()
        planned = (await client.post('/api/activities/1/workout', json=choice(row, 'planned', 'LONG_RUN', tags=['BENCHMARK_TAG']))).json()
        assert planned['workout']['effective'] == 'UNKNOWN'
        inferred = await client.post('/api/activities/1/workout/infer', json={'canonical_version_id': row['canonical_version_id_now']})
        assert inferred.status_code == 200, inferred.text
        row = inferred.json()['workout']
        assert row['effective'] == 'THRESHOLD_INTERVALS'
        match = row['layers']['inferred']['payload']['matches'][0]
        assert match['lap']['index'] == match['step']['index'] == 0
        assert match['step']['raw_sha256'] == hashlib.sha256(raw).hexdigest()
        corrected = (await client.post('/api/activities/1/workout', json=choice(row, primary='RECOVERY_EASY'))).json()['workout']
        assert corrected['layers']['planned']['payload']['primary'] == 'LONG_RUN'
        assert corrected['layers']['inferred']['payload']['primary'] == 'THRESHOLD_INTERVALS'
        assert corrected['effective'] == 'RECOVERY_EASY' and len(corrected['history']) == 3
        again = (await client.post('/api/activities/1/workout/infer', json={'canonical_version_id': row['canonical_version_id_now']})).json()['workout']
        assert len(again['history']) == 3  # same source/config inference is idempotent
        second = (await client.post('/api/activities/1/workout', json=choice(corrected, primary='EASY_AEROBIC'))).json()['workout']
        assert second['layers']['corrected']['previous_id'] == corrected['layers']['corrected']['id']
        after = (await client.get('/api/activities/1/efforts')).json()
        assert before['version_id'] == after['version_id']
        first, last = distance(before, 400)['best'], distance(after, 400)['best']
        assert first['id'] == last['id'] and first['elapsed_seconds'] == last['elapsed_seconds']
        assert last['provenance']['session_classification']['effective'] == 'EASY_AEROBIC'
        assert last['provenance']['qualification']['maximality'] == 'UNKNOWN'
        assert app.app.state.archive.read(hashlib.sha256(raw).hexdigest()) == raw
        assert len((await client.get('/api/workouts/changes')).json()['events']) == 4


async def test_known_at_and_event_cutoffs_do_not_leak_later_corrections(history, monkeypatch):
    async with server() as client:
        row = await setup(client)
        monkeypatch.setattr(canonical, 'knowledge_time', lambda: '2027-01-01T00:00:00+00:00')
        first = (await client.post('/api/activities/1/workout', json=choice(row))).json()['workout']
        monkeypatch.setattr(canonical, 'knowledge_time', lambda: '2027-01-02T00:00:00+00:00')
        await client.post('/api/activities/1/workout', json=choice(first, primary='LONG_RUN'))
        past = (await client.get('/api/activities/1/workout?as_of=2027-01-01T12:00:00Z')).json()
        assert past['effective'] == 'EASY_AEROBIC' and len(past['history']) == 1
        assert past['withheld'][0]['reason'] == 'LATER_KNOWLEDGE'
        current = (await client.get('/api/activities/1/workout')).json()
        await client.post('/api/activities/1/workout', json=choice(current, primary='TEMPO_THRESHOLD', event_utc='2028-01-01T00:00:00Z'))
        event_limited = (await client.get('/api/activities/1/workout?as_of=2027-02-01T00:00:00Z&knowledge_as_of=2029-01-01T00:00:00Z')).json()
        assert event_limited['effective'] == 'LONG_RUN'
        assert event_limited['withheld'][0]['reason'] == 'FUTURE_EVENT'


@pytest.mark.parametrize('linked,name', [(False, 'THRESHOLD_INTERVALS'), (True, '5K race world record'), (True, '<script>alert(1)</script>')])
async def test_unsupported_titles_or_unexecuted_steps_do_not_infer_category(history, linked, name):
    async with server() as client:
        row = await setup(client, structured_fit(linked=linked, name=name))
        value = (await client.post('/api/activities/1/workout/infer', json={'canonical_version_id': row['canonical_version_id_now']})).json()['workout']
        assert value['effective'] == 'UNKNOWN'
        page = await client.get('/activities/1/workout')
        assert '<script>alert(1)</script>' not in page.text
        assert name in json.dumps(value)


async def test_config_mapping_is_editable_versioned_and_applies_only_to_completed_structure(history):
    async with server() as client:
        row = await setup(client, structured_fit(name='My steady block'))
        config = {'completed_step_names': {'My steady block': 'TEMPO_THRESHOLD'}}
        assert (await client.post('/api/workouts/configs/my-map', json=config)).status_code == 200
        changed = (await client.post('/api/activities/1/workout/infer', json={'canonical_version_id': row['canonical_version_id_now'], 'config_version': 'my-map'})).json()['workout']
        assert changed['effective'] == 'TEMPO_THRESHOLD'
        assert changed['layers']['inferred']['payload']['config'] == config
        assert (await client.post('/api/workouts/configs/my-map', json={'completed_step_names': {}})).status_code == 409


async def test_non_pb_controlled_test_and_race_verification_remain_separate(history, monkeypatch):
    async with server() as client:
        await setup(client)
        extraction = (await client.get('/api/activities/1/efforts')).json()
        selected = await client.post('/api/activities/1/efforts/inspect', json={'version': extraction['version_id'], 'source': extraction['selected_fit_sha256'], 'start_distance_m': 100.5, 'distance_m': 400})
        c = selected.json()['candidate']
        target = f'/api/activities/1/efforts/{extraction["version_id"]}/{c["id"]}/context'
        request = {'category': 'VERIFIED_TEST', 'tags': [], 'actor': 'Synthetic athlete', 'note': 'Controlled benchmark', 'protocol': 'Recorded 400m protocol, measured elapsed, explicit effort.', 'maximal_confirmed': True,
                   'qualification': {'maximality': 'CONFIRMED', 'control_state': 'CONTROLLED', 'evidence_refs': [{'kind': 'athlete_attestation', 'reference': 'Synthetic protocol record', 'method': 'Athlete confirmed the controlled attempt.'}]}, 'supersedes': ''}
        monkeypatch.setattr(canonical, 'knowledge_time', lambda: '2027-01-01T00:00:00+00:00')
        response = await client.post(target, json=request)
        assert response.status_code == 200, response.text
        first = response.json()['candidate']
        q = first['provenance']['qualification']
        assert q['test_evidence_status'] == 'SUPPORTED_ATTESTATION' and q['rest_state'] == 'UNKNOWN'
        assert q['context_id'] == first['acknowledged_context_id']
        assert first['computed_provenance']['category'] == 'EASY_OR_UNKNOWN'
        assert first['provenance']['preceding_work']['cumulative_start_distance_m'] == '100.5'
        monkeypatch.setattr(canonical, 'knowledge_time', lambda: '2027-01-02T00:00:00+00:00')
        race = {**request, 'category': 'RACE', 'maximal_confirmed': False, 'supersedes': q['context_id'],
                'qualification': {'race_distance_verification': 'DOCUMENTED', 'race_time_verification': 'ATTESTED', 'evidence_refs': [{'kind':'document','reference':'Synthetic race-distance certificate','method':'Recorded document, not independently validated by the app.'}]}}
        second = (await client.post(target, json=race)).json()['candidate']
        assert second['provenance']['qualification']['race_evidence_status'] == 'SUPPORTED_ATTESTATION'
        assert second['provenance']['qualification']['maximality'] == 'UNKNOWN'
        assert second['provenance']['qualification']['race_distance_verification'] == 'DOCUMENTED'
        with app.app.state.store.history() as db:
            frozen = labels.enrich_candidate(db, c, extraction['version_id'], 1, knowledge_as_of='2027-01-01T12:00:00Z')
        assert frozen['provenance']['category'] == 'VERIFIED_TEST'
        assert frozen['context_input_version'] == first['context_input_version']
        public_frozen = (await client.get(target.removesuffix('/context'),params={'knowledge_as_of':'2027-01-01T12:00:00Z'})).json()['candidate']
        assert public_frozen['provenance']['qualification']['context_id'] == first['acknowledged_context_id']
        assert second['context_input_version'] != first['context_input_version']
        assert (await client.post(target, json=race)).status_code == 409


@pytest.mark.parametrize('declared,maximal,expected', [('RACE', False, 'UNKNOWN'), ('VERIFIED_TEST', False, 'UNKNOWN'), ('VERIFIED_TEST', True, 'CONFIRMED')])
async def test_legacy_declarations_never_invent_test_conditions(history, declared, maximal, expected):
    async with server() as client:
        await setup(client)
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row, 400)['best']
        response = await client.post(f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}/context', json={'category': declared, 'actor': 'A', 'note': 'Legacy declaration', 'tags': [], 'maximal_confirmed': maximal, 'protocol': 'Protocol if applicable'})
        q = response.json()['candidate']['provenance']['qualification']
        assert q['maximality'] == expected
        assert q['rest_state'] == q['control_state'] == 'UNKNOWN'
        assert q['test_evidence_status'] == q['race_evidence_status'] == 'UNVERIFIED'


async def test_recorded_fatigue_and_source_evidence_preserve_qc_and_reject_forged_refs(history):
    async with server() as client:
        await setup(client)
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row, 400)['best']
        target = f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}/context'
        q = {'rest_state': 'FATIGUED', 'maximality': 'NOT_MAXIMAL', 'evidence_refs': [{'kind': 'source', 'raw_sha256': row['selected_fit_sha256'], 'message_group': 'record_mesgs', 'index': 0, 'method': 'Recorded preceding work, subjective fatigue separately declared.'}]}
        request = {'category': 'TRAINING_FAST', 'tags': ['FATIGUED_BLOCK'], 'actor': 'A', 'note': 'Accumulated work felt tiring', 'qualification': q}
        response = await client.post(target, json=request)
        assert response.status_code == 200, response.text
        result = response.json()['candidate']
        assert result['provenance']['category'] == 'TRAINING_FAST'
        assert result['provenance']['qualification']['maximality'] == 'NOT_MAXIMAL'
        assert result['computed_quality_state'] == c['computed_quality_state']
        for bad in ({'raw_sha256': 'f'*64}, {'index': 999999}, {'index': True}, {'method': ' '}):
            invalid = deepcopy(request)
            invalid['qualification']['evidence_refs'][0].update(bad)
            assert (await client.post(target, json=invalid)).status_code == 422


async def test_race_maximality_is_independent_and_documented_requires_document_evidence(history):
    async with server() as client:
        await setup(client)
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row,400)['best']
        target = f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}/context'
        request = {'category':'RACE','tags':[],'actor':'Synthetic runner','note':'Race effort was maximal; no rested-test protocol claimed.',
                   'maximal_confirmed':True,'qualification':{'maximality':'CONFIRMED','race_distance_verification':'ATTESTED','race_time_verification':'ATTESTED',
                   'evidence_refs':[{'kind':'athlete_attestation','method':'Runner checked the recorded race result.','reference':'Synthetic race result'}]}}
        response = await client.post(target,json=request)
        assert response.status_code == 200,response.text
        q = response.json()['candidate']['provenance']['qualification']
        assert q['maximality']=='CONFIRMED' and q['race_evidence_status']=='SUPPORTED_ATTESTATION'
        assert q['test_evidence_status']=='UNVERIFIED' and q['rest_state']=='UNKNOWN'
        request['qualification']['race_distance_verification']='DOCUMENTED'
        assert (await client.post(target,json=request)).status_code==422


async def test_invalid_labels_stale_revisions_owned_efforts_and_strength_modality(history):
    async with server() as client:
        row = await setup(client)
        for bad in ({'primary': 'BENCHMARK_TAG'}, {'primary': 'STRENGTH_CONDITIONING'}, {'tags': ['']}, {'actor': ' '}, {'canonical_version_id': 'e'*64}, {'supporting_efforts': [{'version_id': 'e'*64, 'candidate_id': 'f'*64}]}):
            response = await client.post('/api/activities/1/workout', json=choice(row, **bad))
            assert response.status_code in (404, 409, 422), response.text
        first = await client.post('/api/activities/1/workout', json=choice(row))
        assert first.status_code == 200
        assert (await client.post('/api/activities/1/workout', json=choice(row))).status_code == 409
        for raw in (b'{}', b'[]', b'{"layer":"planned","layer":"corrected"}', b'{"primary":NaN}'):
            assert (await client.post('/api/activities/1/workout', content=raw)).status_code == 422
        assert (await client.get('/api/activities/999/workout')).status_code == 404
        assert (await client.get('/api/activities/999999999999999999999999/workout')).status_code == 404


async def test_raw_json_import_restart_readonly_and_recompute_leave_extraction_identity(history, tmp_path):
    raw = structured_fit()
    async with server() as client:
        row = await setup(client, raw)
        extraction = (await client.get('/api/activities/1/efforts')).json()
        c = distance(extraction, 400)['best']
        target = f'/api/activities/1/efforts/{extraction["version_id"]}/{c["id"]}'
        await client.post(target+'/review', json={'action':'reject','actor':'Synthetic reviewer','note':'Do not publish this interval.'})
        document = json.dumps({'activity_id': 1, 'label': choice(row, explanation='Exact report text 47.12345678901234567890')}, indent=2).encode()
        imported = await client.post('/api/workouts/import', files={'document': ('label.json', document, 'application/json')})
        assert imported.status_code == 200, imported.text
        saved = imported.json()['saved']['payload']
        assert saved['import_sha256'] == hashlib.sha256(document).hexdigest()
        assert app.app.state.archive.read(saved['import_sha256']) == document
        await upload(client, ('duplicate.fit', raw))
        await client.post('/api/efforts/recompute', json={})
        reread = (await client.get(target)).json()['candidate']
        assert reread['effective_quality_state'] == 'INVALID' and reread['id'] == c['id']
        assert (await client.get('/api/activities/1/efforts')).json()['version_id'] == extraction['version_id']
        before = hashlib.sha256((tmp_path/'runs.sqlite3').read_bytes()).hexdigest()
        for path in ('/workouts', '/activities/1/workout', '/api/activities/1/workout', '/api/workouts/configs', '/api/workouts/changes', target):
            assert (await client.get(path)).status_code == 200
        assert hashlib.sha256((tmp_path/'runs.sqlite3').read_bytes()).hexdigest() == before
    async with server() as client:
        assert (await client.get('/api/activities/1/workout')).json()['effective'] == 'EASY_AEROBIC'
        assert (await client.get(target)).json()['candidate']['effective_quality_state'] == 'INVALID'


async def test_genuine_merged_g2_store_keeps_rejections_inspections_and_recent_boundary(history, tmp_path):
    fixture = json.loads(gzip.decompress((Path(__file__).parent/'fixtures'/'workout-g2-59faa54.json.gz').read_bytes()))
    assert fixture['base_commit'] == '59faa544096d9b6fc196e4d0aac2992ca84e9cce'
    assert efforts.CODE_SHA256 == fixture['code_sha256']
    path = tmp_path/'runs.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(fixture['sql'])
    os.chmod(path,0o600)
    archive = Archive(tmp_path/'archive')
    raw = bytes.fromhex(fixture['raw_hex'])
    assert archive.put(raw) == fixture['source_sha256']
    async with server() as client:
        store = app.app.state.store
        assert efforts.bests(store,fixture['cutoff'],1) == fixture['bests']
        before = await client.get('/api/activities/1/workout')
        assert before.json()['effective'] == 'UNKNOWN'
        await client.post('/api/activities/1/workout',json=choice(before.json()))
        inspected = f'/api/activities/1/efforts/{fixture["version_id"]}/{fixture["inspection_id"]}'
        current = (await client.get(inspected)).json()['candidate']
        assert current['context_history'][0]['category'] == 'TRAINING_FAST'
        assert current['provenance']['qualification']['maximality'] == 'UNKNOWN'
        await upload(client,('same-original.fit',raw))
        await client.post('/api/efforts/recompute',json={})
        after = (await client.get('/api/activities/1/efforts')).json()
        assert after['version_id'] == fixture['version_id']
        assert efforts.bests(store,fixture['cutoff'],1) == fixture['bests']
        rejected = (await client.get(f'/api/activities/1/efforts/{fixture["version_id"]}/{fixture["rejected_id"]}')).json()['candidate']
        assert rejected['effective_quality_state'] == 'INVALID'
        assert (await client.get(inspected)).json()['candidate']['id'] == fixture['inspection_id']


async def test_alias_merge_and_source_correction_preserve_original_label_identity(history):
    history.original_error = ConnectionProblem('Synthetic original unavailable')
    async with server() as client:
        await submit(client)
        old = (await client.get('/api/activities/1/workout')).json()
        first = (await client.post('/api/activities/1/workout',json=choice(old,'planned','LONG_RUN'))).json()['saved']
        raw = structured_fit(start=START+300)
        await upload(client,('manual.fit',raw))
        history.original_error = None
        history.original = zipped(raw)
        pending = await submit(client)
        assert (await resolve(client,pending,{'action':'link','activity_id':2})).status_code == 200
        merged = (await client.get('/api/activities/2/workout')).json()
        assert merged['layers']['planned']['id'] == first['id']
        assert merged['layers']['planned']['activity_id'] == 1
        assert merged['source_changed'] is True
        assert (await client.post('/api/activities/2/workout',json=choice(old))).status_code == 409
        inferred = (await client.post('/api/activities/2/workout/infer',json={'canonical_version_id':merged['canonical_version_id_now']})).json()['workout']
        assert inferred['layers']['inferred']['activity_id'] == 2
        assert inferred['layers']['planned']['id'] == first['id']


async def test_concurrent_effort_writes_acknowledge_their_own_immutable_context(history):
    async with server() as client:
        await setup(client)
        row = (await client.get('/api/activities/1/efforts')).json()
        c = distance(row,400)['best']
        target = f'/api/activities/1/efforts/{row["version_id"]}/{c["id"]}/context'
        requests = [{'category':'TRAINING_FAST','actor':f'Writer {i}','note':f'Exact declaration {i}','tags':[]} for i in range(6)]
        replies = await asyncio.gather(*(client.post(target,json=body) for body in requests))
        ids = []
        for body, response in zip(requests,replies):
            assert response.status_code == 200,response.text
            value = response.json()['candidate']
            ids.append(value['acknowledged_context_id'])
            assert value['context_history'][-1]['id'] == value['acknowledged_context_id']
            assert value['context_history'][-1]['note'] == body['note']
        assert len(set(ids)) == 6


@pytest.mark.parametrize('name',list(examples()))
async def test_workout_names_do_not_change_continuity_or_observed_best(history,name):
    async with server() as client:
        row = await setup(client,examples()[name])
        before = efforts.bests(app.app.state.store,'2027-01-01T00:00:00Z')
        await client.post('/api/activities/1/workout',json=choice(row,'planned','FASTER_INTERVALS',tags=['BENCHMARK_TAG']))
        await client.post('/api/activities/1/workout',json=choice(row,primary='LONG_RUN'))
        after = efforts.bests(app.app.state.store,'2027-01-01T00:00:00Z')
        assert before == after
