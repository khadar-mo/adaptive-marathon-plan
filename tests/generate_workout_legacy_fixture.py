"""Rebuild the synthetic upgrade fixture using the actual merged G2 source tree."""
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


COMMIT = "59faa544096d9b6fc196e4d0aac2992ca84e9cce"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
import efforts
from storage import Store, Archive
from fit_data import decode_fit, absolute_time
from fit_fixture import START
from test_canonical import trace_fit

destination = Path(sys.argv[1])
archive = Archive(destination/'archive')
store = Store(destination/'runs.sqlite3', archive)
raw = trace_fit([(0,0),(2,10),(4,20),(6,30)])
sha = archive.put(raw)
fit = {**decode_fit(raw), 'sha256':sha}
job = store.start(None,None,source='fit')
store.save(job,fit['fields'],[('fit',sha,'fit')],None,fit,'synthetic-flat.fit')
store.finish(job,'complete')
efforts.recompute(store,config_version='legacy-flat-fifteen',config={**efforts.DEFAULT_CONFIG,'distances_m':[15]})
row = efforts.read(store,'1')
c = row['inspected_source']['result']['distances'][0]['best']
efforts.review(store,'1',row['version_id'],c['id'],'reject','Synthetic reviewer','This one exact interval is rejected.')
inspection = efforts.save_inspection(store,'1',row['version_id'],sha,6.25,15)
efforts.context(store,'1',row['version_id'],inspection['id'],category='TRAINING_FAST',tags=['FATIGUED_BLOCK'],actor='Synthetic athlete',note='Recorded fatigue; no maximality claimed.')
cutoff=(absolute_time(START)+timedelta(days=1,seconds=2)).isoformat()
with sqlite3.connect(store.path) as db:
    dump='\n'.join(db.iterdump())
out={'base_commit':sys.argv[2],'code_sha256':efforts.CODE_SHA256,'version_id':row['version_id'],'rejected_id':c['id'],'inspection_id':inspection['id'],'source_sha256':sha,'cutoff':cutoff,'bests':efforts.bests(store,cutoff,1),'sql':dump,'raw_hex':raw.hex()}
(destination/'fixture.json').write_text(json.dumps(out,sort_keys=True))
'''


def main():
    with tempfile.TemporaryDirectory(prefix="workout-old-g2-") as temporary:
        root = Path(temporary)
        code, data = root/'code', root/'data'
        code.mkdir()
        data.mkdir()
        archive = subprocess.check_output(['git','archive',COMMIT],cwd=ROOT)
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(code,filter='data')
        env = {'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(code)+os.pathsep+str(code/'tests'),'RUNS_GARMIN_OFFLINE':'1'}
        subprocess.run([sys.executable,'-c',SCRIPT,str(data),COMMIT],cwd=code,env=env,check=True)
        value = json.loads((data/'fixture.json').read_text())
        target = ROOT/'tests'/'fixtures'/'workout-g2-59faa54.json.gz'
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(gzip.compress(json.dumps(value,sort_keys=True).encode(),mtime=0))
        print(target.name, target.stat().st_size, value['code_sha256'])


if __name__ == '__main__':
    main()
