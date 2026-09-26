"""FastAPI-приложение контракта C1 «ML → backend».

    uvicorn mkl.product_api:app --host 0.0.0.0 --port 8001

Режим задаёт ML_MODE:
- stub (по умолчанию) — ответы строит mkl.product_stub из синтетического
  справочника; данные, модели и тяжёлые модули mkl не нужны;
- real — каждый эндпоинт, кроме /health, вызывает _real_<имя>(). Пока это
  заглушки с NotImplementedError и ID задачи, эндпоинт отвечает 501.

Тяжёлые модули (mkl.service, serve, train, polars) импортируются только
внутри _real_*, поэтому образ в режиме stub стартует без бандла.

Все расчёты идут под одним threading.Lock: backend вызывает ML строго
последовательно, а замок защищает от параллельных запросов снаружи (ML1-04).
"""
import datetime as dt
import os
import threading
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query

from . import product_stub
from .product_contract import (
    SCHEMA_VERSION,
    DirectionItem,
    Health,
    OutcomeQuery,
    OutcomeResult,
    ReadyResponse,
    ScoreRequest,
    ScoreResponse,
    WeeklyResponse,
)

API_PREFIX = "/api/v1"
MODES = ("stub", "real")


def _real_ready(asof: dt.date | None) -> ReadyResponse:
    """ЗАГЛУШКА — владелец ML1-03 (C1).
    Заменить: готовность данных бандла и артефактов A_link и D на asof;
    mkl.guard_weekly.readiness() даёт часть недельной очереди. Импорт mkl — здесь.
    Контракт: ReadyResponse с source="live"; тест tests/test_product_api.py должен
    остаться зелёным.
    """
    raise NotImplementedError("ML1-03: /ready в real-режиме не реализован")


def _real_directions() -> list[DirectionItem]:
    """ЗАГЛУШКА — владелец ML1-03 (C1).
    Заменить: пилотные головы из serve.load_heads() (product_status: pilot) и
    недельная очередь, как product_stub.directions().
    Контракт: list[DirectionItem]; тест tests/test_product_api.py должен остаться зелёным.
    """
    raise NotImplementedError("ML1-03: /api/v1/directions в real-режиме не реализован")


def _real_score(req: ScoreRequest) -> ScoreResponse:
    """ЗАГЛУШКА — владелец ML1-03 (C1); устойчивость — ML1-04; артефакт по asof — ML1-05b.
    Заменить: каждая голова — отдельный вызов mkl.service.alerts_for_head в своём
    try (ошибка A_link не гасит D), статус головы, threshold_end, model_lag_days,
    threshold_feasible; coverage пилотных голов (у D — знаменатель по оборудованию);
    workorders.build с obj_name и obj_parent_name. Импорт mkl.service — здесь.
    Контракт: ScoreRequest → ScoreResponse с source="live"; тест
    tests/test_product_api.py должен остаться зелёным.
    """
    raise NotImplementedError(
        "ML1-03: /api/v1/score в real-режиме не реализован (ML1-04, ML1-05b)")


def _real_weekly(asof: dt.date) -> WeeklyResponse:
    """ЗАГЛУШКА — владелец ML1-04 (C1).
    Заменить: mkl.guard_weekly.weekly_inspections(asof); день без данных → 200
    no_data вместо 409. Импорт mkl.guard_weekly — здесь.
    Контракт: WeeklyResponse с source="live"; тест tests/test_product_api.py должен
    остаться зелёным.
    """
    raise NotImplementedError(
        "ML1-04: /api/v1/guard-weekly-inspections в real-режиме не реализован")


def _real_outcomes(items: list[OutcomeQuery]) -> list[OutcomeResult]:
    from .outcomes import resolve

    return resolve(items)


def create_app(mode: str | None = None) -> FastAPI:
    """Приложение C1. mode=None — из ML_MODE, по умолчанию stub."""
    mode = mode or os.environ.get("ML_MODE", "stub")
    if mode not in MODES:
        raise ValueError(f"ML_MODE={mode!r}: допустимо {', '.join(MODES)}")
    lock = threading.Lock()

    def run(stub_fn: Callable, real_fn: Callable, *args, exclusive: bool = True):
        """exclusive=False — для лёгких проверок вроде /ready: они не должны ждать
        минутный расчёт /score, иначе backend сочтёт ML недоступным."""
        fn = real_fn if mode == "real" else stub_fn
        try:
            if not exclusive:
                return fn(*args)
            with lock:
                return fn(*args)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    app = FastAPI(
        title="Москоллектор ML — контракт C1",
        version=SCHEMA_VERSION,
        description=("Прогнозы пилотных голов A_link и D, недельная охранная очередь и "
                     f"факт по выданному. Режим: {mode}."),
    )
    app.state.mode = mode

    @app.get("/health", response_model=Health)
    def health() -> Health:
        """Живое: процесс отвечает, режим и версия схемы."""
        return Health(mode=mode)

    @app.get("/ready", response_model=ReadyResponse)
    def ready(asof: dt.date | None = Query(None, description="сутки расчёта")) -> ReadyResponse:
        return run(product_stub.ready, _real_ready, asof, exclusive=False)

    @app.get(f"{API_PREFIX}/directions", response_model=list[DirectionItem])
    def directions() -> list[DirectionItem]:
        return run(product_stub.directions, _real_directions)

    @app.post(f"{API_PREFIX}/score", response_model=ScoreResponse)
    def score(req: ScoreRequest) -> ScoreResponse:
        return run(product_stub.score, _real_score, req)

    @app.get(f"{API_PREFIX}/guard-weekly-inspections", response_model=WeeklyResponse)
    def guard_weekly_inspections(
        asof: dt.date = Query(..., description="понедельник расчёта"),
    ) -> WeeklyResponse:
        try:
            return run(product_stub.weekly, _real_weekly, asof)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(f"{API_PREFIX}/outcomes", response_model=list[OutcomeResult])
    def outcomes(items: list[OutcomeQuery]) -> list[OutcomeResult]:
        return run(product_stub.outcomes, _real_outcomes, items)

    return app


app = create_app()
