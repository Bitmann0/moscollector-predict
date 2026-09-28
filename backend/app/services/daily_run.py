"""Дневной цикл: ML → прогнозы, версии, факты, журнал выдачи, заявки и уведомления.

Точки входа: POST /api/v1/admin/run-daily,
`python -m app.services.daily_run --asof YYYY-MM-DD` и scripts/preload_demo.py.

1. Запрос к ML: головы A_link и D, журнал выданного из issued_log за 7 дней до asof
   (день asof не входит), history_complete_from = asof − 7.
2. ML недоступен или ответ нарушает C1 → прогон пишется со статусом головы error и
   текстом ошибки, ответ 200 без прогнозов.
3. Upsert forecasts по alert_id (по recommendation_id у недельной очереди), строка
   forecast_versions на каждый прогон, issued_log для строк в бюджете, черновики
   work_orders по order_id.
4. По понедельникам — недельная очередь guard_weekly тем же прогоном и черновик
   «Проверка охранной сигнализации объекта» на каждую рекомендацию, срок — valid_to.
5. Созревшие прогнозы отправляются в ML `/outcomes`; автоматический факт хранится
   отдельно от ручного исхода.
6. После commit: alert.new на каждый новый прогноз в бюджете, run.finished на прогон.

Лимит показа (ML2-13, экран «Настройки», parameters.limit_by_head). В бюджете остаются
первые N рекомендаций ML по рангу у каждой головы, остальные сохраняются с
in_budget=false и extra.cut_by_limit=true: их не видно в журнале, по ним нет alert.new,
черновика заявки и запроса факта. N не выше проверенного: 20 у A_link, 3 у D, 4 у
недельной очереди (schemas/parameters.py, VERIFIED_LIMITS).

Журнал выданного пишется после лимита, то есть из показанного. От него зависит пауза
в 7 дней: ML не выдаёт повторно канал, выданный за 7 суток до asof. Если бы журнал брал
всю выдачу ML, рекомендации с ранга N+1 до 20 ушли бы на паузу, хотя диспетчер их не
видел, и риск по этим каналам молчал бы неделю. С журналом показанного такие каналы
завтра снова конкурируют за место в выдаче. Точность top-N при паузе по показанному
отдельно не замерялась: ML проверял top-20 с паузой по своей выдаче из 20.
Недельная очередь журнала выданного не читает: паузу в 14 суток ML восстанавливает
по своей выдаче из 4 объектов (ml/src/mkl/guard_weekly.py, replay). Объект, срезанный
лимитом, для ML всё равно выдан и две недели в очередь не вернётся.

weekly_only=True не вызывает /score: прогон kind=weekly_guard пишет только недельную
очередь и её черновики, журнал выданного не меняется. Так прелоад проходит понедельники
января–мая, не считая на них A_link и D.
"""
import argparse
import hashlib
import logging
import threading
from datetime import date, datetime, time, timedelta

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..db import session_factory
from ..schemas.misc import HeadRunResult, RunDailyOut
from ..schemas.ml import (
    AlertOut,
    IssuedEntry,
    OutcomeQuery,
    ScoreRequest,
    ScoreResponse,
    WeeklyResponse,
    WorkOrderOut,
)
from . import parameters
from .helpers import assume_msk, msk_midnight, now_utc, to_db
from .ml_client import MlClient, MlUnavailable, get_ml_client
from .notifications import publish_safe

log = logging.getLogger(__name__)

PILOT_HEADS = ["A_link", "D"]
WEEKLY_HEAD = "guard_weekly"
HISTORY_DAYS = 7
WEEKLY_HORIZON_HOURS = 168
WEEKLY_WORK_TYPE = "Проверка охранной сигнализации объекта"
WEEKLY_PRIORITY = "плановая"  # план обхода, не срочный выезд; в C3 это planned
ML_ERRORS = (MlUnavailable, ValidationError)


def _source(ml_source: str) -> str:
    return "stub" if ml_source == "stub" else "live"


