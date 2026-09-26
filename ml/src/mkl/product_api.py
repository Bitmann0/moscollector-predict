"""FastAPI-приложение контракта C1 «ML → backend».

    uvicorn mkl.product_api:app --host 0.0.0.0 --port 8001

Режим задаёт ML_MODE:
- stub (по умолчанию) — ответы строит mkl.product_stub из синтетического
  справочника; данные, модели и тяжёлые модули mkl не нужны;
- real — пилотные модели и недельная очередь читаются из бандла.

Тяжёлые модули (mkl.service, serve, train, polars) импортируются только
внутри _real_*, поэтому образ в режиме stub стартует без бандла.

Все расчёты идут под одним threading.Lock: backend вызывает ML строго
последовательно, а замок защищает от параллельных запросов снаружи (ML1-04).
"""
import datetime as dt
import logging
import os
import pickle
import threading
from collections.abc import Callable
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


PILOT_HEADS = ("A_link", "D")


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
    """Metadata cache invalidates when atomic retraining replaces the artifact."""
    with path.open("rb") as stream:
        art = pickle.load(stream)
    return {key: art.get(key) for key in ("metadata", "threshold", "saved_at")}


def _pilot_artifact(head: str, day: dt.date) -> dict:
    from . import serve

    path = serve.model_path(head)
    art = _artifact_info(path, path.stat().st_mtime_ns)
    serve.validate_pilot_artifact(
        head, art, day,
        max_lag_days=int(serve.load_heads()[head].get("max_model_lag_days", 14)))
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
        for head in PILOT_HEADS:
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
    from . import service, serve, workorders
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
        total = 0
        if (PATHS.interim / "channels.parquet").exists():
            if catalog is None:
                import polars as pl
                catalog = pl.read_parquet(PATHS.interim / "channels.parquet")
            population = (catalog.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
                          if head == "D" else catalog)
            total = population["ch"].n_unique()
        reason = ("только каналы оборудования" if head == "D" else None)
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
                # train_latest.py saves nextafter(1.0) when no threshold meets the
                # precision gate: the head is silent by design, not merely empty.
                threshold_feasible=art["threshold"] <= 1.0)
            scored = len({a.address.channel for a in got if a.address.channel is not None})
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
