"""Public G2 routes kept separate from importer orchestration."""
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import efforts
import effort_views
import workout_labels


class Recompute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,80}$")
    config: dict | None = None


class Interval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_distance_m: float = Field(ge=0, allow_inf_nan=False, strict=True)
    distance_m: float = Field(gt=0, allow_inf_nan=False, strict=True)
    start_coordinate_m: str | None = Field(None, pattern=r"^\d+(?:/\d+)?$", max_length=160)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "reject", "annotate"]
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=4000)


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["VERIFIED_TEST", "RACE", "TRAINING_FAST", "FATIGUED_BLOCK", "EASY_OR_UNKNOWN"]
    tags: list[str] = Field(default_factory=list, max_length=20)
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=4000)
    protocol: str | None = Field(None, max_length=4000)
    maximal_confirmed: bool = Field(False, strict=True)
    qualification: dict | None = None
    supersedes: str | None = Field(None, pattern=r"^$|^[a-f0-9]{64}$")


def call(function, *args, context_as_of=None, knowledge_as_of=None, **kwargs):
    try:
        result = function(*args, **kwargs)
        return workout_labels.decorate(args[0], result, as_of=context_as_of, knowledge_as_of=knowledge_as_of) if function in (efforts.read, efforts.inspect, efforts.bests) else result
    except workout_labels.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except efforts.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from None


def register(app):
    @app.post("/api/efforts/recompute")
    def recompute_all(choice: Recompute):
        return {"activities": call(efforts.recompute, app.state.store, config_version=choice.config_version, config=choice.config)}

    @app.post("/api/activities/{activity_id}/efforts/recompute")
    def recompute_one(activity_id: str, choice: Recompute):
        return {"activities": call(efforts.recompute, app.state.store, [activity_id], config_version=choice.config_version, config=choice.config)}

    @app.get("/api/efforts")
    def bests(cutoff: str | None = None, days: int | None = Query(None, ge=1, le=36500)):
        return call(efforts.bests, app.state.store, cutoff, days)

    @app.get("/efforts", response_class=HTMLResponse)
    def bests_page(cutoff: str | None = None, days: int | None = Query(None, ge=1, le=36500)):
        return effort_views.bests(call(efforts.bests, app.state.store, cutoff, days))

    @app.get("/api/activities/{activity_id}/efforts")
    def activity(activity_id: str, version: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                 source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$")):
        return call(efforts.read, app.state.store, activity_id, version, source)

    @app.get("/activities/{activity_id}/efforts", response_class=HTMLResponse)
    def activity_page(activity_id: str, version: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                      source: str | None = Query(None, pattern=r"^[a-f0-9]{64}$")):
        return effort_views.activity(call(efforts.read, app.state.store, activity_id, version, source))

    @app.post("/api/activities/{activity_id}/efforts/inspect")
    def selected_interval(activity_id: str, interval: Interval):
        c = call(efforts.save_inspection, app.state.store, activity_id, interval.version, interval.source,
                 interval.start_distance_m, interval.distance_m, interval.start_coordinate_m)
        return {"candidate": c, "html_url": f"/activities/{activity_id}/efforts/{interval.version}/{c['id']}"}

    @app.get("/api/activities/{activity_id}/efforts/{version}/{candidate_id}")
    def inspect(activity_id: str, version: str, candidate_id: str, offset: int = Query(0, ge=0, le=2**31 - 1),
                limit: int = Query(200, ge=1, le=500), as_of: str | None = None, knowledge_as_of: str | None = None):
        return call(efforts.inspect, app.state.store, activity_id, version, candidate_id, offset=offset, limit=limit, context_as_of=as_of, knowledge_as_of=knowledge_as_of)

    @app.get("/activities/{activity_id}/efforts/{version}/{candidate_id}", response_class=HTMLResponse)
    def inspect_page(activity_id: str, version: str, candidate_id: str, offset: int = Query(0, ge=0, le=2**31 - 1),
                     limit: int = Query(200, ge=1, le=500), as_of: str | None = None, knowledge_as_of: str | None = None):
        return effort_views.inspection(call(efforts.inspect, app.state.store, activity_id, version, candidate_id, offset=offset, limit=limit, context_as_of=as_of, knowledge_as_of=knowledge_as_of))

    @app.post("/api/activities/{activity_id}/efforts/{version}/{candidate_id}/review")
    def review(activity_id: str, version: str, candidate_id: str, verdict: Verdict):
        c = call(efforts.review, app.state.store, activity_id, version, candidate_id, verdict.action, verdict.actor, verdict.note)
        return {"candidate": c, "html_url": f"/activities/{activity_id}/efforts/{version}/{candidate_id}"}

    @app.post("/api/activities/{activity_id}/efforts/{version}/{candidate_id}/context")
    def context(activity_id: str, version: str, candidate_id: str, choice: Context):
        c = call(workout_labels.save_effort_context, app.state.store, activity_id, version, candidate_id, **choice.model_dump())
        return {"candidate": c, "html_url": f"/activities/{activity_id}/efforts/{version}/{candidate_id}"}
