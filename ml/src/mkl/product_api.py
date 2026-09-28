"""FastAPI-приложение контракта C1 «ML → backend».

    uvicorn mkl.product_api:app --host 0.0.0.0 --port 8001

Режим задаёт ML_MODE:
- stub (по умолчанию) — ответы строит mkl.product_stub из синтетического
  справочника; данные, модели и тяжёлые модули mkl не нужны;
- real — модели A_link, B, E, правило D и недельная очередь читаются из бандла.

Тяжёлые модули (mkl.service, serve, train, polars) импортируются только
внутри _real_*, поэтому образ в режиме stub стартует без бандла.

Все расчёты идут под одним threading.Lock: backend вызывает ML строго
последовательно, а замок защищает от параллельных запросов снаружи (ML1-04).

В режиме real при старте в отдельном потоке идёт прогрев (_real_warmup, ML2-10):
/health отвечает сразу, /ready пишет в detail, что прогрев не закончен.
"""
import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from . import product_stub
from .product_contract import (
    SCHEMA_VERSION,
    AlertOut,
    CoverageOut,
    DirectionHead,
    DirectionItem,
    Health,
    HeadStatus,
    OutcomeQuery,
    OutcomeResult,
    ReadyResponse,
    ScoreRequest,
    ScoreResponse,
    WeeklyResponse,
    WorkOrderOut,
)

API_PREFIX = "/api/v1"
MODES = ("stub", "real")
log = logging.getLogger(__name__)


PILOT_HEADS = ("A_link", "D", "B", "E")
# Пояснение к охвату: по каким сущностям голова вообще отвечает.
COVERAGE_REASON = {
    "D": "только каналы оборудования",
    "B": "объекты по участкам 10 пикетов; риск считается на участок",
    "E": "только объекты с насосами: «Затоплен» приходит с каналов насосов",
}


def _last_feature_day() -> dt.date | None:
    """Projection pushdown keeps readiness cheap even for the full feature store."""
    import polars as pl
    from .config import PATHS

    path = PATHS.features / "sensor.parquet"
    if not path.exists():
        return None
    return pl.scan_parquet(path).select(pl.col("day").max()).collect().item()


def _has_feature_day(day: dt.date) -> bool:
    import polars as pl
    from .config import PATHS

    path = PATHS.features / "sensor.parquet"
    return path.exists() and pl.scan_parquet(path).filter(
        pl.col("day") == day).select(pl.len()).collect().item() > 0


@lru_cache(maxsize=12)
def _artifact_info(path: Path, mtime_ns: int) -> dict:
    """Metadata cache keyed by the chosen file: each dated artifact has its own
    entry, and atomic retraining of any of them changes mtime_ns."""
    from . import serve

    art = serve.load_artifact(path)
    return {key: art.get(key) for key in ("metadata", "threshold", "saved_at")}


def _pilot_artifact(head: str, day: dt.date) -> dict:
    from . import rule_head, serve

    cfg = serve.load_heads()[head]
    if rule_head.is_rule(cfg):
        art = rule_head.artifact(head, cfg, day)
    else:
        # Same choice as serve.score, so threshold_end and model_version in the
        # response describe the file that produced the risk.
        path = serve.artifact_path(head, day, cfg)
        art = _artifact_info(path, path.stat().st_mtime_ns)
    serve.validate_pilot_artifact(head, art, day, max_lag_days=serve.max_lag_days(cfg))
    return art


