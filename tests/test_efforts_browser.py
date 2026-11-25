from playwright.sync_api import expect, sync_playwright
import httpx
import time

from demo_efforts import examples
import efforts
from test_canonical import trace_fit
from test_fit_browser import chrome_executable
from test_health import serve_database


def test_effort_verification_review_context_and_mobile_keyboard(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path / 'runs.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('console', lambda msg: errors.append(msg.text) if msg.type in ('warning', 'error') else None)
            page.on('requestfailed', lambda req: errors.append(req.url))
            page.on('response', lambda res: errors.append(res.url) if res.status >= 400 else None)
            page.goto(base + '/imports')
            page.get_by_label('FIT files', exact=True).set_input_files([
                {"name": name, "mimeType": "application/octet-stream", "buffer": raw} for name, raw in examples().items()])
            page.get_by_role('button', name='Upload FIT files').press('Enter')
            # The three-file background batch includes exact trace extraction;
            # wait for its visible completion, not Playwright's 5-second default.
            expect(page.get_by_text('Complete', exact=True)).to_be_visible(timeout=30_000)
            page.get_by_role('link', name='Activity 1', exact=True).first.click()
            page.get_by_role('link', name='Inspect continuous efforts').press('Enter')
            expect(page.get_by_role('heading', level=1)).to_have_text('Activity continuous efforts')
            records = page.get_by_role('table', name='Activity target distances')
            five = records.get_by_role('row').filter(has=page.get_by_role('cell', name='5000', exact=True))
            expect(five).to_contain_text('1440.000000')
            five.get_by_role('link').first.press('Enter')
            expect(page.get_by_role('heading', level=1)).to_have_text('Verify continuous effort')
            for width in (1280, 375):
                page.set_viewport_size({"width": width, "height": 900})
                page.get_by_text('Start raw records and interpolation', exact=True).press('Enter')
                page.get_by_role('link', name='Next records', exact=True).press('Enter')
                expect(page.get_by_role('table', name='Original interval records')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('link', name='Previous records', exact=True).click()
            page.get_by_label('Reviewer', exact=True).fill('Synthetic reviewer')
            page.get_by_label('Review note', exact=True).fill('Checked both raw endpoint brackets and elapsed subtraction.')
            page.get_by_role('button', name='Accept', exact=True).press('Enter')
            expect(page.get_by_text('Reviewer verdict:', exact=False)).to_contain_text('accept')
            page.get_by_role('combobox', name='Effort category', exact=True).select_option('RACE')
            page.get_by_label('Recorded by', exact=True).fill('Synthetic athlete')
            page.get_by_label('Context note', exact=True).fill('Synthetic race label; maximality not confirmed.')
            page.get_by_role('button', name='Save effort context').press('Enter')
            expect(page.get_by_text('Kind:', exact=False)).to_contain_text('RACE')
            expect(page.get_by_text('Kind:', exact=False)).to_contain_text('Confirmed maximal: NO')
            page.get_by_role('link', name='Back to activity efforts').click()
            page.get_by_label('Start cumulative metres', exact=True).fill('100.5')
            page.get_by_label('Target metres', exact=True).fill('5000')
            page.get_by_role('button', name='Inspect interval', exact=True).press('Enter')
            expect(page.get_by_role('table', name='Exact endpoint brackets')).to_be_visible()
            with page.expect_response(lambda res: '/api/activities/1/efforts/' in res.url) as response:
                page.get_by_role('link', name='Verification JSON', exact=True).click()
            c = response.value.json()['candidate']
            assert 0 < c['start']['fraction'] < 1 and 0 < c['end']['fraction'] < 1
            page.goto(base + '/efforts?cutoff=2026-01-15T00:00:00Z')
            expect(page.get_by_role('table', name='Observed best efforts')).to_contain_text('1500.000000')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors == []


def test_rejected_flat_effort_replacement_preserves_exact_coordinate_in_browser(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path / 'runs.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            uploaded = client.post('/api/imports/fit', files=[('files', ('flat.fit', trace_fit([(0, 0), (2, 10), (4, 20), (6, 30)])))])
            uploaded.raise_for_status()
            for _ in range(100):
                report = client.get(uploaded.headers['location']).json()
                if report['state'] == 'complete':
                    break
                time.sleep(.05)
            assert report['state'] == 'complete'
            response = client.post('/api/efforts/recompute', json={'config_version': 'browser-fifteen-metres',
                'config': {**efforts.DEFAULT_CONFIG, 'distances_m': [15]}})
            response.raise_for_status()
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width': 375, 'height': 900})
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('response', lambda res: errors.append(res.url) if res.status >= 400 else None)
            page.goto(base + '/activities/1/efforts')
            row = page.get_by_role('row').filter(has=page.get_by_role('cell', name='15', exact=True))
            row.get_by_role('link').first.press('Enter')
            expect(page.get_by_text('This interval is the deterministic midpoint', exact=False)).to_be_visible()
            rejected = set()
            for i in range(2):
                rejected.add(page.url.rsplit('/', 1)[-1])
                page.get_by_label('Reviewer', exact=True).fill('Synthetic reviewer')
                page.get_by_label('Review note', exact=True).fill(f'Reject representative {i + 1}; other attained ties remain.')
                page.get_by_role('button', name='Reject', exact=True).press('Enter')
                expect(page.get_by_text('Reviewer verdict:', exact=False)).to_contain_text('reject')
                page.get_by_role('link', name='Best efforts', exact=True).press('Enter')
                row = page.get_by_role('row').filter(has=page.get_by_role('cell', name='15', exact=True))
                expect(row).to_contain_text('3.000000')
                with page.expect_request(lambda req: req.method == 'POST' and req.url.endswith('/efforts/inspect')) as inspected:
                    row.get_by_role('button', name='Inspect activity 1', exact=True).first.press('Enter')
                submitted = inspected.value.post_data_json
                expect(page.get_by_role('heading', level=1)).to_have_text('Verify continuous effort')
                # The form immediately navigates after its fetch. Read the saved
                # verification resource, rather than a discarded fetch body.
                with page.expect_response(lambda res: '/api/activities/1/efforts/' in res.url) as verification:
                    page.get_by_role('link', name='Verification JSON', exact=True).press('Enter')
                result = verification.value.json()['candidate']
                assert isinstance(submitted['start_coordinate_m'], str)
                assert result['start']['distance_coordinate_m'] == submitted['start_coordinate_m']
                assert result['id'] not in rejected and result['elapsed_seconds'] == 3
                page.go_back()
                expect(page.get_by_role('heading', level=1)).to_have_text('Verify continuous effort')
                expect(page.get_by_text('Reviewer verdict:', exact=False)).to_contain_text('UNREVIEWED')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors == []
