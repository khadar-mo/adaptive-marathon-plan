"""Rebuild the old Garmin ZIP and workload-inventory fixture using the merged importer."""
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


BASE = "ba3ed18a9a70a99f06f36800e5dce962915d26d8"
ROOT = Path(__file__).resolve().parents[1]
GENERATOR = r"""
import asyncio, hashlib, json, sqlite3, sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZipInfo
from pytest import MonkeyPatch
import canonical, storage, workload_inputs
from test_canonical import trace_fit
from test_imports import history, server, submit

directory = Path(sys.argv[1])
first = "2025-05-04T10:00:00.100000+00:00"
later = "2025-05-04T10:00:00.700000+00:00"
raw = trace_fit([(0,0),(5,15),(10,30),(15,45)])
archive = BytesIO()
with ZipFile(archive, "w") as z:
    z.writestr(ZipInfo("synthetic.fit", (2024,2,29,10,0,0)), raw)
async def run():
    with MonkeyPatch.context() as patch:
        remote = history.__wrapped__(directory, patch)
        remote.original = archive.getvalue()
        patch.setattr(storage, "now", lambda:first)
        patch.setattr(canonical, "knowledge_time", lambda:first)
        patch.setattr(workload_inputs, "now", lambda:later)
        async with server() as client:
            assert (await submit(client))["state"] == "complete"
            response = await client.post("/api/workload/capture",json={})
            assert response.status_code == 200, response.text
asyncio.run(run())
with sqlite3.connect(directory/"runs.sqlite3") as db:
    assert db.execute("SELECT known_at FROM workload_processing_inventories").fetchone()[0] == later
    dump = "\n".join(db.iterdump())
result = {"base_commit":sys.argv[2], "canonical_at":first, "inventory_at":later,
          "inner_sha":hashlib.sha256(raw).hexdigest(), "outer_sha":hashlib.sha256(archive.getvalue()).hexdigest(),
          "sql":dump}
(directory/"fixture.json").write_text(json.dumps(result,sort_keys=True))
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        code, data = Path(tmp) / "code", Path(tmp) / "data"
        code.mkdir()
        data.mkdir()
        with tarfile.open(fileobj=BytesIO(subprocess.check_output(["git", "archive", BASE], cwd=ROOT))) as archive:
            archive.extractall(code, filter="data")
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": os.pathsep.join(map(str, (code, code/"tests")))}
        subprocess.run([sys.executable, "-c", GENERATOR, data, BASE], cwd=code, env=env, check=True)
        payload = json.loads((data / "fixture.json").read_text())
        target = ROOT / "tests/fixtures/source-history-g3-ba3ed18.json.gz"
        target.write_bytes(gzip.compress(json.dumps(payload,sort_keys=True).encode(),mtime=0))
        print(target.name, target.stat().st_size)


if __name__ == "__main__":
    main()
