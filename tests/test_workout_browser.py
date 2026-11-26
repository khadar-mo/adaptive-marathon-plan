from playwright.sync_api import expect, sync_playwright
import httpx
import json
import time

from test_fit_browser import chrome_executable
from test_health import serve_database
from test_workout_labels import structured_fit


def test_workout_labels_and_qualification_roundtrip_on_desktop_mobile_and_keyboard(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path/'runs.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            response = client.post('/api/imports/fit', files=[('files', ('synthetic.fit', structured_fit()))])
            response.raise_for_status()
            for _ in range(100):
                report = client.get(response.headers['location']).json()
                if report['state'] == 'complete':
                    break
                time.sleep(.05)
            assert report['state'] == 'complete'
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width':1280,'height':900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: errors.append(message.text) if message.type in ('error','warning') else None)
            page.on('requestfailed', lambda request: errors.append(request.url))
            page.on('response', lambda response: errors.append(response.url) if response.status >= 400 else None)
            page.goto(base+'/activities/1')
            page.get_by_role('link',name='Inspect workout labels',exact=True).press('Enter')
            expect(page.get_by_role('heading',level=1)).to_have_text('Activity workout labels')
            for width in (1280,375):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('combobox',name='Planned intent type',exact=True).select_option('LONG_RUN')
            page.get_by_label('Planned intent recorded by',exact=True).fill('Synthetic athlete')
            page.get_by_label('Planned intent source reference',exact=True).fill('Synthetic plan')
            page.get_by_label('Planned intent explanation',exact=True).fill('Long run with a benchmark tag.')
            page.get_by_label('Planned intent tags',exact=True).fill('BENCHMARK_TAG')
            page.get_by_role('button',name='Save planned intent',exact=True).press('Enter')
            expect(page.locator('#planned-label')).to_have_text('LONG_RUN')
            expect(page.locator('#effective-label')).to_contain_text('UNKNOWN')
            page.get_by_role('button',name='Infer from recorded structure',exact=True).press('Enter')
            expect(page.locator('#inferred-label')).to_have_text('THRESHOLD_INTERVALS')
            page.get_by_role('combobox',name='Runner correction type',exact=True).select_option('EASY_AEROBIC')
            page.get_by_label('Runner correction recorded by',exact=True).fill('Synthetic athlete')
            page.get_by_label('Runner correction source reference',exact=True).fill('Actual workout note')
            page.get_by_label('Runner correction explanation',exact=True).fill('<script>literal note</script>')
            page.get_by_role('button',name='Save runner correction',exact=True).press('Enter')
            expect(page.locator('#corrected-label')).to_have_text('EASY_AEROBIC')
            expect(page.locator('#planned-label')).to_have_text('LONG_RUN')
            expect(page.locator('#inferred-label')).to_have_text('THRESHOLD_INTERVALS')
            page.get_by_role('link',name='Continuous efforts and test evidence',exact=True).press('Enter')
            page.get_by_role('row').filter(has=page.get_by_role('cell',name='400',exact=True)).get_by_role('link').first.press('Enter')
            page.get_by_role('combobox',name='Effort category',exact=True).select_option('VERIFIED_TEST')
            page.get_by_label('Recorded by',exact=True).fill('Synthetic athlete')
            page.get_by_label('Context note',exact=True).fill('Controlled effort, no claim of scientific validation.')
            page.get_by_label('Benchmark protocol',exact=True).fill('Synthetic repeated controlled 400m protocol.')
            page.get_by_role('combobox',name='Explicit maximal confirmation',exact=True).select_option('yes')
            page.get_by_role('combobox',name='Controlled test context',exact=True).select_option('CONTROLLED')
            page.get_by_label('Evidence reference',exact=True).fill('Synthetic protocol attestation')
            page.get_by_label('Verification method',exact=True).fill('Runner confirmed the stated protocol was completed.')
            page.get_by_role('button',name='Save effort context',exact=True).press('Enter')
            expect(page.locator('#effort-qualification')).to_contain_text('SUPPORTED_ATTESTATION')
            page.get_by_text('Workout classification and effort qualification',exact=True).press('Enter')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            with page.expect_response(lambda response:'/api/activities/1/efforts/' in response.url) as saved:
                page.get_by_role('link',name='Verification JSON',exact=True).press('Enter')
            q = saved.value.json()['candidate']['provenance']
            assert q['qualification']['control_state'] == 'CONTROLLED'
            assert q['qualification']['rest_state'] == 'UNKNOWN'
            assert q['session_classification']['effective'] == 'EASY_AEROBIC'
            assert errors == []


def test_label_import_config_and_stale_edit_error_are_visible(tmp_path):
    chrome = chrome_executable()
    with serve_database(tmp_path/'runs.sqlite3') as port, sync_playwright() as driver:
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            response = client.post('/api/imports/fit', files=[('files', ('synthetic.fit', structured_fit()))])
            response.raise_for_status()
            for _ in range(100):
                if client.get(response.headers['location']).json()['state'] == 'complete':
                    break
                time.sleep(.05)
            row = client.get('/api/activities/1/workout').json()
            document = {'activity_id':1,'label':{'layer':'planned','primary':'LONG_RUN','canonical_version_id':row['canonical_version_id_now'],
                'actor':'Synthetic athlete','explanation':'Imported original intent','source':'owned-export','source_reference':'synthetic.json'}}
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={'width':375,'height':900})
            errors, expected_http = [], {'active':False,'seen':[]}
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('requestfailed',lambda request:errors.append(request.url))
            page.on('console',lambda message:errors.append(message.text) if message.type in ('warning','error') and not expected_http['active'] else None)
            page.on('response',lambda response:expected_http['seen'].append(response.status) if response.status>=400 and expected_http['active'] else errors.append(response.url) if response.status>=400 else None)
            page.goto(base+'/workouts')
            page.get_by_label('New configuration version',exact=True).fill('browser-map')
            page.get_by_role('textbox',name='Mapping JSON',exact=True).fill('{"completed_step_names":{"THRESHOLD_INTERVALS":"TEMPO_THRESHOLD"}}')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.get_by_role('button',name='Save classifier configuration',exact=True).press('Enter')
            expect(page.get_by_text('Saved classifier configurations',exact=True)).to_be_visible()
            page.get_by_label('Label JSON file',exact=True).set_input_files({'name':'intent.json','mimeType':'application/json','buffer':json.dumps(document).encode()})
            page.get_by_role('button',name='Import label',exact=True).press('Enter')
            expect(page.locator('#planned-label')).to_have_text('LONG_RUN')
            page.get_by_role('combobox',name='Classifier version',exact=True).select_option('browser-map')
            page.get_by_role('button',name='Infer from recorded structure',exact=True).press('Enter')
            expect(page.locator('#inferred-label')).to_have_text('TEMPO_THRESHOLD')
            # Another client corrects the same revision while the form remains open.
            with httpx.Client(base_url=base) as client:
                current=client.get('/api/activities/1/workout').json()
                changed={**document['label'],'supersedes':current['layers']['planned']['id'],'primary':'RECOVERY_EASY'}
                assert client.post('/api/activities/1/workout',json=changed).status_code==200
            page.get_by_label('Planned intent recorded by',exact=True).fill('Stale editor')
            page.get_by_role('textbox',name='Planned intent explanation',exact=True).fill('Stale revision must not replace the newer label.')
            expected_http['active']=True
            page.get_by_role('button',name='Save planned intent',exact=True).press('Enter')
            expect(page.locator('#workout-status')).to_contain_text('This label changed; reload')
            assert expected_http['seen']==[409]
            expected_http['active']=False
            page.reload()
            expect(page.locator('#planned-label')).to_have_text('RECOVERY_EASY')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors==[]


