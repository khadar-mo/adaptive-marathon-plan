"""Real activity-page entry/correction on desktop and mobile with synthetic data."""
from playwright.sync_api import expect, sync_playwright

from test_canonical import trace_fit
from test_fit_browser import chrome_executable
from test_health import serve_database


def test_activity_fueling_entry_correction_exact_text_missingness_and_keyboard(tmp_path):
    with serve_database(tmp_path/'runs.sqlite3') as port, sync_playwright() as driver:
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page=browser.new_page(viewport={'width':1280,'height':900})
            page.set_default_timeout(10000)
            base=f'http://127.0.0.1:{port}'
            errors=[]
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type in ('warning','error') else None)
            page.on('requestfailed',lambda request:errors.append(request.url))
            page.goto(base+'/imports')
            page.get_by_label('FIT files',exact=True).set_input_files({'name':'synthetic-run.fit','mimeType':'application/octet-stream','buffer':trace_fit()})
            page.get_by_role('button',name='Upload FIT files').click()
            expect(page.get_by_text('Complete',exact=True)).to_be_visible()
            page.get_by_role('link',name='Activity 1',exact=True).first.click()
            before=page.request.get(base+'/api/activities/1').json()
            page.get_by_role('link',name='Record fueling practice and tolerance').press('Enter')
            expect(page.get_by_role('heading',level=1)).to_have_text('Fueling practice and tolerance')
            expect(page.get_by_text('No fueling entry supplied at this cutoff; practice and tolerance are unknown.',exact=True)).to_be_visible()
            form=page.locator('form.context-entry')
            for label,value in [('Source record ID','browser-fueling'),('Reported by','Synthetic runner'),('Report and notes timestamp','2026-10-25T14:00:00+00:00')]:
                form.get_by_label(label,exact=True).fill(value)
            strategy='\n  My own 60.12345678901234567890 g/h at 00:30\n  water ♥  '
            form.get_by_label('Your food / drink strategy',exact=True).fill(strategy)
            form.get_by_label('Practice status',exact=True).select_option('COMPLETED')
            form.get_by_role('button',name='Save fueling practice').press('Enter')
            expect(page.get_by_role('heading',name='self-report / browser-fueling · revision 1',exact=True)).to_be_visible()
            data=page.request.get(base+'/api/activities/1/fueling').json()
            initial,=data['reports']
            assert initial['user_strategy']==strategy and initial['tolerance_status']=='UNKNOWN'
            cutoff=initial['known_at']
            page.get_by_role('link',name='Inspect exact revision',exact=True).click()
            expect(page.get_by_role('heading',level=1)).to_have_text('Fueling exact revision')
            expect(page.get_by_text('Revision and source provenance',exact=True)).to_be_visible()
            page.get_by_role('link',name='Back to activity fueling history').click()
            # An existing imported CRLF note must survive a correction to another field.
            correction={'kind':'fueling','activity_id':1,'source':'self-report','source_id':'browser-fueling',
                        'actor':'Synthetic runner','reported_at':'2026-10-25T14:00:00+00:00','practice_status':'COMPLETED',
                        'user_strategy':strategy,'notes':'\n  Imported line one\r\nline two\rfinal ♥  ',
                        'supersedes':initial['version_id'],'correction_reason':'Synthetic existing-format notes'}
            response=page.request.post(base+'/api/activities/1/fueling',data=correction)
            assert response.status==200,response.text()
            page.reload()
            page.set_viewport_size({'width':375,'height':812})
            page.get_by_role('link',name='Correct fueling record',exact=True).press('Enter')
            form=page.locator('form.context-entry')
            expect(form.get_by_label('Your food / drink strategy',exact=True)).to_have_value(strategy)
            expect(form.get_by_label('Notes',exact=True)).to_have_value(correction['notes'].replace('\r\n','\n').replace('\r','\n'))
            form.get_by_label('Correction reason',exact=True).fill('Added my tolerance report')
            form.get_by_label('GI tolerance / symptoms',exact=True).fill('  Mild nausea, then comfortable.  ')
            form.get_by_label('Tolerance report timestamp',exact=True).fill('2026-10-25T13:30:00Z')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            form.get_by_role('button',name='Save fueling correction').press('Enter')
            expect(page.get_by_role('heading',name='self-report / browser-fueling · revision 3',exact=True)).to_be_visible()
            final,=page.request.get(base+'/api/activities/1/fueling').json()['reports']
            assert final['user_strategy']==strategy and final['notes']==correction['notes']
            assert final['tolerance']=='  Mild nausea, then comfortable.  ' and final['tolerance_status']=='REPORTED'
            assert final['research_status']=='RESEARCH_PENDING'
            page.get_by_label('As-of timestamp',exact=True).fill(cutoff)
            page.get_by_role('button',name='Inspect fueling cutoff').press('Enter')
            expect(page.get_by_role('heading',name='self-report / browser-fueling · revision 1',exact=True)).to_be_visible()
            assert not page.get_by_text(final['tolerance'],exact=True).count()
            for width in (375,1280):
                page.set_viewport_size({'width':width,'height':812})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.request.get(base+'/api/activities/1').json()==before
            assert errors==[]
