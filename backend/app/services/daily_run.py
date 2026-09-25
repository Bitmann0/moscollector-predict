"""Дневной цикл: /score и недельная очередь → прогнозы, версии, журнал выданного, заявки, SSE.

Живое, минимум. Точки входа: POST /api/v1/admin/run-daily и
`python -m app.services.daily_run --asof YYYY-MM-DD`.

ЗАГЛУШКА части — владелец PM-09 (C1, C2).
Заменить: правило «замена дня» — повторный расчёт того же asof должен заменять записи
issued_log за (голова, asof), сейчас он только дописывает недостающие; шаг «факт» —
POST /outcomes для прогнозов с valid_to ≤ demo_now и запись outcomes.outcome_auto;
прелоад окна (scripts/preload_demo.py).
Контракт: run_daily(db, asof, ml) -> RunDailyOut не меняется; тест
tests/test_daily_run.py должен остаться зелёным.

Что делает сейчас:
1. Запрос к ML: головы A_link и D, журнал выданного из issued_log за 7 дней до asof
   (день asof не входит), history_complete_from = asof − 7.
2. ML недоступен или ответ нарушает C1 → прогон пишется со статусом головы error и
   текстом ошибки, ответ 200 без прогнозов.
3. Upsert forecasts по alert_id (по recommendation_id у недельной очереди), строка
   forecast_versions на каждый прогон, issued_log для строк в бюджете, черновики
   work_orders по order_id.
4. По понедельникам — недельная очередь guard_weekly тем же прогоном.
5. После commit: alert.new на каждый новый прогноз в бюджете, run.finished на прогон.
"""
import argparse
import logging
from datetime import date, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..db import session_factory
from ..schemas.misc import HeadRunResult, RunDailyOut
from ..schemas.ml import (
    AlertOut,
    IssuedEntry,
    ScoreRequest,
    ScoreResponse,
    WeeklyResponse,
    WorkOrderOut,
)
from .helpers import assume_msk, msk_midnight, now_utc, to_db
from .ml_client import MlClient, MlUnavailable, get_ml_client
from .notifications import publish_safe

log = logging.getLogger(__name__)

PILOT_HEADS = ["A_link", "D"]
WEEKLY_HEAD = "guard_weekly"
HISTORY_DAYS = 7
WEEKLY_HORIZON_HOURS = 168
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

    def alert(self, alert: AlertOut, source: str) -> models.Forecast | None:
        scenario = vocab.scenario_by_head(alert.head)
        if scenario is None:
            log.warning("alert %s: голова %s вне словаря scenario", alert.alert_id, alert.head)
            return None
        fields = {
            "id": alert.alert_id, "kind": "alert", "scenario": scenario["code"],
            "head": alert.head, "asof": alert.asof,
            "valid_from": to_db(assume_msk(alert.valid_from)),
            "valid_to": to_db(assume_msk(alert.valid_to)),
            "horizon_hours": alert.horizon_hours, "score_type": "probability",
            "risk": alert.risk, "priority_score": None, "rank": alert.rank,
            "in_budget": alert.in_budget, "obj_id": alert.address.obj,
            "channel_id": alert.address.channel,
            "address": alert.address.model_dump(mode="json"),
            "factors": [f.model_dump(mode="json") for f in alert.factors],
            "extra": {"title": alert.title, "status_note": alert.status_note,
                      "model_version": alert.model_version,
                      "above_threshold": alert.above_threshold},
            "data_status": alert.status, "case_key": alert.case_key, "source": source,
        }
        return self.upsert(fields, alert.model_dump(mode="json", exclude={"factors"}))

    def weekly(self, resp: WeeklyResponse) -> int:
        source = _source(resp.source)
        valid_from = to_db(msk_midnight(resp.valid_from))
        valid_to = to_db(msk_midnight(resp.valid_to))
        for p in resp.priorities:
            fields = {
                "id": p.recommendation_id, "kind": "weekly_recommendation",
                "scenario": "guard_weekly", "head": WEEKLY_HEAD, "asof": resp.asof,
                "valid_from": valid_from, "valid_to": valid_to,
                "horizon_hours": WEEKLY_HORIZON_HOURS, "score_type": "relative_priority",
                "risk": None, "priority_score": p.priority_score, "rank": p.rank,
                "in_budget": True, "obj_id": p.obj, "channel_id": None,
                "address": {"obj": p.obj, "obj_name": p.obj_name,
                            "obj_parent_name": p.obj_parent_name,
                            "address_known": p.address_known},
                "factors": [],
                "extra": {"evidence": p.evidence, "recent_alarm_days_7": p.recent_alarm_days_7,
                          "recent_alarm_days_30": p.recent_alarm_days_30,
                          "guard_state_age_days": p.guard_state_age_days,
                          "model_version": resp.model_version},
                "data_status": "ok", "case_key": p.case_key, "source": source,
            }
            self.upsert(fields, p.model_dump(mode="json"))
        return len(resp.priorities)


