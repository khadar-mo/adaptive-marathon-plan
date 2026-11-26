"""Read-only fixed-session calendar diagnostic."""
from fastapi import APIRouter, Request

from availability_web import invalid
from calendar_fit import Check, check_calendar


router = APIRouter()


@router.post("/api/calendar-fit")
def check(query: Check, request: Request):
    try:
        with request.app.state.store.history() as db:
            return check_calendar(db, query)
    except (ValueError, OverflowError) as exc:
        invalid(exc)