def test_note_only_edits_preserve_all_references_and_explicit_removal_works(tmp_path):
    chrome=chrome_executable()
    with serve_database(tmp_path/'runs.sqlite3') as port, sync_playwright() as driver:
        base=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base) as client:
            response=client.post('/api/imports/fit',files=[('files',('synthetic.fit',structured_fit()))])
            response.raise_for_status()
            for _ in range(100):
                if client.get(response.headers['location']).json()['state']=='complete':
                    break
                time.sleep(.05)
            workout=client.get('/api/activities/1/workout').json()
            extraction=client.get('/api/activities/1/efforts').json()
            candidates=[r['best'] for r in extraction['inspected_source']['result']['distances'] if r['distance_m'] in (400,800)]
            refs=[{'version_id':extraction['version_id'],'candidate_id':c['id']} for c in candidates]
            label={'layer':'planned','primary':'LONG_RUN','canonical_version_id':workout['canonical_version_id_now'],
                'actor':'Synthetic runner','explanation':'Original planned note','source':'owned-plan','source_reference':'Original source note',
                'tags':['BENCHMARK_TAG'],'planned_session_ref':'synthetic-plan/session-42','supporting_efforts':refs}
            assert client.post('/api/activities/1/workout',json=label).status_code==200
            effort_url=f'/activities/1/efforts/{extraction["version_id"]}/{candidates[0]["id"]}'
            declaration={'category':'RACE','tags':['FATIGUED_BLOCK','Synthetic-tag'],'actor':'Synthetic runner','note':'Original context note',
                'protocol':'Original race conditions','qualification':{'maximality':'NOT_MAXIMAL','rest_state':'FATIGUED','control_state':'CONTROLLED',
                'race_distance_verification':'DOCUMENTED','race_time_verification':'ATTESTED','evidence_refs':[
                    {'kind':'source','method':'Original recorded sample','raw_sha256':extraction['selected_fit_sha256'],'message_group':'record_mesgs','index':0},
                    {'kind':'document','method':'Checked recorded result','reference':'Synthetic race result'},
                    {'kind':'athlete_attestation','method':'Runner statement','reference':'Synthetic tired effort statement'}]}}
            saved=client.post('/api'+effort_url+'/context',json=declaration)
            assert saved.status_code==200,saved.text
            original=saved.json()['candidate']['context_history'][-1]
        with driver.chromium.launch(executable_path=chrome) as browser:
            page=browser.new_page(viewport={'width':375,'height':900})
            page.set_default_timeout(5000)
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type in ('error','warning') else None)
            page.on('requestfailed',lambda request:errors.append(request.url))
            page.on('response',lambda response:errors.append(response.url) if response.status>=400 else None)
            page.goto(base+'/activities/1/workout')
            expect(page.get_by_label('Planned intent recorded by',exact=True)).to_have_value(label['actor'])
            page.get_by_role('textbox',name='Planned intent explanation',exact=True).fill('Only the planned note changed')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.get_by_role('button',name='Save planned intent',exact=True).press('Enter')
            expect(page.get_by_role('textbox',name='Planned intent explanation',exact=True)).to_have_value('Only the planned note changed')
            with httpx.Client(base_url=base) as client:
                planned=client.get('/api/activities/1/workout').json()['layers']['planned']['payload']
                assert planned['planned_session_ref']==label['planned_session_ref']
                assert [{k:r[k] for k in ('version_id','candidate_id')} for r in planned['supporting_efforts']]==refs
                assert all(planned[k]==label[k] for k in ('primary','tags','actor','source','source_reference'))
            page.get_by_role('button',name='Remove planned intent supporting efforts',exact=True).click()
            page.get_by_label('Planned intent session reference',exact=True).fill('')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.get_by_role('button',name='Save planned intent',exact=True).press('Enter')
            expect(page.get_by_label('Planned intent session reference',exact=True)).to_have_value('')
            with httpx.Client(base_url=base) as client:
                planned=client.get('/api/activities/1/workout').json()['layers']['planned']['payload']
                assert planned['supporting_efforts']==[] and planned['planned_session_ref'] is None
            page.goto(base+effort_url)
            expect(page.get_by_role('combobox',name='Effort category',exact=True)).to_have_value('RACE')
            expect(page.get_by_role('combobox',name='Explicit maximal confirmation',exact=True)).to_have_value('known_no')
            page.get_by_role('textbox',name='Context note',exact=True).fill('Only the context note changed')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.get_by_role('button',name='Save effort context',exact=True).press('Enter')
            expect(page.get_by_role('textbox',name='Context note',exact=True)).to_have_value('Only the context note changed')
            with httpx.Client(base_url=base) as client:
                after=client.get('/api'+effort_url).json()['candidate']['context_history'][-1]
                assert all(after[k]==original[k] for k in ('category','tags','actor','protocol','maximal_confirmed','qualification'))
                assert len(after['qualification']['evidence_refs'])==3
            page.get_by_role('button',name='Remove all effort evidence references',exact=True).click()
            for label_name in ('Race distance evidence','Race elapsed-time evidence'):
                page.get_by_role('combobox',name=label_name,exact=True).select_option('UNKNOWN')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.get_by_role('button',name='Save effort context',exact=True).press('Enter')
            expect(page.locator('#effort-qualification')).to_contain_text('Race evidence: UNVERIFIED')
            with httpx.Client(base_url=base) as client:
                after=client.get('/api'+effort_url).json()['candidate']['context_history'][-1]
                assert after['qualification']['evidence_refs']==[] and after['category']=='RACE'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors==[]
