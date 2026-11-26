"""G4 independent arithmetic, public source selection and immutable capture."""
from copy import deepcopy
from datetime import timedelta
import json
import math
import threading

import pytest

import app
import canonical
import effort_math
import efforts
import estimator_math as maths
import estimators
from demo_efforts import dense, recording
from demo_estimators import CUTOFF, examples
from demo_recordings import START
from test_fit_import import upload
from test_imports import history, server, digest


@pytest.fixture
def anyio_backend():
    return "asyncio"


POLICY = estimators.DEFAULT_CONFIG['review_policy']


def observation(distance, time, activity=1, provenance=None):
    return {'id': str((distance, time, activity)), 'ref': {'activity_id': activity},
            'candidate': {'distance_m': distance, 'elapsed_seconds': time,
                          'provenance': provenance or {'category': 'EASY_OR_UNKNOWN', 'verified_maximal': False}}}


def test_T06_centered_ols_exact_parameters_residuals_and_units():
    rows = [observation(d, (d - 100) / 3, i) for i, d in enumerate((400, 800, 5000), 1)]
    fit = maths.critical_speed(rows, POLICY)
    primary = fit['primary']
    assert primary['cs_mps'] == pytest.approx(3, abs=1e-12)
    assert primary['d_prime_m'] == pytest.approx(100, abs=1e-10)
    assert primary['r_squared'] == pytest.approx(1, abs=1e-14)
    assert all(abs(r['residual_m']) < 1e-10 for r in primary['residuals'])
    assert primary['units']['cs'] == 'm/s' and primary['units']['d_prime'] == 'm'
    assert fit['subsets'][0]['original_p02_group']
    assert fit['leave_one_distance_out']['state'] == 'NOT_AVAILABLE'
    assert fit['leave_one_distance_out']['fits'] == []


def test_T07_T08_two_distances_and_perfect_easy_inputs_do_not_validate_physiology():
    rows = [observation(d, d / 3) for d in (400, 800, 1000)]
    assert maths.ols(rows[:2], POLICY)['reason'] == 'NEED_THREE_DISTINCT_SUPPORTED_DISTANCES'
    fit = maths.ols(rows, POLICY)
    assert fit['r_squared'] == pytest.approx(1)
    assert fit['fit_state'] == 'TRAINING_DERIVED_UNVERIFIED'
    assert fit['personal_predictive_validity'] == 'UNTESTED'
    assert 'ALL_INPUTS_SHARE_ONE_ACTIVITY' in fit['flags']
    assert fit['source_overlap']['distinct_activities'] == 1
    assert not fit['prescription_authority']


@pytest.mark.parametrize(('rows', 'reason'), [
    ([observation(d, 100) for d in (400, 800, 1000)], 'SINGULAR_ELAPSED_TIMES'),
    ([observation(d, 100) for d in (400, 400, 1000)], 'REPEATED_DISTANCE_SELECT_ONE_OBSERVATION_PER_DISTANCE'),
    ([observation(d, 100) for d in (400, 800, 1609.344)], 'NEED_THREE_DISTINCT_SUPPORTED_DISTANCES'),
    ([observation(d, t) for d, t in ((400, 0), (800, 300), (1000, 400))], 'NONPOSITIVE_OR_NONFINITE_INPUT'),
    ([observation(d, t) for d, t in ((400, math.inf), (800, 300), (1000, 400))], 'NONPOSITIVE_OR_NONFINITE_INPUT'),
    ([observation(d, t) for d, t in ((400, 1e308), (800, 1), (1000, 2))], 'NONCOMPUTABLE_NUMERICAL_FIT'),
])
def test_undefined_fits_are_inspectable_without_nan_or_invented_points(rows, reason):
    fit = maths.ols(rows, POLICY)
    assert fit['status'] == 'INSUFFICIENT_DATA' and fit['reason'] == reason
    assert fit['cs_mps'] is None and fit['d_prime_m'] is None and fit['r_squared'] is None
    json.dumps(fit, allow_nan=False)


