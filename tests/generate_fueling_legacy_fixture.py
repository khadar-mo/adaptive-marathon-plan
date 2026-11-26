"""Build a synthetic pre-G1 database through merged G0's normal upload API."""
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

COMMIT = "4a0630f65885b1515e59302075e37c5632bcb2da"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import httpx
import app
from fit_fixture import make_fit

destination=Path(sys.argv[1])
raw=make_fit(metrics={'total_distance':10000,'total_elapsed_time':3600,'total_timer_time':3500})
async def build():
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://127.0.0.1:59901') as client:
            response=await client.post('/api/imports/fit',files={'files':('synthetic-g0.fit',raw,'application/octet-stream')})
            assert response.status_code==202,response.text
            report=(await client.get(response.json()['report_url'])).json()
            assert report['state']=='complete',report
            activity=(await client.get('/api/activities/1')).json()
            assert activity['distance_m']==10000 and activity['elapsed_seconds']==3600
            with sqlite3.connect(destination/'runs.sqlite3') as db:
                assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_activity_heads'").fetchone()
                dump='\n'.join(db.iterdump())
            out={'base_commit':sys.argv[2],'source_sha256':hashlib.sha256(raw).hexdigest(),'sql':dump,
                 'raw_hex':raw.hex(),'expected_distance_m':10000,'expected_elapsed_seconds':3600}
            (destination/'fixture.json').write_text(json.dumps(out,sort_keys=True))
asyncio.run(build())
'''


def main():
    with tempfile.TemporaryDirectory(prefix="fueling-old-g0-") as temporary:
        root=Path(temporary)
        code,data=root/'code',root/'data'
        code.mkdir(); data.mkdir()
        source=subprocess.check_output(['git','archive',COMMIT],cwd=ROOT)
        with tarfile.open(fileobj=BytesIO(source)) as tar:
            tar.extractall(code,filter='data')
        env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(code)+os.pathsep+str(code/'tests'),
             'RUNS_GARMIN_OFFLINE':'1','RUNS_DB_PATH':str(data/'runs.sqlite3'),'RUNS_ARCHIVE_PATH':str(data/'archive')}
        subprocess.run([sys.executable,'-c',SCRIPT,str(data),COMMIT],cwd=code,env=env,check=True)
        target=ROOT/'tests'/'fixtures'/'fueling-g0-4a0630f.json.gz'
        target.write_bytes(gzip.compress((data/'fixture.json').read_bytes(),mtime=0))
        print(target.name,target.stat().st_size)


if __name__ == '__main__':
    main()
