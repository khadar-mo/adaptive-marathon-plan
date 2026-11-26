"""Local fitness profile API."""
from fastapi import APIRouter, Request

import fitness
from workload_web import call

router = APIRouter()


@router.get("/api/fitness")
def current(request: Request, as_of: str | None = None, knowledge_cutoff: str | None = None,
            mode: str = "EVENT_TIME_RECONSTRUCTION"):
    return call(fitness.read, request.app.state.store, as_of=as_of, knowledge_cutoff=knowledge_cutoff, mode=mode)