def _real_ready(asof: dt.date | None) -> ReadyResponse:
    """Check the requested historical day, rather than claiming stale data is live."""
    from .config import PATHS

    last_day = None
    try:
        last_day = _last_feature_day()
        if last_day is None or not (PATHS.interim / "channels.parquet").exists():
            return ReadyResponse(status="missing_data", asof=asof,
                                 data_last_day=last_day, source="live",
                                 detail="нет признаков каналов или справочника")
        day = asof or last_day
        if day > last_day:
            return ReadyResponse(status="future_source", asof=asof,
                                 data_last_day=last_day, source="live",
                                 detail=f"данные заканчиваются {last_day}")
        if not _has_feature_day(day):
            return ReadyResponse(status="missing_data", asof=asof,
                                 data_last_day=last_day, source="live",
                                 detail=f"нет признаков за {day}")
        if asof is None and (dt.date.today() - last_day).days > 2:
            return ReadyResponse(status="stale_source", asof=None,
                                 data_last_day=last_day, source="live",
                                 detail="новые данные не поступали более двух суток")
        from . import rule_head, serve
        heads = serve.load_heads()
        for head in PILOT_HEADS:
            if rule_head.is_rule(heads[head]):
                # Порог правила считается при /score: здесь только наличие входов,
                # иначе /ready не уложится в таймаут backend.
                if not rule_head.inputs_ready():
                    raise FileNotFoundError(f"{head}: нет панели исходов для порога правила")
                continue
            _pilot_artifact(head, day)
        return ReadyResponse(status="ready", asof=asof,
                             data_last_day=last_day, source="live")
    except FileNotFoundError as exc:
        return ReadyResponse(status="missing_data", asof=asof,
                             data_last_day=last_day, source="live", detail=str(exc))
    except ValueError as exc:
        return ReadyResponse(status="stale", asof=asof,
                             data_last_day=last_day, source="live", detail=str(exc))
    except Exception as exc:
        log.exception("ML readiness failed")
        return ReadyResponse(status="error", asof=asof,
                             data_last_day=last_day, source="live", detail=str(exc))


def _real_directions() -> list[DirectionItem]:
    from . import contract, serve

    items = []
    for head, cfg in serve.load_heads().items():
        if cfg.get("product_status") == "pilot":
            items.append(DirectionItem(
                direction=cfg["direction"],
                title=contract.DIRECTIONS[cfg["direction"]],
                heads=[DirectionHead(head=head, title=cfg["title"],
                                     horizon_hours=int(cfg["horizon_days"]) * 24,
                                     budget_per_day=int(cfg["budget_per_day"]))]))
    items.append(DirectionItem(
        direction="unauthorised_access",
        title=contract.DIRECTIONS["unauthorised_access"],
        heads=[DirectionHead(head="guard_weekly",
                             title="Недельная очередь осмотра охранного контура",
                             horizon_hours=168)]))
    return items


def _real_score(req: ScoreRequest) -> ScoreResponse:
    """Score each head independently; a failed head never produces fake alerts."""
    from . import rule_head, service, serve, workorders
    from .config import EQUIPMENT_STYPES, PATHS

    statuses: dict[str, HeadStatus] = {}
    alerts = []
    coverage = []
    last_day = _last_feature_day()
    has_day = bool(last_day and req.asof <= last_day and
                   _has_feature_day(req.asof))
    catalog = None
    for head in dict.fromkeys(req.heads):
        cfg = serve.load_heads()[head]
        # B и E отвечают по объектам (участкам объекта), A_link и D — по каналам.
        by_object = cfg["entity"][0] == "obj"
        total = 0
        if (PATHS.interim / "channels.parquet").exists():
            if catalog is None:
                import polars as pl
                catalog = pl.read_parquet(PATHS.interim / "channels.parquet")
            if cfg.get("population") == "pump_objects":
                total = len(serve.pump_objects())
            elif by_object:
                total = catalog["obj"].drop_nulls().n_unique()
            else:
                population = (catalog.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
                              if head == "D" else catalog)
                total = population["ch"].n_unique()
        reason = COVERAGE_REASON.get(head)
        try:
            if not has_day:
                raise ValueError(f"no feature rows for requested day {req.asof}")
            art = _pilot_artifact(head, req.asof)
            history = req.issued_histories.get(head)
            issued = ([(e.channel if e.channel is not None else e.obj, e.sent_day)
                       for e in history] if history is not None else None)
            got = service.alerts_for_head(
                head, req.asof, with_factors=req.with_factors,
                issued_history=issued,
                history_complete_from=req.history_complete_from)
            alerts.extend(got)
            statuses[head] = HeadStatus(
                result_status="ok" if any(a.in_budget for a in got) else "empty_valid",
                model_version=art.get("saved_at"),
                threshold_end=dt.date.fromisoformat(art["metadata"]["threshold_end"]),
                model_lag_days=(req.asof - dt.date.fromisoformat(
                    art["metadata"]["threshold_end"])).days,
                # train_latest.py saves nextafter(1.0), a rule head saves inf when
                # no threshold meets the precision gate: silent by design.
                threshold_feasible=rule_head.feasible(art))
            scored = (len({a.address.obj for a in got if a.address.obj is not None})
                      if by_object else
                      len({a.address.channel for a in got if a.address.channel is not None}))
        except Exception as exc:
            detail = str(exc)
            status = ("no_data" if "no feature rows" in detail or
                      "sensor.parquet" in detail else
                      "stale" if "pilot lag" in detail or
                      "threshold window" in detail or
                      "pilot target changed" in detail else "error")
            if status == "error":
                log.exception("ML head %s failed on %s", head, req.asof)
            statuses[head] = HeadStatus(result_status=status, detail=detail)
            scored = 0
        coverage.append(CoverageOut(
            head=head, direction=cfg["direction"], entities_total=total,
            entities_scored=scored, reason=reason,
            fraction=round(scored / total, 4) if total else 0.0))

    alerts.sort(key=lambda a: (-a.risk, a.head, a.rank))
    orders = workorders.build(alerts)
    names = {a.address.obj: a.address for a in alerts if a.address.obj}
    return ScoreResponse(
        asof=req.asof, source="live", heads=statuses,
        data_snapshot={"data_last_day": last_day.isoformat() if last_day else None},
        alerts=[AlertOut.model_validate(a.to_dict()) for a in alerts],
        coverage=coverage,
        work_orders=[WorkOrderOut.model_validate({
            **order.to_dict(),
            "obj_name": names[order.obj].obj_name if order.obj in names else None,
            "obj_parent_name": (names[order.obj].obj_parent_name
                                if order.obj in names else None),
        }) for order in orders])