def test_signed_intercept_nonpositive_slope_and_versioned_review_policies():
    negative = maths.ols([observation(d, (d + 100) / 3) for d in (400, 800, 1000)], POLICY)
    assert negative['d_prime_m'] == pytest.approx(-100)
    assert 'NEGATIVE_D_PRIME_PRESERVED' in negative['flags']
    assert negative['policy_checks']['d_prime_min_m']['state'] == 'UNKNOWN'
    reviewed = maths.ols([observation(d, (d + 100) / 3) for d in (400, 800, 1000)], {**POLICY, 'd_prime_min_m': 0})
    assert reviewed['status'] == 'CONFLICTED_EVIDENCE' and reviewed['d_prime_m'] == negative['d_prime_m']
    backwards = maths.ols([observation(d, (2000 - d) / 3) for d in (400, 800, 1000)], POLICY)
    assert backwards['cs_mps'] == pytest.approx(-3) and 'NONPOSITIVE_CS' in backwards['flags']
    zero = maths.ols([observation(d, t) for d, t in ((400, 100), (800, 400), (1500, 150))], POLICY)
    assert zero['cs_mps'] == 0 and 'NONPOSITIVE_CS' in zero['flags']


def test_four_distances_loo_all_subsets_and_instability_not_confidence():
    rows = [observation(d, (d - 100) / 3 + (90 if d == 5000 else 0), i)
            for i, d in enumerate((400, 800, 1500, 5000), 1)]
    fitted = maths.critical_speed(rows, {**POLICY, 'max_loo_relative_cs_change': .001})
    assert len(fitted['subsets']) == 5 and len(fitted['leave_one_distance_out']['fits']) == 4
    assert all(len(r['fit']['distances_m']) == 3 for r in fitted['leave_one_distance_out']['fits'])
    assert fitted['sensitivity_policy_checks']['max_loo_relative_cs_change']['state'] == 'REVIEW'
    assert fitted['primary']['status'] == 'CONFLICTED_EVIDENCE'
    assert 'NOT A 95%' in fitted['subset_distribution']['label']
    assert fitted['subset_distribution']['range_mps'] > 0
    assert any(s['original_p02_group'] for s in fitted['subsets'])


def test_centering_handles_large_time_origin_and_riegel_matches_independent_formula():
    rows = [observation(d, 1e12 + d / 4) for d in (400, 800, 1000)]
    fit = maths.ols(rows, POLICY)
    assert fit['cs_mps'] == 4 and fit['d_prime_m'] == -4e12
    source = observation(5000, 1500)
    result = maths.riegel(source, 42195, 1.06, POLICY)
    assert result['elapsed_seconds'] == pytest.approx(1500 * math.exp(1.06 * math.log(42195 / 5000)), rel=1e-14)
    assert result['comparison_state'] == 'EXPLORATORY' and result['far_extrapolation_review']['state'] == 'UNKNOWN'
    assert maths.riegel(source, 1e308, 1e308, POLICY)['reason'] == 'NONCOMPUTABLE_POWER_LAW'


def test_labels_reviews_and_unsupported_legacy_attestations_remain_separate():
    p = {'category': 'VERIFIED_TEST', 'verified_maximal': True, 'protocol': 'Track test', 'declaration_basis': 'EXPLICIT_USER_ATTESTATION'}
    assert not maths.qualification(p)['comparison_eligible']
    q = {'schema_version': 'effort-qualification-v1', 'maximality': 'CONFIRMED', 'rest_state': 'RESTED',
         'control_state': 'CONTROLLED', 'test_evidence_status': 'SUPPORTED_ATTESTATION'}
    assert maths.qualification({**p, 'qualification': q})['credible_maximal_trial']
    assert not maths.qualification({**p, 'qualification': {**q, 'rest_state': 'UNKNOWN'}})['credible_maximal_trial']
    race = {'schema_version': 'effort-qualification-v1', 'maximality': 'UNKNOWN', 'race_distance_verification': 'DOCUMENTED',
            'race_time_verification': 'ATTESTED', 'race_evidence_status': 'SUPPORTED_ATTESTATION'}
    assert maths.qualification({'category': 'RACE', 'qualification': race})['comparison_eligible']
    assert not maths.qualification({'category': 'RACE', 'qualification': race})['credible_maximal_trial']


