"""Actual History → workload → coverage/calibration/correction/freeze user flow."""
import json

from test_fit_browser import chrome_executable
from test_health import serve_database
from test_fit_summary_spans import summary_fit


def test_workload_history_forms_intensity_correction_freeze_desktop_mobile(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    chrome=chrome_executable()
    with serve_database(tmp_path/'runs.sqlite3') as port, sync_playwright() as driver:
        base=f'http://127.0.0.1:{port}'
        with driver.chromium.launch(executable_path=chrome) as browser:
            page=browser.new_page(viewport={'width':1280,'height':900})
            page.set_default_timeout(15000)
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type in ('warning','error') else None)
            page.goto(base+'/imports')
            page.get_by_label('FIT files',exact=True).set_input_files({'name':'synthetic-summary.fit','mimeType':'application/octet-stream','buffer':summary_fit()})
            page.get_by_role('button',name='Upload FIT files',exact=True).click()
            expect(page.get_by_text('Complete',exact=True)).to_be_visible()
            page.get_by_role('link',name='History',exact=True).press('Enter')
            page.get_by_role('link',name='Workload',exact=True).press('Enter')
            expect(page.get_by_role('heading',level=1)).to_have_text('Training workload')
            page.locator('summary').filter(has_text='Enter running coverage').click()
            form=page.locator('form[data-kind=coverage]')
            for label,value in [('Source record ID','browser-census'),('Evidence / reason','Synthetic complete running record'),
                                ('London From date','2024-02-01'),('London To exclusive date','2024-03-02'),
                                ('Observed through timestamp','2024-03-02T00:00:00Z'),('Attestation / manifest timestamp','2024-03-02T01:00:00Z')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Endpoint basis',exact=True).select_option('BOTH')
            form.get_by_role('button',name='Save running coverage',exact=True).press('Enter')
            expect(page.get_by_role('heading',name='coverage · browser-census:END · revision 1',exact=True)).to_be_visible()
            expect(page.get_by_role('heading',name='coverage · browser-census:START · revision 1',exact=True)).to_be_visible()
            page.get_by_text('Enter a sourced zone profile',exact=True).click()
            form=page.locator('form[data-kind=zone_profile]')
            for label,value in [('Source record ID','browser-zones'),('Evidence / reason','Synthetic measured calibration'),
                                ('Zone method','Supplied synthetic three bands'),('Measured metric','heart_rate'),('Metric unit','bpm'),
                                ('Calibration evidence','Synthetic laboratory method v1; no real physiological claim'),
                                ('Effective from timestamp','2020-01-01T00:00:00Z')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Method reliability',exact=True).select_option('CALIBRATED')
            form.get_by_label('Band definitions (JSON)',exact=True).fill(json.dumps([{'label':'low','upper':140},{'label':'middle','lower':140,'upper':170},{'label':'high','lower':170}]))
            form.get_by_role('button',name='Save zone profile',exact=True).click()
            expect(page.get_by_role('heading',name='zone_profile · browser-zones · revision 1',exact=True)).to_be_visible()
            page.get_by_text('Enter measured intensity seconds',exact=True).click()
            form=page.locator('form[data-kind=band_ledger]')
            for label,value in [('Source record ID','browser-bands'),('Evidence / reason','Synthetic measured timer bands'),
                                ('Measured band seconds (JSON)','{"low":"1000.125","middle":"300.25","high":"100"}'),
                                ('Explicit unclassified seconds','99.625'),('Measurement timestamp','2024-02-28T23:50:00Z'),
                                ('Measurement method / source semantics','Accumulated seconds while timer running')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Measured duration basis',exact=True).select_option('timer_seconds')
            form.get_by_role('button',name='Save measured band ledger',exact=True).click()
            expect(page.get_by_role('heading',name='band_ledger · browser-bands · revision 1',exact=True)).to_be_visible()
            # Correct the explicit END assertion, keeping both immutable revisions visible.
            heading=page.get_by_role('heading',name='coverage · browser-census:END · revision 1',exact=True)
            heading.locator('xpath=following-sibling::p[1]').get_by_role('link',name='Correct this input').click()
            form=page.locator('form[data-kind=coverage]')
            form.get_by_label('Correction reason',exact=True).fill('Clarify synthetic evidence attribution')
            form.get_by_label('Evidence / reason',exact=True).fill('Synthetic census verified against supplied manifest')
            form.get_by_role('button',name='Save correction',exact=True).click()
            heading=page.get_by_role('heading',name='coverage · browser-census:END · revision 2',exact=True)
            expect(heading).to_be_visible()
            heading.locator('xpath=following-sibling::p[1]').get_by_role('link',name='Input revision history').click()
            expect(page.get_by_role('heading',name='Revision 1',exact=True)).to_be_visible()
            expect(page.get_by_role('heading',name='Revision 2',exact=True)).to_be_visible()
            page.get_by_role('link',name='Training workload',exact=True).click()
            page.get_by_label('Event cutoff',exact=True).fill('2024-03-01T12:00:00Z')
            page.get_by_label('Proposed START timestamp',exact=True).fill('2024-03-01T15:00:00Z')
            page.get_by_label('Proposed distance in metres',exact=True).fill('18000')
            page.get_by_role('button',name='Inspect workload',exact=True).press('Enter')
            expect(page.locator('#l30-result')).to_contain_text('CALCULABLE')
            expect(page.locator('#l30-result')).to_contain_text('ratio 3.6')
            page.get_by_text('7-day bounds, durations, intensity and coverage evidence',exact=True).click()
            expect(page.get_by_text('1000.125',exact=False).first).to_be_visible()
            for width in (1280,375):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(tmp_path/f'workload-{width}.png'))
            data=page.request.get(base+'/api/workload?as_of=2024-03-01T12%3A00%3A00Z').json()
            group,=data['rolling'][0]['intensity']['groups']
            assert group['seconds']['low']=='1000.125' and group['unknown_seconds']=='99.625'
            page.get_by_role('button',name='Freeze this calculation',exact=True).press('Enter')
            expect(page.get_by_role('link',name='Frozen evidence JSON',exact=True)).to_be_visible()
            link=page.get_by_role('link',name='Replay frozen inputs',exact=True).get_attribute('href')
            assert page.request.get(base+link).json()['status']=='IDENTICAL'
            assert errors==[]
            print(f'Browser screenshots: {tmp_path}')
