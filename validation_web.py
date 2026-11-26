"""Local FitnessSnapshot and held-out evaluation interfaces."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

import fitness_inputs
import fm_snapshots as snapshots
from fitness_web import call

router = APIRouter()


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str | None = None
    knowledge_cutoff: str | None = None
    mode: str = "AS_KNOWN"
    config_version: str = fitness_inputs.DEFAULT_VERSION
    previous_id: str | None = None


@router.get("/api/snapshots")
def history(request: Request):
    return snapshots.history(request.app.state.store)


@router.post("/api/snapshots")
def capture(request: Request, query: SnapshotRequest):
    return call(snapshots.capture, request.app.state.store, **query.model_dump())


@router.get("/api/snapshots/{identity}")
def snapshot(request: Request, identity: str):
    return call(snapshots.load_snapshot, request.app.state.store, identity)


@router.get("/api/snapshots/{identity}/inputs")
def inputs(request: Request, identity: str):
    return call(snapshots.load_snapshot, request.app.state.store, identity, "inputs")


@router.get("/api/snapshots/{identity}/planner")
def planner(request: Request, identity: str):
    return call(snapshots.load_snapshot, request.app.state.store, identity, "planner")


@router.get("/api/snapshots/{identity}/replay")
def replay(request: Request, identity: str):
    return call(snapshots.load_snapshot, request.app.state.store, identity, "replay")


@router.get("/api/snapshots/{identity}/compare/{previous}")
def compare(request: Request, identity: str, previous: str):
    return call(snapshots.compare, request.app.state.store, identity, previous)
