"""Real user entry, correction, import and config flow, required on Linux CI."""
import json
from test_fit_browser import chrome_executable
from test_health import serve_database
from test_canonical import trace_fit


def test_context_desktop_mobile_keyboard_import_and_corrections(tmp_path):
    chrome = chrome_executable()
    from playwright.sync_api import expect, sync_playwright
    with serve_database(tmp_path / 'runs.sqlite3') as port, sync_playwright() as driver:
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width':1280,'height':900})
            page.set_default_timeout(10000)
            base=f'http://127.0.0.1:{port}'
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type in ('warning','error') else None)
            page.goto(base+'/imports')
            page.get_by_label('FIT files',exact=True).set_input_files({'name':'synthetic.fit','mimeType':'application/octet-stream','buffer':trace_fit()})
            page.get_by_role('button',name='Upload FIT files').click()
            expect(page.get_by_text('Complete',exact=True)).to_be_visible()
            page.get_by_role('link',name='Context',exact=True).press('Enter')
            expect(page.get_by_role('heading',level=1)).to_have_text('Context inputs and history')
            page.get_by_text('Wellness observation',exact=True).first.click()
            form=page.locator('form[data-kind=wellness]')
            for label,value in [('Source record ID','browser-rhr'),('Local date','2026-10-25'),('Metric','resting_hr'),('Value','47.12345678901234567890'),('Unit','bpm')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_role('button',name='Save wellness observation').press('Enter')
            expect(page.get_by_text('47.12345678901234567890 bpm',exact=True)).to_be_visible()
            page.get_by_role('link',name='Correct this record').click()
            form=page.locator('form[data-kind=wellness]')
            form.get_by_label('Value',exact=True).fill('48.25')
            form.get_by_label('Correction reason',exact=True).fill('Corrected synthetic transcription')
            form.get_by_role('button',name='Save correction').click()
            expect(page.get_by_text('48.25 bpm',exact=True)).to_be_visible()
            page.get_by_role('link',name='History and source details').click()
            expect(page.get_by_role('heading',name='Revision 1',exact=True)).to_be_visible()
            expect(page.get_by_role('heading',name='Revision 2',exact=True)).to_be_visible()
            page.get_by_role('link',name='Context inputs',exact=True).click()
            page.get_by_text('Input policies and rating scales',exact=True).click()
            form=page.locator('form.context-definition').filter(has=page.locator('input[value=scale]'))
            for label,value in [('Scale version','symptom-0-5-v1'),('Scale name','Synthetic symptom scale'),('Scale minimum','0'),('Scale maximum','5'),('Scale unit','rating'),('Scale description','User-defined amount of named symptom')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_role('button',name='Save rating scale').click()
            page.get_by_text('Post-run check-in',exact=True).first.click()
            form=page.locator('form[data-kind=post_run]')
            for label,value in [('Source record ID','browser-feedback'),('Report timestamp','2026-10-25T13:00:00Z'),('Canonical activity ID','1'),('RPE','4.5'),('Pain location','left calf'),('Pain / discomfort','reported discomfort')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('RPE scale',exact=True).select_option('rpe-0-10-v1')
            form.get_by_label('Distinct pain',exact=True).check()
            form.get_by_role('button',name='Save post-run check-in').click()
            expect(page.get_by_text('distinct_pain · 2026-10-25T13:00:00Z · PLANNER_REVIEW_ONLY',exact=True)).to_be_visible()
            page.get_by_text('Pre-week readiness',exact=True).first.click()
            form=page.locator('form[data-kind=preweek]')
            for label,value in [('Source record ID','browser-week'),('Report timestamp','2026-10-25T14:00:00Z'),('Week beginning Monday','2026-10-26'),('Fatigue','2')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Fatigue scale',exact=True).select_option('symptom-0-5-v1')
            form.get_by_role('button',name='Save pre-week readiness').click()
            expect(page.get_by_role('heading',name='preweek · self-report / browser-week · revision 1')).to_be_visible()
            page.get_by_text('Strength session',exact=True).first.click()
            form=page.locator('form[data-kind=strength]')
            for label,value in [('Source record ID','browser-gym'),('Local date','2026-10-25'),('Duration in seconds','3600.25'),('Exercises (JSON)','[{"name":"Squat","sets":3,"load":20.12345678901234567890,"load_unit":"kg"}]')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Completion status',exact=True).select_option('completed')
            form.get_by_label('Lower-body emphasis',exact=True).select_option('yes')
            form.get_by_role('button',name='Save strength session').click()
            expect(page.get_by_role('heading',name='strength · self-report / browser-gym · revision 1')).to_be_visible()
            page.get_by_text('Source coverage',exact=True).first.click()
            form=page.locator('form[data-kind=coverage]')
            for label,value in [('Source record ID','browser-coverage'),('First covered date','2026-10-20'),('Last covered date','2026-10-24'),('Attestation / manifest timestamp','2026-10-25T15:00:00Z'),('Coverage evidence / missing reason','Synthetic complete exported interval')]:
                form.get_by_label(label,exact=True).fill(value)
            form.get_by_label('Coverage status',exact=True).select_option('COMPLETE')
            form.get_by_role('button',name='Save source coverage').click()
            expect(page.get_by_role('heading',name='coverage · self-report / browser-coverage · revision 1')).to_be_visible()
            raw=b'kind,source,source_id,local_date,metric,value,unit\nwellness,owned-export,csv-sleep,2026-10-25,sleep_duration,25200.25,s\n'
            page.get_by_label('Context file').set_input_files({'name':'synthetic.csv','mimeType':'text/csv','buffer':raw})
            page.get_by_label('Export format',exact=True).select_option('csv')
            page.get_by_role('button',name='Import context',exact=True).click()
            expect(page.get_by_text('25200.25 s',exact=True)).to_be_visible()
            page.get_by_text('Input policies and rating scales',exact=True).click()
            form=page.locator('form.context-definition').filter(has=page.locator('input[value=policy]'))
            form.get_by_label('Policy version',exact=True).fill('browser-median-v1')
            form.get_by_label('Daily aggregation',exact=True).select_option('median')
            form.get_by_role('button',name='Save input policy').click()
            for width in (1280,375):
                page.set_viewport_size({'width':width,'height':812})
                page.get_by_label('Read policy',exact=True).select_option('browser-median-v1')
                page.get_by_role('button',name='Inspect context',exact=True).press('Enter')
                expect(page.get_by_label('Read policy',exact=True)).to_have_value('browser-median-v1')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_text('Daily observation groups and retained conflicts',exact=True).press('Enter')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            response=page.request.get(base+'/api/context')
            data=response.json()
            assert data['report_counts']=={'wellness':2,'post_run':1,'preweek':1,'strength':1,'coverage':1,'fueling':0}
            assert data['strength_running_distance_m']=='0'
            strength,=[row for row in data['records'] if row['kind']=='strength']
            assert strength['exercises'][0]['load']=='20.12345678901234567890'
            assert errors==[]