async def import_trials(client):
    for name, raw in examples().items():
        result = await upload(client, (name, raw))
        assert result['state'] == 'complete'


async def three_config(client, version='three-trials'):
    response = await client.post('/api/estimators/configurations', json={'version': version,
        'config': {**estimators.DEFAULT_CONFIG, 'cs_distances_m': [400, 800, 5000]}})
    assert response.status_code == 200, response.text
    return version


def window(result, days=112):
    return next(w for w in result['critical_speed']['windows'] if w['days'] == days)


@pytest.mark.anyio
async def test_public_T06_T08_exact_synthetic_capture_profile_precision_and_read_only_ui(history, tmp_path):
    async with server() as client:
        await import_trials(client)
        config = await three_config(client)
        before = digest((tmp_path / 'runs.sqlite3').read_bytes())
        preview = await client.get('/api/estimators', params={'cutoff': CUTOFF, 'config_version': config})
        assert preview.status_code == 200, preview.text
        p = preview.json()['preview']
        assert window(p)['primary']['cs_mps'] == pytest.approx(3, abs=1e-12)
        assert window(p)['primary']['d_prime_m'] == pytest.approx(100, abs=1e-9)
        assert window(p)['primary']['fit_state'] == 'TRAINING_DERIVED_UNVERIFIED'
        assert len(window(p)['primary']['residuals']) == 3
        assert window(p)['primary']['source_overlap']['distinct_activities'] == 3
        assert {w['days'] for w in p['critical_speed']['windows']} == {56, 84, 112, 168}
        assert len(p['riegel']['comparisons']) > 4  # retain every source, no fastest marathon selection
        eight = next(o for o in p['evidence']['observations'] if o['candidate']['distance_m'] == 800 and o['ref']['activity_id'] == 2)
        assert eight['candidate']['end']['fraction'] == pytest.approx(2 / 3)  # dense 5-second final bracket
        assert eight['candidate']['elapsed_seconds'] == pytest.approx(700 / 3)
        assert eight['source_version_id'] in p['evidence']['source_versions']
        assert eight['canonical_version_id'] in p['evidence']['canonical_versions']
        assert eight['candidate']['start']['sample_indices']
        for url in ('/estimators?cutoff=' + CUTOFF.replace('+', '%2B'), '/efforts'):
            response = await client.get(url)
            assert response.status_code == 200, response.text
        assert digest((tmp_path / 'runs.sqlite3').read_bytes()) == before
        saved = await client.post('/api/estimators', json={'cutoff': CUTOFF, 'config_version': config})
        assert saved.status_code == 200, saved.text
        result = saved.json()['evaluation']
        assert result['input_hash'] == p['input_hash'] and result['knowledge_time_frozen'] is False
        assert result['knowledge_cutoff_utc'] is None
        assert result['created_at'] != result['event_cutoff_utc']
        again = (await client.post('/api/estimators', json={'cutoff': CUTOFF, 'config_version': config})).json()['evaluation']
        assert again == result
        assert (await client.get(saved.json()['url'])).json() == result
        assert 'Copied input ledger' in (await client.get(saved.json()['html_url'])).text
        assert not history.calls


