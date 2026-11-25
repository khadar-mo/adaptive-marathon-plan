"""Execute the rendered production submit handler and send its bytes to real HTTP.

Node provides a bounded JS harness for this regression; CI's mandatory browser
flow also exercises actual DOM entry and the same high-precision exercise value.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess

import httpx
import pytest
from test_health import serve_database


HARNESS = r'''
const fs=require('node:fs');
const vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
let submit, redirected, requestBody, responseStatus;
const status={textContent:''};
const form={action:input.url,dataset:{kind:'strength'},
 classList:{contains:name=>name==='context-entry'},
 querySelector:()=>status,addEventListener:(name,handler)=>{if(name==='submit')submit=handler;}};
class EntryData {
 *[Symbol.iterator](){yield* input.entries;}
 get(name){return (input.entries.find(([key])=>key===name)||[])[1];}
 getAll(name){return input.entries.filter(([key])=>key===name).map(([,value])=>value);}
}
vm.runInNewContext(input.script,{document:{querySelectorAll:()=>[form]},FormData:EntryData,
 location:{assign:value=>{redirected=value;}},
 fetch:async(url,options)=>{requestBody=options.body; const response=await fetch(url,options);responseStatus=response.status;return response;}});
(async()=>{await submit({preventDefault(){},submitter:{disabled:false}});
 process.stdout.write(JSON.stringify({message:status.textContent,redirected,requestBody,responseStatus}));
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
'''


def test_rendered_form_preserves_unquoted_decimal_lexemes_to_real_storage(tmp_path):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node runs the local production-JS regression; CI browser replay independently covers this flow')
    exercise_json='[{"name":"Squat","sets":3,"load":20.12345678901234567890,"load_unit":"kg"},{"name":"Press","load":1.2345678901234567890e-3,"load_unit":"kg"}]'
    entries=[['source','self-report'],['source_id','form-precision'],['local_date','2026-10-25'],
             ['duration_s','3600.25'],['status','completed'],['exercises',exercise_json]]
    with serve_database(tmp_path/'runs.sqlite3') as port:
        base=f'http://127.0.0.1:{port}'
        html=httpx.get(base+'/context').text
        script,=re.findall(r'<script>(.*?)</script>',html,re.S)
        result=subprocess.run([node,'-e',HARNESS],input=json.dumps({'script':script,'entries':entries,'url':base+'/api/context/records'}),
                              text=True,capture_output=True,timeout=15,cwd=Path(__file__).resolve().parents[1])
        assert result.returncode==0,result.stderr
        submitted=json.loads(result.stdout)
        assert submitted['responseStatus']==200 and submitted['redirected']=='/context',submitted
        saved,=httpx.get(base+'/api/context').json()['records']
        assert [item['load'] for item in saved['exercises']]==['20.12345678901234567890','0.0012345678901234567890']
        assert exercise_json in submitted['requestBody']
        raw=(tmp_path/'runs-archive'/saved['sources'][0]['raw_sha256']).read_text()
        assert exercise_json in raw