def _log_issued(db: Session, asof: date, alerts: list[AlertOut]) -> None:
    """Журнал выданного: все строки в бюджете. Уже записанное за (голова, asof) не трогаем."""
    existing = {(r.head, r.entity_key) for r in db.scalars(
        select(models.IssuedLog).where(models.IssuedLog.asof == asof))}
    for alert in alerts:
        key = entity_key(alert.address.channel, alert.address.obj)
        if not alert.in_budget or key is None or (alert.head, key) in existing:
            continue
        existing.add((alert.head, key))
        db.add(models.IssuedLog(head=alert.head, asof=asof, entity_key=key,
                                forecast_id=alert.alert_id))


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
        else:  # статус и автора не трогаем: заявку могли уже взять в работу
            for key, value in fields.items():
                setattr(row, key, value)
        saved += 1
    return saved


def _head_state(status, in_budget: int) -> dict:
    return {**status.model_dump(mode="json"), "alerts_in_budget": in_budget}


def _error_state(detail: str) -> dict:
    return {"result_status": "error", "detail": detail}


def _score(db: Session, saver: _Saver, asof: date, ml: MlClient,
           heads: dict, raw: dict) -> int:
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
    in_budget: dict[str, int] = {}
    for alert in resp.alerts:
        if saver.alert(alert, source) is not None and alert.in_budget:
            in_budget[alert.head] = in_budget.get(alert.head, 0) + 1
    _log_issued(db, asof, resp.alerts)
    orders = _save_work_orders(db, resp.work_orders, source)
    for head, status in resp.heads.items():
        heads[head] = _head_state(status, in_budget.get(head, 0))
    raw["score"] = resp.model_dump(mode="json", exclude={"alerts", "work_orders"})
    return orders


def _weekly(saver: _Saver, asof: date, ml: MlClient, heads: dict, raw: dict) -> None:
    try:
        resp = ml.weekly(asof)
    except ML_ERRORS as exc:
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        heads[WEEKLY_HEAD] = _error_state(detail)
        raw["weekly_error"] = detail
        return
    n = saver.weekly(resp)
    heads[WEEKLY_HEAD] = {"result_status": resp.result_status, "detail": None,
                          "alerts_in_budget": n}
    raw["weekly"] = resp.model_dump(mode="json", exclude={"priorities"})


def _alert_new(row: models.Forecast) -> None:
    scenario = vocab.scenario(row.scenario)
    obj = (row.address or {}).get("obj_name") or row.obj_id or "адрес неизвестен"
    publish_safe("alert.new", {
        "id": row.id, "kind": row.kind, "scenario": row.scenario, "head": row.head,
        "asof": row.asof.isoformat(), "rank": row.rank, "risk": row.risk,
        "priority_score": row.priority_score, "obj_id": row.obj_id, "obj_name": obj,
        "channel_id": row.channel_id, "source": row.source,
    }, severity="warning", title=f"{scenario['title']}: {obj}")


def run_daily(db: Session, asof: date, ml: MlClient) -> RunDailyOut:
    run = models.ForecastRun(asof=asof, kind="daily", started_at=now_utc(), heads={}, raw={})
    db.add(run)
    db.flush()
    saver = _Saver(db, run)
    heads: dict[str, dict] = {}
    raw: dict = {}
    orders = _score(db, saver, asof, ml, heads, raw)
    if asof.weekday() == 0:
        _weekly(saver, asof, ml, heads, raw)
    run.heads = heads
    run.raw = raw
    run.finished_at = now_utc()
    db.commit()
    for row in saver.new:
        _alert_new(row)
    failed = [h for h, s in heads.items() if s["result_status"] == "error"]
    publish_safe("run.finished", {"run_id": run.id, "asof": asof.isoformat(),
                                  "heads": {h: s["result_status"] for h, s in heads.items()}},
                 severity="warning" if failed else "info",
                 title=f"Расчёт за {asof:%d.%m.%Y}" + (" с ошибкой" if failed else ""))
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
    args = parser.parse_args(argv)
    with session_factory()() as db:
        out = run_daily(db, args.asof, get_ml_client())
    print(out.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