@pytest.mark.anyio
async def test_saved_non_pb_sources_separate_slower_riegel_and_no_repeated_cs_distance(history):
    async with server() as client:
        await import_trials(client)
        source = (await client.get('/api/activities/3/efforts')).json()
        inspected = await client.post('/api/activities/3/efforts/inspect', json={'version': source['version_id'],
            'source': source['selected_fit_sha256'], 'start_distance_m': 100.5, 'distance_m': 400})
        assert inspected.status_code == 200
        candidate = inspected.json()['candidate']
        ref = {'activity_id': 3, 'version_id': source['version_id'], 'source_sha256': source['selected_fit_sha256'], 'candidate_id': candidate['id']}
        catalog = (await client.get('/api/estimators', params={'cutoff': CUTOFF})).json()['catalog']
        assert any(o['ref'] == ref for o in catalog)
        faster = next(o['ref'] for o in catalog if o['distance_m'] == 400 and o['ref']['activity_id'] == 1 and not o['unavailable_reasons'])
        response = await client.post('/api/estimators', json={'cutoff': CUTOFF, 'riegel_sources': [faster, ref], 'cs_sources': [ref]})
        assert response.status_code == 200, response.text
        r = response.json()['evaluation']
        predictions = [c for c in r['riegel']['comparisons'] if c['target_distance_m'] == 42195]
        assert len(predictions) == 2 and predictions[1]['elapsed_seconds'] > predictions[0]['elapsed_seconds']
        assert window(r)['primary']['status'] == 'INSUFFICIENT_DATA'
        assert any(o['candidate']['id'] == candidate['id'] for o in r['evidence']['observations'])
        repeated = await client.post('/api/estimators', json={'cutoff': CUTOFF, 'cs_sources': [faster, ref]})
        assert repeated.status_code == 422 and 'one CS source' in repeated.text


@pytest.mark.anyio
async def test_history_keeps_source_geometry_rejected_context_and_config_versions(history, monkeypatch):
    async with server() as client:
        await import_trials(client)
        saved = (await client.post('/api/estimators', json={'cutoff': CUTOFF})).json()
        original = saved['evaluation']
        o = original['evidence']['observations'][0]
        ref = o['ref']
        path = f'/api/activities/{ref["activity_id"]}/efforts/{ref["version_id"]}/{ref["candidate_id"]}'
        context = await client.post(path + '/context', json={'category': 'RACE', 'actor': 'Synthetic runner', 'note': 'Later label'})
        assert context.status_code == 200
        review = await client.post(path + '/review', json={'action': 'reject', 'actor': 'Synthetic reviewer', 'note': 'Later rejection'})
        assert review.status_code == 200
        rebuild = await client.post('/api/activities/1/efforts/recompute', json={'config_version': 'new-g2', 'config': {**efforts.DEFAULT_CONFIG, 'recent_days': 84}})
        assert rebuild.status_code == 200
        later = (await client.post('/api/estimators', json={'cutoff': CUTOFF})).json()['evaluation']
        assert later['input_hash'] != original['input_hash']
        monkeypatch.setattr(efforts, 'load', lambda *args, **kwargs: pytest.fail('Saved evaluation must not resolve current G2 heads'))
        assert (await client.get(saved['url'])).json() == original
        assert (await client.get(saved['html_url'])).status_code == 200


@pytest.mark.anyio
async def test_as_of_missing_windows_exact_boundaries_and_no_future_source_input(history):
    async with server() as client:
        await import_trials(client)
        config = await three_config(client)
        original = (await client.post('/api/estimators', json={'cutoff': CUTOFF, 'config_version': config})).json()['evaluation']
        await upload(client, ('future-fast.fit', recording(dense([(0, 0), (1400, 5000)]), start=START + 60 * 86400)))
        after = (await client.post('/api/estimators', json={'cutoff': CUTOFF, 'config_version': config})).json()['evaluation']
        assert after['critical_speed'] == original['critical_speed'] and after['riegel'] == original['riegel']
        assert any(r['reason'] == 'FUTURE_ACTIVITY' for r in after['evidence']['g2_selection']['excluded'])
        assert all(o['ref']['activity_id'] != 4 for o in after['evidence']['observations'])
        end = max(effort_math.stamp(o['candidate']['end_utc']) for o in original['evidence']['observations'])
        cutoff = (end + timedelta(days=112)).isoformat()
        exact = (await client.post('/api/estimators', json={'cutoff': cutoff, 'config_version': config})).json()['evaluation']
        assert any(w['observation_id'] for r in exact['observed_profile'] for w in r['recent'] if w['days'] == 112)
        old = (await client.post('/api/estimators', json={'cutoff': (end + timedelta(days=400)).isoformat(), 'config_version': config})).json()['evaluation']
        assert all(w['state'] == 'MISSING_RECENT' for w in old['critical_speed']['windows'])
        assert old['riegel']['comparisons'] == []
        assert any(r['all_time'] for r in old['observed_profile'])


