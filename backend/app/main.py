from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from .config import Settings
from .database import MaintenanceRepository
from .history import history_channel, history_day, history_summary
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
        forecasts, metadata = analyze(
            settings.data_dir,
            settings.forecast_hours,
            time_zone=settings.time_zone,
            window_hours=settings.analysis_window_hours,
        )
    except DataValidationError as error:
        forecasts, metadata = [], {"model_status": "data_unavailable"}
        app.state.load_error = str(error)
    app.state.forecasts = [forecast.to_dict() for forecast in forecasts]
    app.state.metadata = metadata
    app.state.repository = MaintenanceRepository(settings.database_path)
    yield


app = FastAPI(
    title="МосКоллектор Predict API",
    description="Эвристическое ранжирование каналов СМВУ для проверки. Прогноз не подключён.",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/api/v1/health")
def health() -> dict:
    metadata = app.state.metadata
    data_to = metadata.get("data_to")
    age = (
        (datetime.now(UTC) - datetime.fromisoformat(data_to)).total_seconds() / 3600
        if data_to
        else None
    )
    stale = age is None or age > settings.max_data_age_hours or age < 0
    ready = not bool(app.state.load_error) and (settings.mode == "historical" or not stale)
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "detail": app.state.load_error
        or (
            "Данные устарели или датированы будущим" if settings.mode == "live" and stale else None
        ),
        **metadata,
        "mode": settings.mode,
        "data_age_hours": round(age, 3) if age is not None else None,
        "data_stale": stale,
        "max_data_age_hours": settings.max_data_age_hours,
    }


@app.get("/api/v1/ready")
def readiness() -> JSONResponse:
    state = health()
    return JSONResponse(state, status_code=200 if state["ready"] else 503)


def require_ready() -> None:
    state = health()
    if not state["ready"]:
        raise HTTPException(status_code=409, detail=state["detail"])


@app.get("/api/v1/summary")
def summary() -> dict:
    forecasts = app.state.forecasts
    counts = {level: 0 for level in ("critical", "high", "medium", "low")}
    for forecast in forecasts:
        counts[forecast["risk_level"]] += 1
    return {
        **health(),
        "risk_counts": counts,
        "active_forecasts": counts["critical"] + counts["high"] + counts["medium"],
        "max_risk_score": max((item["risk_score"] for item in forecasts), default=0),
        "maintenance_requests": len(app.state.repository.list()),
        "forecast_horizon_hours": None,
    }


@app.get("/api/v1/forecasts")
def list_forecasts(
    risk_level: Annotated[Literal["critical", "high", "medium", "low"] | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 250,
    offset: Annotated[int, Query(ge=0)] = 0,
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
                [
                    str(item["channel_id"]),
                    item["sensor_name"],
                    item["sensor_type"],
                    item["location"],
                ]
            ).casefold()
        ]
    return {"total": len(items), "items": items[offset : offset + limit]}


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
    require_ready()
    forecast = get_forecast(channel_id)
    snapshot = {
        **forecast,
        "assessment_mode": settings.mode,
        "data_from": app.state.metadata.get("data_from"),
        "data_to": app.state.metadata.get("data_to"),
        "model_version": app.state.metadata.get("model_version"),
    }
    return app.state.repository.add_feedback(snapshot, **payload.model_dump())


@app.post("/api/v1/maintenance-requests", status_code=201)
def create_maintenance_request(payload: MaintenanceCreate) -> dict:
    require_ready()
    forecast = next(
        (item for item in app.state.forecasts if item["channel_id"] == payload.channel_id), None
    )
    if not forecast:
        raise HTTPException(status_code=404, detail="Прогноз для канала не найден")
    try:
        item, created = app.state.repository.create({**forecast, "assessment_mode": settings.mode})
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"created": created, "item": item}


@app.post("/api/v1/maintenance-requests/auto")
def auto_create_maintenance_requests() -> dict:
    require_ready()
    # Validate all existing drafts before writing any, avoiding a partly applied batch.
    candidates = [f for f in app.state.forecasts if f["risk_level"] in {"critical", "high"}]
    candidate_ids = {f["channel_id"] for f in candidates}
    if any(
        item["channel_id"] in candidate_ids and item["assessment_mode"] != settings.mode
        for item in app.state.repository.list()
    ):
        raise HTTPException(
            status_code=409, detail="Есть черновики другого режима; проверьте журнал"
        )
    created_items = []
    for forecast in candidates:
        item, created = app.state.repository.create({**forecast, "assessment_mode": settings.mode})
        if created:
            created_items.append(item)
    return {"created": len(created_items), "items": created_items}


STATIC_DIR = Path(__file__).parent / "static"


def read_history(function, *args):
    try:
        return function(settings.history_features_path, *args)
    except (ValueError, OSError, duckdb.Error) as error:
        raise HTTPException(
            status_code=503, detail="Годовая история не подключена или недоступна"
        ) from error


@app.get("/api/v1/history/summary")
def get_history_summary() -> dict:
    return read_history(history_summary)


@app.get("/api/v1/history/days")
def get_history_day(
    day: date,
    channel: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    return read_history(history_day, day, channel, limit, offset)


@app.get("/api/v1/history/channels/{channel_id}")
def get_history_channel(
    channel_id: int,
    end: date,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> dict:
    return read_history(history_channel, channel_id, end, days)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
