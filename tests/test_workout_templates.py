"""Template inputs, exact arithmetic, history and failure cases through the app."""
from copy import deepcopy
import asyncio
from decimal import Decimal, localcontext
import gzip
import json
from pathlib import Path
import sqlite3

import pytest
import httpx

from storage import Archive
from test_health import serve_database
from test_imports import history, server
import workout_templates as templates


API = "/api/workout-templates"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def block(key, kind, metres, **extra):
    return {"block_id":key, "kind":kind, "distance_m":metres, "intensity":"Fixture effort",
            "intensity_method":"Runner description", **extra}


def submission(**changes):
    return {"request_id":"first", "expected_revision":None, "name":"Synthetic interval set", "actor":"Fixture runner",
            "reason":"Fixture arithmetic", "primary":"THRESHOLD_INTERVALS", "role":"PRIMARY_QUALITY",
            "tags":["BENCHMARK_TAG", "literal, comma"],
            "blocks":[block("wu","WARM_UP",500), block("rep1","WORK",400,protected=True),
                      block("rec1","ACTIVE_RECOVERY",150,rest_after_s="37.125"),
                      block("rep2","WORK",400,protected=True), block("cd","COOL_DOWN",550,min_m=0,max_m=700,allow_split=True)],
            **changes}


async def test_repetitions_bounds_and_historical_decisions(history, tmp_path, monkeypatch):
    clock = ["2026-11-25T10:00:00.100001+00:00"]
    monkeypatch.setattr(templates.inputs,"now",lambda:clock[0])
    doc = submission()
    async with server() as client:
        assert (await client.get(API)).json()["templates"] == []
        resp = await client.post(API+"/interval",json=doc)
        assert resp.status_code == 200, resp.text
        first=resp.json()["record"]
        a=first["accounting"]
        assert (a["total_m"],a["work_m"],a["easy_m"],a["rest_s"]) == (2000,800,1200,"37.125")
        assert [b["block_id"] for b in a["blocks"]] == ["wu","rep1","rec1","rep2","cd"]
        named = API+"/interval/versions/"+first["version_id"]
        preview = (await client.post(named+"/preview",json={"distances_m":{"cd":551}})).json()["accounting"]
        assert (preview["total_m"],preview["work_m"],preview["easy_m"])==(2001,800,1201)
        for changes in ({"rep1":401},{"cd":701},{"rep2":0},{"new":1},{"cd":True},{"cd":550.5}):
            assert (await client.post(named+"/preview",json={"distances_m":changes})).status_code==422
        assert (await client.get(named)).json()==first
        replay=await client.post(API+"/interval",json=doc)
        assert replay.json()["record"]==first and replay.json()["replayed"]
        assert (await client.post(API+"/other",json=doc)).status_code==409
        assert (await client.post(API+"/interval",json={**doc,"tags":[]})).status_code==409
        assert (await client.post(API+"/interval",json={**doc,"request_id":"stale"})).status_code==409
        clock[0]="2026-11-25T10:00:00.100002+00:00"
        edit={**doc,"request_id":"second","expected_revision":first["version_id"],"name":"Revised interval set"}
        second=(await client.post(API+"/interval",json=edit)).json()["record"]
        assert second["revision"]==2 and second["previous_version_id"]==first["version_id"]
    async with server() as client:
        now=(await client.get(API+"/interval")).json()
        assert now["record"]==second and [v["revision"] for v in now["versions"]]==[2,1]
        cut={"as_of":"2026-11-25T11:00:00.100001+01:00"}
        assert (await client.get(API+"/interval",params=cut)).json()["record"]==first
        assert (await client.get(API,params=cut)).json()["templates"][0]["name"]==first["name"]
        assert (await client.get(API+"/interval/versions/"+second["version_id"],params=cut)).status_code==404
        assert (await client.post(API+"/interval/versions/"+second["version_id"]+"/preview",
                                  params=cut,json={"distances_m":{}})).status_code==404
        assert (await client.get(named)).json()==first
        assert (await client.get(API,params={"as_of":"2026-11-25T10:00:00.100000Z"})).json()["templates"]==[]
        html=(await client.get("/workout-templates/interval",params=cut)).text
        assert "template-editor" not in html and "literal, comma" in html and "Revised interval set" not in html
    with sqlite3.connect(tmp_path/"runs.sqlite3") as db:
        db.row_factory=sqlite3.Row
        assert templates.read_version(db,"interval",version_id=first["version_id"])==first


