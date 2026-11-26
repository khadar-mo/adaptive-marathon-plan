"""Local public G4 calculate/save, versioned choices and immutable history."""
from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse

import efforts
import estimators
import estimator_views


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except efforts.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from None


def register(app):
    @app.get("/api/estimators")
    def preview(cutoff: str | None = None, config_version: str = Query(estimators.DEFAULT_CONFIG_VERSION, pattern=estimators.VERSION_PATTERN)):
        return call(estimators.preview, app.state.store, cutoff, config_version)

    @app.get("/estimators", response_class=HTMLResponse)
    def page(cutoff: str | None = None, config_version: str = Query(estimators.DEFAULT_CONFIG_VERSION, pattern=estimators.VERSION_PATTERN)):
        return estimator_views.dashboard(call(estimators.preview, app.state.store, cutoff, config_version))

    @app.post("/api/estimators/configurations")
    def configuration(definition: estimators.Definition):
        return call(estimators.register, app.state.store, definition)

    @app.post("/api/estimators")
    def calculate(request: estimators.Calculation):
        result = call(estimators.calculate, app.state.store, request)
        return {"evaluation": result, "html_url": "/estimators/" + result["id"], "url": "/api/estimators/" + result["id"]}

    @app.get("/api/estimators/{evaluation_id}")
    def saved(evaluation_id: str):
        return call(estimators.read, app.state.store, evaluation_id)

    @app.get("/estimators/{evaluation_id}", response_class=HTMLResponse)
    def saved_page(evaluation_id: str):
        return estimator_views.saved(call(estimators.read, app.state.store, evaluation_id))
