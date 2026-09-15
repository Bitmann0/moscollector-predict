from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from .config import Settings
from .database import MaintenanceRepository
from .model import DataValidationError, analyze

settings = Settings.from_env()


class MaintenanceCreate(BaseModel):
    channel_id: int = Field(gt=0)


class FeedbackCreate(BaseModel):
    decision: Literal["inspection_required", "monitor", "dismissed", "fault_confirmed"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    author: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.load_error = None
    try:
        forecasts, metadata = analyze(settings.data_dir, settings.forecast_hours)
    except DataValidationError as error:
        forecasts, metadata = [], {"model_status": "data_unavailable"}
        app.state.load_error = str(error)
    app.state.forecasts = [forecast.to_dict() for forecast in forecasts]
    app.state.metadata = metadata
    app.state.repository = MaintenanceRepository(settings.database_path)
    yield


app = FastAPI(
    title="МосКоллектор Predict API",
    description="Прогнозирование деградации датчиков СМВУ и превентивное обслуживание.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/api/v1/health")
def health() -> dict:
    ready = not bool(app.state.load_error)
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "detail": app.state.load_error,
        **app.state.metadata,
    }


@app.get("/api/v1/summary")
def summary() -> dict:
    forecasts = app.state.forecasts
    counts = {level: 0 for level in ("critical", "high", "medium", "low")}
    for forecast in forecasts:
        counts[forecast["risk_level"]] += 1
    return {
        **app.state.metadata,
        "risk_counts": counts,
        "active_forecasts": counts["critical"] + counts["high"] + counts["medium"],
        "max_risk_score": max((item["risk_score"] for item in forecasts), default=0),
        "maintenance_requests": len(app.state.repository.list()),
        "forecast_horizon_hours": settings.forecast_hours,
    }


@app.get("/api/v1/forecasts")
def list_forecasts(
    risk_level: Annotated[
        Literal["critical", "high", "medium", "low"] | None, Query()
    ] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 250,
) -> dict:
    items = app.state.forecasts
    if risk_level:
        items = [item for item in items if item["risk_level"] == risk_level]
    if search:
        needle = search.casefold()
        items = [
            item
            for item in items
            if needle
            in " ".join(
                [str(item["channel_id"]), item["sensor_name"], item["sensor_type"], item["location"]]
            ).casefold()
        ]
    return {"total": len(items), "items": items[:limit]}


@app.get("/api/v1/forecasts/{channel_id}")
def get_forecast(channel_id: int) -> dict:
    forecast = next(
        (item for item in app.state.forecasts if item["channel_id"] == channel_id), None
    )
    if not forecast:
        raise HTTPException(status_code=404, detail="Прогноз для канала не найден")
    return forecast


@app.get("/api/v1/maintenance-requests")
def list_maintenance_requests() -> dict:
    items = app.state.repository.list()
    return {"total": len(items), "items": items}


@app.get("/api/v1/forecasts/{channel_id}/feedback")
def list_feedback(channel_id: int) -> dict:
    items = app.state.repository.feedback(channel_id)
    return {"total": len(items), "items": items}


@app.post("/api/v1/forecasts/{channel_id}/feedback", status_code=201)
def add_feedback(channel_id: int, payload: FeedbackCreate) -> dict:
    forecast = get_forecast(channel_id)
    snapshot = {**forecast, "data_from": app.state.metadata.get("data_from"),
                "data_to": app.state.metadata.get("data_to"),
                "model_version": app.state.metadata.get("model_version")}
    return app.state.repository.add_feedback(snapshot, **payload.model_dump())


@app.post("/api/v1/maintenance-requests", status_code=201)
def create_maintenance_request(payload: MaintenanceCreate) -> dict:
    forecast = next(
        (item for item in app.state.forecasts if item["channel_id"] == payload.channel_id), None
    )
    if not forecast:
        raise HTTPException(status_code=404, detail="Прогноз для канала не найден")
    item, created = app.state.repository.create(forecast)
    return {"created": created, "item": item}


@app.post("/api/v1/maintenance-requests/auto")
def auto_create_maintenance_requests() -> dict:
    created_items = []
    for forecast in app.state.forecasts:
        if forecast["risk_level"] not in {"critical", "high"}:
            continue
        item, created = app.state.repository.create(forecast)
        if created:
            created_items.append(item)
    return {"created": len(created_items), "items": created_items}


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