@pytest.mark.anyio
async def test_owned_refs_reject_wrong_source_and_future_explicit_inputs_stay_unavailable(history):
    async with server() as client:
        await import_trials(client)
        preview = (await client.get('/api/estimators', params={'cutoff': CUTOFF})).json()
        ref = preview['catalog'][0]['ref']
        for bad in ({**ref, 'activity_id': 9999}, {**ref, 'source_sha256': 'a' * 64}, {**ref, 'candidate_id': 'b' * 64}):
            assert (await client.post('/api/estimators', json={'cutoff': CUTOFF, 'riegel_sources': [bad]})).status_code == 404
        before = effort_math.stamp(preview['catalog'][0]['end_utc']) - timedelta(days=1)
        response = await client.post('/api/estimators', json={'cutoff': before.isoformat(), 'riegel_sources': [ref], 'cs_sources': [ref]})
        assert response.status_code == 200, response.text
        r = response.json()['evaluation']
        assert r['riegel']['comparisons'] == []
        assert 'FUTURE_ACTIVITY_OR_SAMPLE' in r['riegel']['excluded'][0]['reasons']
        assert all(w['primary']['cs_mps'] is None for w in r['critical_speed']['windows'])


@pytest.mark.anyio
async def test_boundary_winner_without_durable_g2_id_is_fully_copied(history):
    from fit_data import absolute_time
    async with server() as client:
        await upload(client, ('boundary.fit', recording(dense([(0, 0), (120, 400), (300, 800)]))))
        cutoff = (absolute_time(START + 240) + timedelta(days=112)).isoformat()
        g2 = (await client.get('/api/efforts', params={'cutoff': cutoff})).json()
        four = next(r for r in g2['distances'] if r['distance_m'] == 400)
        winner = next(r for r in four['recent'] if r['days'] == 112)['best']
        assert winner['inspection_required']
        path = f'/api/activities/1/efforts/{winner["version_id"]}/{winner["id"]}'
        assert (await client.get(path)).status_code == 404
        result = (await client.post('/api/estimators', json={'cutoff': cutoff})).json()['evaluation']
        selected = next(o for o in result['evidence']['observations'] if o['candidate']['id'] == winner['id'])
        assert selected['candidate']['start'] == winner['start']
        assert selected['candidate']['end'] == winner['end']
        assert selected['candidate']['audit'] == winner['audit']
        source = result['evidence']['source_versions'][selected['source_version_id']]
        assert source['canonical_source']['trace']['samples']['rows']
        assert source['source']['fit_sha256'] == winner['source_sha256']
        assert result['evidence']['g2_selection']['selection_id'] == g2['selection_id']


