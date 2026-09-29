"""Точка входа API. Живое.

Все маршруты C2 живут под /api/v1. Собранный фронт (backend/app/static, стадия
node корневого Dockerfile) отдаётся с корня: любой путь вне /api получает
index.html, и маршрутизацию берёт на себя React Router.
"""
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse

from . import directory, xml_api
from .audit import AuditMiddleware
from .config import get_settings
from .limits import BodyLimitMiddleware
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
    # Неверная настройка LDAP останавливает старт api, а не отвечает 503 на каждый вход.
    directory.check_config(get_settings())
    app = FastAPI(
        title="Москоллектор — сервис диспетчера",
        version="0.2.0",
        description=(
            "## Интерактивная документация\n\n"
            "В Swagger UI можно выполнить любой запрос через **Try it out**. Для браузерной "
            "сессии сначала вызовите `POST /api/v1/auth/login`: защищённые запросы далее используют "
            "HttpOnly cookie автоматически. Интеграции вместо cookie передают `X-API-Key`.\n\n"
            "## Правила данных\n\n"
            "Все даты без зоны трактуются как Europe/Moscow, в БД время хранится в UTC. "
            "`unknown` — отсутствие достаточных наблюдений, а не отрицательный исход. "
            "Прогнозы являются ручными рекомендациями и не создают подтверждённую заявку автоматически."
        ),
        openapi_tags=[
            {"name": "auth", "description": "Вход, выход и определение роли текущей сессии."},
            {"name": "system", "description": "Готовность API и ML-контура."},
            {"name": "forecasts", "description": "Журнал, карточки, решения и исходы рекомендаций."},
            {"name": "ingest", "description": "Идемпотентная загрузка СМВУ и журнала ОДС."},
            {"name": "reference", "description": "Справочники объектов, каналов и причин решений."},
        ],
    )
    # XML (ТЗ §7) — самый внутренний слой: предел тела и аудит видят XML-запрос как есть.
    app.add_middleware(xml_api.XmlMiddleware)
    app.add_middleware(AuditMiddleware)
    # Добавлен последним — значит, самый внешний: лишнее тело отсекается до разбора и аудита.
    app.add_middleware(BodyLimitMiddleware)
    for module in ROUTERS:
        app.include_router(module.router, prefix=API_PREFIX)

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version,
                             description=app.description, routes=app.routes,
                             tags=app.openapi_tags)
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes["cookieAuth"] = {"type": "apiKey", "in": "cookie", "name": "mk_session",
                                 "description": "Устанавливается POST /api/v1/auth/login"}
        schemes["apiKeyAuth"] = {"type": "apiKey", "in": "header", "name": "X-API-Key",
                                 "description": "Ключ машинной интеграции"}
        public = {f"{API_PREFIX}/health", f"{API_PREFIX}/auth/login",
                  f"{API_PREFIX}/auth/logout"}
        for path, operations in schema["paths"].items():
            if path in public:
                continue
            for method, operation in operations.items():
                if method.lower() in {"get", "post", "put", "patch", "delete"}:
                    operation["security"] = [{"cookieAuth": []}, {"apiKeyAuth": []}]
                    operation.setdefault("responses", {}).setdefault(
                        "401", {"description": "Сессия отсутствует, истекла или недействительна"})
                    operation["responses"].setdefault(
                        "403", {"description": "У роли нет требуемого права"})
        xml_api.add_openapi(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

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
