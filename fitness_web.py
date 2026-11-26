"""Local fitness profile, comparison inputs, and immutable snapshot routes."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

import efforts
import fitness
import fitness_inputs as inputs
from workload_web import call as checked

router = APIRouter()


def call(fn, *args, **kwargs):
    try:
        return checked(fn, *args, **kwargs)
    except (KeyError, efforts.NotFound) as exc:
        raise HTTPException(404, str(exc)) from None


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str | None = None
    knowledge_cutoff: str | None = None
    mode: str = "EVENT_TIME_RECONSTRUCTION"
    config_version: str = inputs.DEFAULT_VERSION


@router.get("/api/fitness")
def current(request: Request, as_of: str | None = None, knowledge_cutoff: str | None = None,
            mode: str = "EVENT_TIME_RECONSTRUCTION", config_version: str = inputs.DEFAULT_VERSION):
    return call(fitness.read, request.app.state.store, as_of=as_of, knowledge_cutoff=knowledge_cutoff, mode=mode, config_version=config_version)


@router.post("/api/fitness/capture")
def capture(request: Request, query: Query):
    data = call(fitness.capture, request.app.state.store, **query.model_dump())
    return {**data, "html_url": "/fitness/snapshots/" + data["snapshot_id"]}


@router.get("/api/fitness/snapshots")
def history(request: Request):
    return fitness.history(request.app.state.store)


@router.get("/api/fitness/snapshots/{identity}")
def snapshot(request: Request, identity: str):
    return call(fitness.snapshot, request.app.state.store, identity)


@router.get("/api/fitness/snapshots/{identity}/inputs")
def snapshot_inputs(request: Request, identity: str):
    return call(fitness.snapshot, request.app.state.store, identity, inputs_only=True)


@router.get("/api/fitness/snapshots/{identity}/replay")
def replay(request: Request, identity: str):
    return call(fitness.snapshot, request.app.state.store, identity, replay=True)


@router.post("/api/fitness/configurations")
async def register(request: Request):
    choice = call(inputs.parse, await limited_body(request))
    result = call(inputs.register, request.app.state.store, choice)
    return {**result, "html_url": "/fitness?config_version=" + result["version"]}


@router.post("/api/fitness/run-context")
async def observe(request: Request):
    result = call(inputs.observe, request.app.state.store, await limited_body(request))
    return {**result, "html_url": "/fitness?activity_id=" + str(result["activity_id"])}


@router.get("/fitness", response_class=HTMLResponse)
def page(request: Request, as_of: str | None = None, knowledge_cutoff: str | None = None,
         mode: str = "EVENT_TIME_RECONSTRUCTION", config_version: str = inputs.DEFAULT_VERSION, activity_id: int | None = None):
    import fitness_views
    store = request.app.state.store
    query = {"as_of": as_of or None, "knowledge_cutoff": knowledge_cutoff or None, "mode": mode, "config_version": config_version}
    state = call(fitness.read, store, **query)
    row = next((r for r in state["endurance"]["session_evidence"] if r["activity_id"] == activity_id), None)
    if activity_id is not None and row is None:
        raise HTTPException(404, "Completed run unavailable for the selected profile.")
    return fitness_views.profile(state, query, configs=inputs.choices(store, state["knowledge_cutoff"]),
        history=fitness.history(store, known_by=state["knowledge_cutoff"] if knowledge_cutoff or mode == "AS_KNOWN" else None), record=row,
        notes=inputs.activity_notes(store, activity_id, state["knowledge_cutoff"]) if row else ())


@router.get("/fitness/snapshots/{identity}", response_class=HTMLResponse)
def snapshot_page(request: Request, identity: str):
    import fitness_views
    value = call(fitness.snapshot, request.app.state.store, identity)
    return fitness_views.profile(value["state"], {}, saved=value)


async def limited_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 50_000:
            raise HTTPException(413, "Fitness input exceeds 50 KB.")
    return bytes(body)
