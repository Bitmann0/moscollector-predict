"""Точка входа API. Живое.

Все маршруты C2 живут под /api/v1. Собранный фронт (backend/app/static, стадия
node корневого Dockerfile) отдаётся с корня: любой путь вне /api получает
index.html, и маршрутизацию берёт на себя React Router.
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .audit import AuditMiddleware
from .config import get_settings
from .routers import (
    admin,
    audit,
    auth,
    dashboard,
    events,
    export,
    forecasts,
    geo,
    ingest,
    notifications,
    quality,
    reference,
    settings,
    stream,
    system,
    work_orders,
)

API_PREFIX = "/api/v1"
ROUTERS = [auth, system, dashboard, forecasts, reference, work_orders, events, ingest, geo,
           quality, stream, notifications, export, audit, settings, admin]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Москоллектор — сервис диспетчера",
        version="0.2.0",
        description=(
            "Контракт C2 плана команды: backend → frontend. "
            "Прогнозы считает ML-сервис (C1); интерфейс читает только этот API."
        ),
    )
    app.add_middleware(AuditMiddleware)
    for module in ROUTERS:
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok"}

    static_dir = get_settings().static_dir

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not_found")
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and static_dir.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = static_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="frontend_not_built")
        return FileResponse(index)

    return app


app = create_app()