def _real_weekly(asof: dt.date) -> WeeklyResponse:
    """Return a valid empty state when the historical cache has no data."""
    from . import guard_weekly

    if asof.weekday() != 0:
        raise ValueError("недельная очередь считается по понедельникам")
    try:
        result = guard_weekly.weekly_inspections(asof)
    except (FileNotFoundError, ValueError) as exc:
        return WeeklyResponse(
            asof=asof, valid_from=asof + dt.timedelta(days=2),
            valid_to=asof + dt.timedelta(days=9),
            next_run=asof + dt.timedelta(days=7),
            result_status=("stale" if "stale" in str(exc) else "no_data"),
            data_snapshot={"detail": str(exc)},
            source="live")
    return WeeklyResponse.model_validate({**result, "source": "live"})


def _real_outcomes(items: list[OutcomeQuery]) -> list[OutcomeResult]:
    from .outcomes import resolve

    return resolve(items)


def _real_warmup(lock: threading.Lock) -> tuple[dict[str, float], list[str]]:
    """Заполнить кэши, которые иначе заполняет первый /score после старта.

    Свежий процесс на стенде 28.09 тратил сверх обычного расчёта около 5 с:
    импорт mkl.service и соседей 1,7 с, исходы правила D через duckdb 2,9 с,
    порог правила на неделю 0,3 с, метаданные артефакта A_link 0,2 с,
    справочник адресов 0,1 с (docs/submission/perf/ml_warmup_0928.txt).

    Фичестор не читается. Процесс его не кэширует, а sensor.parquet не
    упорядочен по дню: в каждой из 36 групп строк есть все дни, и срез за любой
    день читает файл целиком — 3,7 с на каждом /score, первом и последующих.
    Предчтение через store.read_slice удлиняло прогрев с 5 до 8 с, и /score,
    пришедший сразу после healthy, делил с ним процессор: 9,81 с против 7,44 с
    у следующего.

    День — последний день фичестора: его запрашивает backend, в демо это
    DEMO_TODAY = 30.06. От дня зависит только порог правила D, он недельный.
    Артефакты A_link грузятся все, датированные и основной: какой из них
    выберет /score, зависит от дня расчёта.

    Шаги независимы: упавший пишется в журнал и в список ошибок, остальные
    выполняются. Замок берёт только шаг правила D, иначе параллельный /score
    собирал бы те же исходы вторым duckdb. serve.score здесь не вызывается:
    флаг serve._with_internals общий на процесс, и вызов вне замка испортил
    бы ответ параллельного /score.
    """
    start = time.perf_counter()
    from . import address, rule_head, serve, service, workorders  # noqa: F401

    timings: dict[str, float] = {"импорт": round(time.perf_counter() - start, 2)}
    failed: list[str] = []

    def step(name: str, fn: Callable) -> None:
        start = time.perf_counter()
        try:
            fn()
        except Exception as exc:
            log.exception("ML warmup step %r failed", name)
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            return
        timings[name] = round(time.perf_counter() - start, 2)

    def locked(fn: Callable, *args) -> Callable:
        def call():
            with lock:
                return fn(*args)
        return call

    heads = serve.load_heads()
    day = _last_feature_day()
    for head in PILOT_HEADS:
        cfg = heads[head]
        if rule_head.is_rule(cfg):
            if day is not None:
                step(f"{head}: порог правила", locked(rule_head.artifact, head, cfg, day))
            continue
        paths = [*serve.dated_model_paths(head).values(), serve.model_path(head)]
        for path in paths:
            if path.exists():
                step(path.name, lambda p=path: _artifact_info(p, p.stat().st_mtime_ns))
    step("справочник адресов", lambda: (
        address._by_channel(), address._by_object(),
        address._segment_has_picket(), address._objects_without_picket()))
    return timings, failed


