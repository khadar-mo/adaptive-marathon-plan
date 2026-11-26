"""Local FitnessSnapshot and held-out evaluation interfaces."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import forecast_validation as validation
import validation_refresh
import validation_views
import fm_snapshots as snapshots
from fitness_web import call

router = APIRouter()


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str | None = None
    knowledge_cutoff: str | None = None
    mode: str = "AS_KNOWN"
    config_version: str | None = None
    previous_id: str | None = None


@router.get("/api/snapshots")
def history(request: Request):
    return snapshots.history(request.app.state.store)


@router.post("/api/snapshots")
def capture(request: Request, query: SnapshotRequest):
    return call(snapshots.capture, request.app.state.store, **query.model_dump())


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/api/snapshots/configuration")
def activate(request: Request, choice: Activation):
    return call(validation_refresh.activate, request.app.state.store, **choice.model_dump())


@router.post("/api/snapshots/refresh")
def refresh(request: Request):
    return call(validation_refresh.refresh, request.app.state.store)


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


class Forecast(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    benchmark: str = Field(min_length=1, max_length=220)
    scheduled_start: str
    distance_m: str = Field(min_length=1, max_length=80)
    kind: str = "PROSPECTIVE"
    riegel_observation_id: str | None = None


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome_snapshot_id: str
    outcome_observation_id: str


@router.get("/api/snapshots/{identity}/observations")
def observations(request: Request, identity: str):
    return call(validation.selectable, request.app.state.store, identity)


@router.get("/api/validation")
def ledger(request: Request):
    return validation.list_ledger(request.app.state.store)


@router.get("/api/validation/backtest")
def backtest(request: Request):
    return validation.backtest_estimators(validation.list_ledger(request.app.state.store))


@router.post("/api/validation/predictions")
def forecast(request: Request, choice: Forecast):
    return call(validation.freeze, request.app.state.store, **choice.model_dump())


@router.get("/api/validation/predictions/{identity}")
def saved_prediction(request: Request, identity: str):
    return call(validation.prediction_record, request.app.state.store, identity)


@router.get("/api/validation/predictions/{identity}/replay")
def replay_prediction(request: Request, identity: str):
    return call(validation.prediction_record, request.app.state.store, identity, replay=True)


@router.post("/api/validation/predictions/{identity}/outcomes")
def outcome(request: Request, identity: str, choice: Outcome):
    return call(validation.evaluate, request.app.state.store, identity, **choice.model_dump())


@router.post("/api/validation/audit")
def audit(request: Request):
    return call(validation.audit, request.app.state.store)


@router.get("/api/snapshots/{identity}/activities/{activity_id}")
def frozen_activity(request: Request, identity: str, activity_id: int):
    bundle = call(snapshots.load_snapshot, request.app.state.store, identity, "inputs")
    run = next((r for r in bundle["g5"]["workload"]["activities"] if r["activity_id"] == activity_id), None)
    if not run:
        raise HTTPException(404, "No selected workout in this snapshot.")
    group = next(g for g in bundle["source_evidence"]["groups"] if g["owner_id"] == activity_id)
    return {"activity": run, "source_evidence": group, "trace": bundle["g5"]["capability_evidence"]["traces"].get(str(activity_id))}


@router.get("/validation", response_class=HTMLResponse)
def dashboard(request: Request, snapshot: str | None = None, previous: str | None = None):
    return call(validation_views.page, request.app.state.store, snapshot, previous)


class HistoricalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_cutoff: str
    knowledge_cutoff: str
    config_version: str | None = None
    benchmark: str = Field(min_length=1, max_length=220)
    scheduled_start: str
    distance_m: str = Field(min_length=1, max_length=80)
    kind: str = snapshots.AS_KNOWN
    riegel_observation_id: str | None = None
    outcome: Outcome | None = None


class WalkForward(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[HistoricalCase] = Field(min_length=1, max_length=128)


@router.post("/api/validation/backtest")
def walk_forward(request: Request, choice: WalkForward):
    return call(validation.walk_forward, request.app.state.store, choice.model_dump()["cases"])