def entity_key(channel: int | None, obj: str | None) -> str | None:
    if channel is not None:
        return f"channel:{channel}"
    if obj:
        return f"obj:{obj}"
    return None


def _issued_entry(key: str, day: date) -> IssuedEntry | None:
    kind, _, value = key.partition(":")
    if kind == "channel" and value.isdigit():
        return IssuedEntry(channel=int(value), sent_day=day)
    if kind == "obj" and value:
        return IssuedEntry(obj=value, sent_day=day)
    return None


def score_request(db: Session, asof: date) -> ScoreRequest:
    since = asof - timedelta(days=HISTORY_DAYS)
    rows = db.scalars(select(models.IssuedLog).where(
        models.IssuedLog.head.in_(PILOT_HEADS), models.IssuedLog.asof >= since,
        models.IssuedLog.asof < asof).order_by(models.IssuedLog.asof, models.IssuedLog.id))
    histories: dict[str, list[IssuedEntry]] = {head: [] for head in PILOT_HEADS}
    for row in rows:
        entry = _issued_entry(row.entity_key, row.asof)
        if entry is not None:
            histories[row.head].append(entry)
    return ScoreRequest(asof=asof, heads=PILOT_HEADS, issued_histories=histories,
                        history_complete_from=since)


class _Saver:
    """Запись одного прогона. Новые прогнозы копятся для SSE после commit."""

    def __init__(self, db: Session, run: models.ForecastRun) -> None:
        self.db = db
        self.run = run
        self.new: list[models.Forecast] = []
        self.upserted = 0

    def upsert(self, fields: dict, payload: dict) -> models.Forecast:
        row = self.db.get(models.Forecast, fields["id"])
        if row is None:
            row = models.Forecast(**fields, first_run_id=self.run.id, last_run_id=self.run.id)
            self.db.add(row)
            if row.in_budget:
                self.new.append(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            row.last_run_id = self.run.id
        self.db.flush()
        self.db.add(models.ForecastVersion(forecast_id=row.id, run_id=self.run.id,
                                           risk=row.risk, rank=row.rank,
                                           recorded_at=now_utc(), payload=payload))
        self.upserted += 1
        return row

    def alert(self, alert: AlertOut, source: str, *,
              cut: bool = False) -> models.Forecast | None:
        """cut — рекомендация в бюджете ML, но за лимитом показа: in_budget=false."""
        scenario = vocab.scenario_by_head(alert.head)
        if scenario is None:
            log.warning("alert %s: голова %s вне словаря scenario", alert.alert_id, alert.head)
            return None
        probability = scenario["score_type"] == "probability"
        fields = {
            "id": alert.alert_id, "kind": "alert", "scenario": scenario["code"],
            "head": alert.head, "asof": alert.asof,
            "valid_from": to_db(assume_msk(alert.valid_from)),
            "valid_to": to_db(assume_msk(alert.valid_to)),
            # Тип оценки — из словаря C3: у головы-правила D риск — значение
            # признака, а не вероятность (ml/reports/RULE_VS_MODEL_RESULT.md).
            "horizon_hours": alert.horizon_hours, "score_type": scenario["score_type"],
            "risk": alert.risk if probability else None,
            "priority_score": None if probability else alert.risk, "rank": alert.rank,
            "in_budget": alert.in_budget and not cut, "obj_id": alert.address.obj,
            "channel_id": alert.address.channel,
            "address": alert.address.model_dump(mode="json"),
            "factors": [f.model_dump(mode="json") for f in alert.factors],
            "extra": {"title": alert.title, "status_note": alert.status_note,
                      "model_version": alert.model_version,
                      "above_threshold": alert.above_threshold,
                      "maintenance_context": alert.maintenance_context,
                      **({"cut_by_limit": True} if cut else {})},
            "data_status": alert.status, "case_key": alert.case_key, "source": source,
        }
        return self.upsert(fields, alert.model_dump(mode="json", exclude={"factors"}))

    def weekly(self, resp: WeeklyResponse, kept: set[str]) -> int:
        """kept — recommendation_id в лимите показа; остальные сохраняются вне бюджета."""
        source = _source(resp.source)
        valid_from = to_db(msk_midnight(resp.valid_from))
        valid_to = to_db(msk_midnight(resp.valid_to))
        for p in resp.priorities:
            cut = p.recommendation_id not in kept
            fields = {
                "id": p.recommendation_id, "kind": "weekly_recommendation",
                "scenario": "guard_weekly", "head": WEEKLY_HEAD, "asof": resp.asof,
                "valid_from": valid_from, "valid_to": valid_to,
                "horizon_hours": WEEKLY_HORIZON_HOURS, "score_type": "relative_priority",
                "risk": None, "priority_score": p.priority_score, "rank": p.rank,
                "in_budget": not cut, "obj_id": p.obj, "channel_id": None,
                "address": {"obj": p.obj, "obj_name": p.obj_name,
                            "obj_parent_name": p.obj_parent_name,
                            "address_known": p.address_known},
                "factors": [],
                "extra": {"evidence": p.evidence, "recent_alarm_days_7": p.recent_alarm_days_7,
                          "recent_alarm_days_30": p.recent_alarm_days_30,
                          "guard_state_age_days": p.guard_state_age_days,
                          "model_version": resp.model_version,
                          **({"cut_by_limit": True} if cut else {})},
                "data_status": "ok", "case_key": p.case_key, "source": source,
            }
            self.upsert(fields, p.model_dump(mode="json"))
        return len(kept)


def cut_by_limit(alerts: list[AlertOut], limits: dict[str, int]) -> set[str]:
    """alert_id рекомендаций в бюджете ML за лимитом показа своей головы: ранг после N-го."""
    by_head: dict[str, list[AlertOut]] = {}
    for alert in alerts:
        if alert.in_budget:
            by_head.setdefault(alert.head, []).append(alert)
    cut: set[str] = set()
    for head, items in by_head.items():
        limit = limits.get(head)
        if limit is not None:
            cut.update(a.alert_id for a in sorted(items, key=lambda a: a.rank)[limit:])
    return cut


def _trim_orders(orders: list[WorkOrderOut], alerts: list[AlertOut],
                 cut: set[str]) -> list[WorkOrderOut]:
    """Черновики заявок без рекомендаций за лимитом. Заявка, в которой не осталось ни
    одной рекомендации, не создаётся; в остальных каналы, пикеты и число алертов
    пересчитываются по оставшимся. rationale ML не пересобирается."""
    if not cut:
        return orders
    by_id = {a.alert_id: a for a in alerts}
    out = []
    for order in orders:
        kept = [i for i in order.alert_ids if i not in cut]
        if not kept:
            continue
        if len(kept) == len(order.alert_ids):
            out.append(order)
            continue
        items = [by_id[i] for i in kept if i in by_id]
        out.append(order.model_copy(update={
            "alert_ids": kept,
            "case_keys": [a.case_key for a in items],
            "n_alerts": len(kept),
            "channels": sorted({a.address.channel for a in items
                                if a.address.channel is not None}),
            "pickets": sorted({a.address.picket for a in items
                               if a.address.picket is not None}),
            "max_risk": max((a.risk for a in items), default=order.max_risk),
        }))
    return out


def _log_issued(db: Session, asof: date, alerts: list[AlertOut], scored: set[str],
                cut: set[str] = frozenset()) -> None:
    """Журнал выданного — показанное диспетчеру: in_budget ML без срезанного лимитом (cut).
    Почему так — в docstring модуля, «Лимит показа».

    Повтор расчёта заменяет выдачу за день только у голов, которые посчитались.

    Голова с result_status=error алертов не вернула: если стереть её прежнюю выдачу,
    пропадёт пауза в 7 дней, и завтра она выдаст те же каналы повторно.
    """
    heads = [h for h in PILOT_HEADS if h in scored]
    db.execute(delete(models.IssuedLog).where(models.IssuedLog.asof == asof,
                                               models.IssuedLog.head.in_(heads)))
    existing: set[tuple[str, str]] = set()
    for alert in alerts:
        key = entity_key(alert.address.channel, alert.address.obj)
        if (not alert.in_budget or alert.alert_id in cut or alert.head not in scored
                or key is None or (alert.head, key) in existing):
            continue
        existing.add((alert.head, key))
        db.add(models.IssuedLog(head=alert.head, asof=asof, entity_key=key,
                                forecast_id=alert.alert_id))


def clear_issued_log(db: Session, date_from: date, date_to: date) -> int:
    """Стирает журнал выданного за asof из [date_from, date_to]. Прелоад зовёт это перед
    прогоном окна: иначе остаётся выдача головы, упавшей в прошлом прогоне."""
    deleted = db.execute(delete(models.IssuedLog).where(
        models.IssuedLog.asof >= date_from, models.IssuedLog.asof <= date_to)).rowcount
    db.commit()
    return deleted or 0


def _order_id(day: date, direction: str, obj: str | None) -> str:
    """Формула mkl.workorders._order_id: повтор расчёта дня обновляет тот же черновик."""
    key = f"{day.isoformat()}|{direction}|{obj}"
    return "WO-" + hashlib.sha256(key.encode()).hexdigest()[:12].upper()


def _weekly_orders(resp: WeeklyResponse) -> list[WorkOrderOut]:
    """Черновик на каждую рекомендацию недельной очереди.

    Ответ C1 недельной очереди заявок не несёт, в отличие от /score, поэтому черновик
    собирает backend. Срок — valid_to рекомендации: D+9, 00:00 МСК, граница окна.
    """
    scenario = vocab.scenario(WEEKLY_HEAD)
    due = datetime.combine(resp.valid_to, time(0))  # без таймзоны: МСК (C1)
    return [WorkOrderOut(
        order_id=_order_id(resp.asof, scenario["direction"], p.obj), created_for=resp.asof,
        due_by=due, priority=WEEKLY_PRIORITY, work_type=WEEKLY_WORK_TYPE,
        direction=scenario["direction"], direction_title=scenario["title"], obj=p.obj,
        obj_name=p.obj_name, obj_parent_name=p.obj_parent_name,
        alert_ids=[p.recommendation_id], case_keys=[p.case_key], n_alerts=1,
        max_risk=p.priority_score,
        rationale=[f"охранная тревога в {p.recent_alarm_days_7} из 7 последних суток",
                   f"суток с охранной тревогой за 30: {p.recent_alarm_days_30}"],
    ) for p in resp.priorities]


def _scenario_of_order(db: Session, order: WorkOrderOut) -> str | None:
    by_direction = next((s["code"] for s in vocab.load()["scenario"]
                         if s["direction"] == order.direction), None)
    if by_direction is not None:
        return by_direction
    for alert_id in order.alert_ids:
        row = db.get(models.Forecast, alert_id)
        if row is not None:
            return row.scenario
    return None


def _save_work_orders(db: Session, orders: list[WorkOrderOut], source: str) -> int:
    saved = 0
    for order in orders:
        scenario = _scenario_of_order(db, order)
        if scenario is None:
            log.warning("work order %s: направление %s вне словаря", order.order_id,
                        order.direction)
            continue
        fields = {
            "forecast_ids": list(order.alert_ids), "scenario": scenario, "obj_id": order.obj,
            "priority": vocab.priority_from_ml(order.priority), "work_type": order.work_type,
            "due_by": to_db(assume_msk(order.due_by)), "rationale": list(order.rationale),
            "pickets": list(order.pickets), "channels": list(order.channels), "source": source,
        }
        row = db.get(models.WorkOrder, order.order_id)
        if row is None:
            now = now_utc()
            db.add(models.WorkOrder(id=order.order_id, status="draft", created_by="system",
                                    created_at=now, **fields))
            db.flush()
            db.add(models.WorkOrderHistory(order_id=order.order_id, from_status=None,
                                           to_status="draft", author="system",
                                           reason="черновик из дневного расчёта", at=now))
        elif row.status == "draft":  # подтверждённую заявку расчёт менять не вправе
            for key, value in fields.items():
                setattr(row, key, value)
        saved += 1
    return saved


def _head_state(status, in_budget: int) -> dict:
    return {**status.model_dump(mode="json"), "alerts_in_budget": in_budget}


def _error_state(detail: str) -> dict:
    return {"result_status": "error", "detail": detail}


def _score(db: Session, saver: _Saver, asof: date, ml: MlClient,
           heads: dict, raw: dict, limits: dict[str, int]) -> int:
    request = score_request(db, asof)
    try:
        resp: ScoreResponse = ml.score(request)
    except ML_ERRORS as exc:
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        for head in request.heads:
            heads[head] = _error_state(detail)
        raw["score_error"] = detail
        return 0
    source = _source(resp.source)
    cut = cut_by_limit(resp.alerts, limits)
    in_budget: dict[str, int] = {}
    for alert in resp.alerts:
        shown = alert.in_budget and alert.alert_id not in cut
        if saver.alert(alert, source, cut=alert.alert_id in cut) is not None and shown:
            in_budget[alert.head] = in_budget.get(alert.head, 0) + 1
    scored = {h for h, status in resp.heads.items() if status.result_status != "error"}
    _log_issued(db, asof, resp.alerts, scored, cut)
    orders = _save_work_orders(db, _trim_orders(resp.work_orders, resp.alerts, cut), source)
    if cut:
        raw["cut_by_limit"] = sorted(cut)
    for head, status in resp.heads.items():
        heads[head] = _head_state(status, in_budget.get(head, 0))
    raw["score"] = resp.model_dump(mode="json", exclude={"alerts", "work_orders"})
    return orders


def _weekly(saver: _Saver, asof: date, ml: MlClient, heads: dict, raw: dict,
            limit: int | None) -> int:
    try:
        resp = ml.weekly(asof)
    except ML_ERRORS as exc:
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        heads[WEEKLY_HEAD] = _error_state(detail)
        raw["weekly_error"] = detail
        return 0
    ranked = sorted(resp.priorities, key=lambda p: p.rank)
    kept = {p.recommendation_id for p in (ranked if limit is None else ranked[:limit])}
    n = saver.weekly(resp, kept)
    heads[WEEKLY_HEAD] = {"result_status": resp.result_status, "detail": None,
                          "alerts_in_budget": n}
    raw["weekly"] = resp.model_dump(mode="json", exclude={"priorities"})
    shown = resp.model_copy(update={"priorities": [p for p in ranked
                                                   if p.recommendation_id in kept]})
    return _save_work_orders(saver.db, _weekly_orders(shown), _source(resp.source))


def _refresh_outcomes(db: Session, asof: date, ml: MlClient, raw: dict) -> None:
    """Запрашивает факт для выданных прогнозов с закрытым окном, у которых его ещё нет.

    Факт считается по меткам бандла и после закрытия окна не меняется. Без отсева уже
    размеченных прелоад за 29 дней отправлял бы в ML всё накопленное каждый день.
    """
    cutoff = to_db(msk_midnight(asof + timedelta(days=1)))
    forecasts = list(db.scalars(
        select(models.Forecast)
        .outerjoin(models.Outcome, models.Outcome.forecast_id == models.Forecast.id)
        .where(models.Forecast.in_budget.is_(True), models.Forecast.valid_to <= cutoff,
               models.Outcome.outcome_auto.is_(None))))
    if not forecasts:
        return
    queries = [OutcomeQuery(id=row.id, kind=row.kind, head=row.head, channel=row.channel_id,
                            obj=row.obj_id, asof=row.asof) for row in forecasts]
    try:
        results = ml.outcomes(queries)
    except ML_ERRORS as exc:
        raw["outcomes_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        return
    by_id = {item.id: item.outcome for item in results}
    for forecast in forecasts:
        if forecast.id not in by_id:
            continue
        outcome = db.get(models.Outcome, forecast.id)
        if outcome is None:
            outcome = models.Outcome(forecast_id=forecast.id, updated_at=now_utc(),
                                     source=forecast.source)
            db.add(outcome)
        outcome.outcome_auto = by_id[forecast.id]
        outcome.updated_at = now_utc()
    raw["outcomes_refreshed"] = len(by_id)


def _alert_new(db: Session, row: models.Forecast) -> None:
    scenario = vocab.scenario(row.scenario)
    obj = (row.address or {}).get("obj_name") or row.obj_id or "адрес неизвестен"
    publish_safe("alert.new", {
        "id": row.id, "kind": row.kind, "scenario": row.scenario, "head": row.head,
        "asof": row.asof.isoformat(), "rank": row.rank, "risk": row.risk,
        "priority_score": row.priority_score, "obj_id": row.obj_id, "obj_name": obj,
        "channel_id": row.channel_id, "source": row.source,
    }, severity="warning", title=f"{scenario['title']}: {obj}", db=db)


_RUN_LOCK = threading.Lock()


def run_daily(db: Session, asof: date, ml: MlClient, *,
              weekly_only: bool = False) -> RunDailyOut:
    """Дневной цикл. Прогоны сериализуются: два параллельных run-daily на один asof
    иначе вставляют одинаковые прогнозы и второй падает на уникальном ключе.
    Замок процесса достаточен: api запускается одним процессом uvicorn."""
    if weekly_only and asof.weekday() != 0:
        raise ValueError(f"weekly_only: {asof.isoformat()} — не понедельник")
    with _RUN_LOCK:
        return _run_daily(db, asof, ml, weekly_only)


def _run_daily(db: Session, asof: date, ml: MlClient, weekly_only: bool) -> RunDailyOut:
    run = models.ForecastRun(asof=asof, kind="weekly_guard" if weekly_only else "daily",
                             started_at=now_utc(), heads={}, raw={})
    db.add(run)
    db.flush()
    saver = _Saver(db, run)
    heads: dict[str, dict] = {}
    limits = parameters.current(db).limit_by_head()
    raw: dict = {"limits": limits}
    _refresh_outcomes(db, asof, ml, raw)
    orders = 0 if weekly_only else _score(db, saver, asof, ml, heads, raw, limits)
    if asof.weekday() == 0:
        orders += _weekly(saver, asof, ml, heads, raw, limits.get(WEEKLY_HEAD))
    run.heads = heads
    run.raw = raw
    run.finished_at = now_utc()
    db.commit()
    for row in saver.new:
        _alert_new(db, row)
    failed = [h for h, s in heads.items() if s["result_status"] == "error"]
    publish_safe("run.finished", {"run_id": run.id, "asof": asof.isoformat(),
                                  "heads": {h: s["result_status"] for h, s in heads.items()}},
                 severity="warning" if failed else "info",
                 title=f"Расчёт за {asof:%d.%m.%Y}" + (" с ошибкой" if failed else ""), db=db)
    return RunDailyOut(
        run_id=run.id, asof=asof,
        heads={h: HeadRunResult(result_status=s["result_status"],
                                alerts_in_budget=s.get("alerts_in_budget", 0),
                                detail=s.get("detail")) for h, s in heads.items()},
        forecasts_upserted=saver.upserted, work_orders_upserted=orders)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.daily_run",
                                     description="Дневной расчёт: ML /score → прогнозы в БД")
    parser.add_argument("--asof", required=True, type=date.fromisoformat,
                        help="день расчёта YYYY-MM-DD")
    parser.add_argument("--weekly-only", action="store_true",
                        help="только недельная очередь; asof — понедельник")
    args = parser.parse_args(argv)
    with session_factory()() as db:
        out = run_daily(db, args.asof, get_ml_client(), weekly_only=args.weekly_only)
    print(out.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