def _warmup_note(state: dict) -> str | None:
    """Приписка к detail в /ready. Статус готовности прогрев не меняет:
    /score во время прогрева работает, только первый расчёт медленнее."""
    if state["status"] == "running":
        return "идёт прогрев моделей и данных, первый расчёт будет медленнее"
    if state["status"] == "failed":
        extra = f" и ещё {len(state['failed']) - 1}" if len(state["failed"]) > 1 else ""
        return (f"прогрев с ошибкой ({state['failed'][0]}{extra}), "
                "первый расчёт будет медленнее")
    return None


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

    # off — заглушке греть нечего; idle — real, но приложение запущено без lifespan
    # (TestClient без with); дальше running → done или failed.
    warmup = {"status": "off" if mode == "stub" else "idle", "failed": [],
              "seconds": None, "thread": None}

    def warm() -> None:
        start = time.perf_counter()
        try:
            timings, failed = _real_warmup(lock)
        except Exception as exc:
            log.exception("ML warmup failed, the first /score will be cold")
            timings, failed = {}, [f"{type(exc).__name__}: {exc}"]
        warmup["failed"] = failed
        warmup["seconds"] = round(time.perf_counter() - start, 1)
        warmup["status"] = "failed" if failed else "done"
        steps = ", ".join(f"{name} {sec} с" for name, sec in timings.items())
        print(f"ML: прогрев {'с ошибками ' if failed else ''}за {warmup['seconds']} с"
              f"{': ' + steps if steps else ''}", flush=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mode == "real":
            warmup["status"] = "running"
            # daemon: остановка контейнера не ждёт недогретый кэш.
            warmup["thread"] = threading.Thread(target=warm, name="ml-warmup", daemon=True)
            warmup["thread"].start()
        yield

    app = FastAPI(
        title="Москоллектор ML — контракт C1",
        version=SCHEMA_VERSION,
        description=("Прогнозы голов A_link, D, B и E, недельная охранная очередь и "
                     f"факт по выданному. Режим: {mode}."),
        lifespan=lifespan,
    )
    app.state.mode = mode
    app.state.warmup = warmup

    @app.get("/health", response_model=Health)
    def health() -> Health:
        """Живое: процесс отвечает, режим и версия схемы. Прогрева не ждёт."""
        return Health(mode=mode)

    @app.get("/ready", response_model=ReadyResponse)
    def ready(asof: dt.date | None = Query(None, description="сутки расчёта")) -> ReadyResponse:
        resp = run(product_stub.ready, _real_ready, asof, exclusive=False)
        # Нового статуса backend не примет: ReadyStatus в backend/app/schemas/ml.py —
        # закрытый Literal, ответ не пройдёт валидацию, и шапка покажет «ML недоступна».
        # Поэтому о прогреве сообщает detail.
        note = _warmup_note(warmup)
        if note:
            resp = resp.model_copy(update={
                "detail": "; ".join(d for d in (resp.detail, note) if d)})
        return resp

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