def pace(unit="s/km", low="300", high="330", **extra):
    return {"unit":unit, "lower":low, "upper":high,"method":"Fixture measurement","source_ref":"fixture:cal-01",
            "calibration":"Fixture only","evidence_known_at":"2025-03-05T08:00:00Z","status":"SUPPORTED",
            "uncertainty":"Test values",**extra}


async def test_exact_duration_mixed_units_rest_and_unknown_zone(history):
    lo="4.250000000000000000000000000000000000001"
    parts=[block("a","WORK",850,pace=pace("min/km",lo,"4.3"),protected=True,rest_after_s="9.0001"),
           block("b","ACTIVE_RECOVERY",100,pace=pace("s/m","0.40","0.445"),zone={"method":"device five-zone","band":"Z2"}),
           block("c","EASY_VOLUME",351,pace=pace(),zone={"method":"runner three-zone","band":"low","calibration":"fixture-zy"})]
    doc=submission(blocks=parts)
    # Supply the pace as a JSON number. Its significant digits exceed JavaScript or Python binary double precision.
    wire=json.dumps(doc).replace('"'+lo+'"',lo)
    async with server() as client:
        resp=await client.post(API+"/bounds",content=wire,headers={"Content-Type":"application/json"})
        assert resp.status_code==200,resp.text
        r=resp.json()["record"];a=r["accounting"]
        assert r["blocks"][0]["pace"]["lower"]==lo
        with localcontext() as ctx:
            ctx.prec=100
            expect=Decimal(lo)*60*850/1000+Decimal(".4")*100+Decimal(300)*351/1000+Decimal("9.0001")
            upper=Decimal("4.3")*60*850/1000+Decimal(".445")*100+Decimal(330)*351/1000+Decimal("9.0001")
            assert Decimal(a["duration_lower_s"])==expect and Decimal(a["duration_upper_s"])==upper
        assert [s["zone_status"] for s in a["blocks"]]==["ZONE_UNVERIFIED","ZONE_UNVERIFIED","METHOD_AND_CALIBRATION_RECORDED"]
        assert r["blocks"][1]["zone"]["band"]=="Z2" and r["blocks"][1]["zone"]["calibration"] is None
        earlier=r["known_at"]
        later=deepcopy(doc);later.update(request_id="weak", expected_revision=r["version_id"])
        later["blocks"][0]["pace"]={"unit":"min/km","lower":lo,"upper":"4.3","status":"UNVERIFIED"}
        weak=(await client.post(API+"/bounds",json=later)).json()["record"]
        assert weak["accounting"]["duration_lower_s"] is None and weak["accounting"]["duration_upper_s"] is None
        assert weak["accounting"]["missing_pace_block_ids"]==["a"] and weak["blocks"][0]["pace"]["lower"]==lo
        assert (await client.get(API+"/bounds",params={"as_of":earlier})).json()["record"]==r


