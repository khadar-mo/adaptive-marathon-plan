"""Real Chromium G4 import, settings, source choice, save, ledger and history."""
import time

import httpx
from playwright.sync_api import expect, sync_playwright

from demo_estimators import CUTOFF, examples
from test_fit_browser import chrome_executable
from test_health import serve_database


def load_trials(client):
    result = client.post('/api/imports/fit', files=[('files', (name, raw)) for name, raw in examples().items()])
    result.raise_for_status()
    for _ in range(100):
        report = client.get(result.headers['location']).json()
        if report['state'] == 'complete':
            return
        time.sleep(.05)
    raise AssertionError(report)


def test_estimator_calculation_config_and_immutable_history_desktop_mobile_keyboard(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path / 'synthetic.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            load_trials(client)
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('console', lambda msg: errors.append(msg.text) if msg.type in ('warning', 'error') else None)
            page.on('response', lambda res: errors.append(res.url) if res.status >= 400 else None)
            page.goto(base + '/efforts?cutoff=2026-01-15T00:00:00Z')
            page.get_by_role('link', name='Compare performance estimators', exact=True).press('Enter')
            expect(page.get_by_role('heading', level=1)).to_have_text('Performance estimators')
            page.get_by_text('Create a configuration version', exact=True).press('Enter')
            page.get_by_label('New version name', exact=True).fill('browser-three-trials')
            page.get_by_label('CS distances in metres, comma separated', exact=True).fill('400, 800, 5000')
            page.get_by_label('D′ lower review bound (m)', exact=True).fill('0')
            page.get_by_role('button', name='Save configuration version', exact=True).press('Enter')
            expect(page.get_by_role('combobox', name='Configuration', exact=True)).to_have_value('browser-three-trials')
            expect(page.get_by_role('table', name='CS parameters 112 days', exact=True)).to_contain_text('100')
            page.get_by_role('button', name='Calculate and save evaluation', exact=True).press('Enter')
            expect(page.get_by_role('heading', level=1)).to_have_text('Saved performance evaluation')
            saved_url = page.url
            for width in (1280, 375):
                page.set_viewport_size({'width': width, 'height': 900})
                parameters = page.get_by_role('table', name='CS parameters 112 days', exact=True)
                expect(parameters.get_by_role('row').nth(1)).to_have_text('310013')
                expect(page.get_by_role('table', name='OLS residuals 112 days', exact=True).get_by_role('row')).to_have_count(4)
                expect(page.get_by_text('Leave-one-distance-out:', exact=False).first).to_contain_text('NOT_AVAILABLE')
                region = page.get_by_role('region', name='Riegel source and target comparisons', exact=True)
                region.focus()
                page.keyboard.press('ArrowRight')
                expect(region).to_be_focused()
                page.get_by_text('Complete observation and known context', exact=True).first.press('Enter')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_text('Complete observation and known context', exact=True).first.press('Enter')
                parameters.scroll_into_view_if_needed()
                page.screenshot(path=str(tmp_path / f'estimator-{width}px.png'))
            with page.expect_response(lambda res: '/api/estimators/' in res.url) as response:
                page.get_by_role('link', name='Saved evaluation JSON and complete copied source evidence', exact=True).press('Enter')
            frozen = response.value.json()
            assert frozen['knowledge_cutoff_utc'] is None
            assert frozen['config']['review_policy']['d_prime_min_m'] == 0
            assert frozen['config']['review_policy']['max_loo_relative_cs_change'] is None
            assert frozen['validation']['personal_predictive'] == 'UNTESTED'
            page.goto(saved_url)
            page.get_by_role('link', name='Configure another evaluation and view history', exact=True).press('Enter')
            page.get_by_role('table', name='Saved evaluations', exact=True).get_by_role('link', name='Open saved evaluation').first.press('Enter')
            expect(page).to_have_url(saved_url)
            assert errors == []


def test_saved_non_pb_can_be_selected_and_empty_cs_does_not_fall_back_in_browser(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path / 'synthetic.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            load_trials(client)
            source = client.get('/api/activities/3/efforts').json()
            response = client.post('/api/activities/3/efforts/inspect', json={
                'version': source['version_id'], 'source': source['selected_fit_sha256'],
                'start_distance_m': 100.5, 'distance_m': 400})
            response.raise_for_status()
            candidate = response.json()['candidate']
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width': 375, 'height': 900})
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('console', lambda msg: errors.append(msg.text) if msg.type in ('warning', 'error') else None)
            page.goto(base + '/estimators?cutoff=2026-01-15T00:00:00Z')
            page.get_by_label('Use window bests for Riegel', exact=True).uncheck()
            page.get_by_label('Use window bests for CS', exact=True).uncheck()
            choice = page.locator(f'input[name="riegel"][value*="{candidate["id"]}"]')
            choice.focus()
            page.keyboard.press('Space')
            expect(choice).to_be_checked()
            page.get_by_role('button', name='Calculate and save evaluation', exact=True).press('Enter')
            expect(page.get_by_role('heading', level=1)).to_have_text('Saved performance evaluation')
            table = page.get_by_role('table', name='Riegel source and target comparisons', exact=True)
            expect(table.get_by_role('row')).to_have_count(5)
            expect(table).to_contain_text('EXPLORATORY')
            expect(page.get_by_text('MISSING_RECENT', exact=False).first).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            with page.expect_response(lambda res: '/api/estimators/' in res.url) as response:
                page.get_by_role('link', name='Saved evaluation JSON and complete copied source evidence', exact=True).press('Enter')
            result = response.value.json()
            ids = {r['source_observation_id'] for r in result['riegel']['comparisons']}
            selected = [o for o in result['evidence']['observations'] if o['id'] in ids]
            assert len(selected) == 1 and selected[0]['candidate']['id'] == candidate['id']
            assert all(w['primary']['cs_mps'] is None for w in result['critical_speed']['windows'])
            assert errors == []