@pytest.mark.anyio
async def test_qualification_context_extension_is_copied_conservatively_with_known_at(history):
    async with server() as client:
        await import_trials(client)
        preview = (await client.get('/api/estimators', params={'cutoff': CUTOFF})).json()
        ref = next(o['ref'] for o in preview['catalog'] if o['distance_m'] == 400 and o['ref']['activity_id'] == 1)
        # Exercise the agreed additive #30 storage contract without importing an
        # unmerged worker module or mutating any existing context event.
        known = canonical.knowledge_time()
        qualification = {'schema_version': 'effort-qualification-v1', 'context_id': 'synthetic-q', 'known_at': known,
            'actor': 'Synthetic runner', 'explanation': 'Controlled synthetic fixture', 'evidence_refs': [],
            'maximality': 'CONFIRMED', 'rest_state': 'RESTED', 'control_state': 'CONTROLLED',
            'race_distance_verification': 'UNKNOWN', 'race_time_verification': 'UNKNOWN',
            'test_evidence_status': 'SUPPORTED_ATTESTATION', 'race_evidence_status': 'UNVERIFIED'}
        payload = {'category': 'VERIFIED_TEST', 'tags': [], 'actor': 'Synthetic runner', 'note': 'Synthetic test',
                   'protocol': 'Rested track protocol', 'maximal_confirmed': True, 'qualification': qualification}
        with app.app.state.store.connect(write=True) as db:
            db.execute('INSERT INTO effort_contexts VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('synthetic-q', ref['version_id'], ref['candidate_id'], ref['source_sha256'], canonical.encoded(payload), known, None))
        response = await client.post('/api/estimators', json={'cutoff': CUTOFF, 'riegel_sources': [ref]})
        assert response.status_code == 200, response.text
        result = response.json()['evaluation']
        comparisons = result['riegel']['comparisons']
        assert all(r['comparison_state'] == 'PREDICTION_COMPARISON' for r in comparisons)
        o = next(o for o in result['evidence']['observations'] if o['id'] == comparisons[0]['source_observation_id'])
        assert o['candidate']['provenance']['qualification'] == qualification
        assert o['candidate']['context_history'][-1]['known_at'] == known
        assert result['knowledge_time_frozen'] is False and result['validation']['personal_predictive'] == 'UNTESTED'


@pytest.mark.anyio
async def test_configuration_immutable_validation_and_empty_install_read_only(history, tmp_path):
    async with server() as client:
        assert (await client.get('/estimators')).status_code == 200
        assert not (tmp_path / 'runs.sqlite3').exists()
        assert (await client.post('/api/estimators', json={'cutoff': '2026-01-01'})).status_code == 422
        for config in ({'riegel_k': True}, {'riegel_k': 0}, {'target_distances_m': [True]}, {'cs_distances_m': [1609.344]},
                       {'recent_days': 113}, {'review_policy': {'d_prime_min_m': 200, 'd_prime_max_m': 100}}):
            assert (await client.post('/api/estimators/configurations', json={'version': 'bad', 'config': config})).status_code == 422
        await three_config(client)
        replacement = await client.post('/api/estimators/configurations', json={'version': 'three-trials', 'config': estimators.DEFAULT_CONFIG})
        assert replacement.status_code == 422
        assert (await client.get('/api/estimators/missing')).status_code == 404
        assert (await client.get('/api/estimators?config_version=missing')).status_code == 404
        empty = (await client.post('/api/estimators', json={'cutoff': CUTOFF})).json()['evaluation']
        assert empty['observed_profile'] == [] and window(empty)['primary']['reason'] == 'NEED_THREE_DISTINCT_SUPPORTED_DISTANCES'


@pytest.mark.anyio
async def test_capture_copies_context_in_one_transaction_and_later_change_is_a_new_evaluation(history, monkeypatch):
    async with server() as client:
        await import_trials(client)
        preview = (await client.get('/api/estimators', params={'cutoff': CUTOFF})).json()['preview']
        ref = preview['evidence']['observations'][0]['ref']
        original_bests = efforts.bests
        entered, finished = threading.Event(), threading.Event()
        failures = []
        def correction():
            entered.set()
            try:
                efforts.context(app.app.state.store, ref['activity_id'], ref['version_id'], ref['candidate_id'],
                    category='RACE', tags=[], actor='Synthetic athlete', note='Concurrent context')
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=correction)
        def concurrent_bests(*args, **kwargs):
            report = original_bests(*args, **kwargs)
            thread.start()
            assert entered.wait(1)
            assert not finished.wait(.05), 'Context writer must not enter during the capture transaction'
            return report
        monkeypatch.setattr(efforts, 'bests', concurrent_bests)
        response = await client.post('/api/estimators', json={'cutoff': CUTOFF})
        thread.join(timeout=5)
        assert response.status_code == 200, response.text
        assert finished.is_set() and failures == []
        old = response.json()['evaluation']
        assert old['input_hash'] == preview['input_hash']
        monkeypatch.setattr(efforts, 'bests', original_bests)
        new = (await client.post('/api/estimators', json={'cutoff': CUTOFF})).json()['evaluation']
        assert new['input_hash'] != old['input_hash']
        assert (await client.get(response.json()['url'])).json() == old
