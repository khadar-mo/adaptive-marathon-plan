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
def register(request: Request, choice: inputs.Definition):
    result = call(inputs.register, request.app.state.store, choice)
    return {**result, "html_url": "/fitness?config_version=" + result["version"]}


@router.post("/api/fitness/run-context")
async def observe(request: Request):
    result = call(inputs.observe, request.app.state.store, await request.body())
    return {**result, "html_url": "/fitness?activity_id=" + str(result["activity_id"])}