@pytest.mark.parametrize("change",[
    {"distance_m":2.5},{"distance_m":True},{"distance_m":-1},{"kind":"STRENGTH_CONDITIONING"},
    {"min_m":400},{"min_m":600,"max_m":601},{"protected":True,"allow_split":True},
    {"kind":"WORK","min_m":50,"max_m":550},{"rest_after_s":"NaN"},{"rest_after_s":"-0.25"},
    {"intensity":"  "},{"intensity_method":"\x00"},{"pace":pace(low="360",high="300")},
    {"pace":pace(calibration=None)},{"pace":pace(evidence_known_at="2026-11-20")},
    {"pace":pace(evidence_known_at="2099-01-01T00:00:00Z")},{"pace":pace(unit="mph")},
])
async def test_invalid_block_is_rejected_without_saved_template(history, change):
    doc=submission();doc["blocks"][0].update(change)
    async with server() as client:
        response=await client.post(API+"/bad",json=doc)
        assert response.status_code==422,response.text
        assert (await client.get(API)).json()["templates"]==[]


async def test_taxonomy_duplicate_ids_and_schema_validation(history):
    async with server() as client:
        for data in (submission(primary="BENCHMARK_TAG"),submission(role="BENCHMARK_TAG"),
                     submission(tags=[" ","x"]),submission(actor=" "),submission(expected_revision="x"),
                     submission(blocks=[block("x","WORK",400),block("x","WORK",400)]),
                     submission(blocks=[block("x","EASY_VOLUME",0)])):
            assert (await client.post(API+"/invalid",json=data)).status_code==422
        assert (await client.post(API+"/bad",content='{"name":"a","name":"b"}')).status_code==422
        assert (await client.post(API+"/bad",content=b" "*((2*1024*1024)+1))).status_code==413
        assert (await client.post(API+"/cross",json=submission(),headers={"Origin":"https://elsewhere.invalid"})).status_code==403
        assert (await client.get(API,params={"as_of":"invalid"})).status_code==422


async def test_genuine_older_store_and_completed_history_preserved(history,tmp_path):
    fixture=json.loads(gzip.decompress((Path(__file__).parent/"fixtures"/"fueling-g0-4a0630f.json.gz").read_bytes()))
    database=tmp_path/"runs.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(fixture["sql"])
    archive = Archive(tmp_path / "archive")
    raw = bytes.fromhex(fixture["raw_hex"])
    sha = archive.put(raw)
    def actuals():
        with sqlite3.connect(database) as db:
            return {t:list(db.execute("SELECT * FROM "+t+" ORDER BY rowid"))
                    for t in ("activities","imports","activity_sources")}
    old=actuals();original=database.read_bytes()
    async with server() as client:
        original_run = (await client.get("/api/activities/1")).json()
        assert (await client.get("/workout-templates")).status_code==200
        assert (await client.get(API)).json()["templates"]==[] and database.read_bytes()==original
        saved=await client.post(API+"/legacy",json=submission())
        assert saved.status_code==200,saved.text
        assert (await client.get(API+"/legacy")).json()["record"]["accounting"]["work_m"]==800
        assert (await client.get("/api/activities/1")).json() == original_run
    assert actuals()==old
    assert archive.read(sha) == raw


async def test_two_servers_serialize_duplicate_and_competing_writes(tmp_path):
    path = tmp_path / "running.sqlite3"
    with serve_database(path) as first_port, serve_database(path) as second_port:
        async with httpx.AsyncClient(trust_env=False) as client:
            urls = [f"http://127.0.0.1:{p}{API}/race" for p in (first_port, second_port)]
            doc = submission()
            created = await asyncio.gather(*(client.post(url, json=doc) for url in urls))
            assert [r.status_code for r in created] == [200, 200]
            assert sorted(r.json()["replayed"] for r in created) == [False, True]
            previous = created[0].json()["record"]
            assert created[1].json()["record"] == previous
            edits = [{**doc, "expected_revision": previous["version_id"], "request_id": key, "name": key}
                     for key in ("option-a", "option-b")]
            changed = await asyncio.gather(*(client.post(url, json=edit) for url, edit in zip(urls, edits)))
            assert sorted(r.status_code for r in changed) == [200, 409]
            for url in urls:
                assert len((await client.get(url)).json()["versions"]) == 2
