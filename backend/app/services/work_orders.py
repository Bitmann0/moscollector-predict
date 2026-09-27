"""Заявки: список, карточка, черновик из прогнозов, смена статуса.

ЗАГЛУШКА — владелец BE-06 (C2, C3).
Заменить: TODO BE-06 — в transition проверку графа статусов
(vocabularies.json: work_order_transitions), права на целевой статус
(work_order_transition_perm), 409 {detail, code} при expected_status ≠ текущему и
публикацию workorder.changed; в create — приоритет и вид работ по сценарию вместо
«плановая» и общего текста.
Контракт: list_orders, get, create и transition не меняются; тест
tests/test_endpoints_shape.py должен остаться зелёным.

Сейчас: список и карточка читают БД с фильтрами status, priority, scenario; create
делает черновик с id "WO-" + sha256 от отсортированных forecast_ids (повтор с теми же
прогнозами возвращает ту же заявку); transition ставит любой статус и пишет историю.
"""
import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.work_orders import (
    HistoryItem,
    WorkOrderCard,
    WorkOrderCreate,
    WorkOrderItem,
    WorkOrderTransition,
)
from ..security import CurrentUser
from .helpers import Refs, count, from_db, now_utc, page_of, to_db

MANUAL_PRIORITY = "planned"


def _item_fields(row: models.WorkOrder, refs: Refs) -> dict:
    return {"id": row.id, "scenario": row.scenario, "object": refs.object_ref(row.obj_id),
            "priority": row.priority, "work_type": row.work_type,
            "due_by": from_db(row.due_by), "status": row.status,
            "forecast_ids": list(row.forecast_ids or []), "created_by": row.created_by,
            "created_at": from_db(row.created_at), "source": row.source}


def list_orders(db: Session, *, status: str | None, priority: str | None,
                scenario: str | None, page: int, page_size: int) -> Page[WorkOrderItem]:
    stmt = select(models.WorkOrder)
    for column, value in ((models.WorkOrder.status, status),
                          (models.WorkOrder.priority, priority),
                          (models.WorkOrder.scenario, scenario)):
        if value is not None:
            stmt = stmt.where(column == value)
    total = count(db, stmt)
    rows = db.scalars(stmt.order_by(models.WorkOrder.created_at.desc(), models.WorkOrder.id)
                      .offset((page - 1) * page_size).limit(page_size))
    refs = Refs(db)
    items = [WorkOrderItem(**_item_fields(r, refs)) for r in rows]
    return page_of(WorkOrderItem, items, total, page, page_size)


def get(db: Session, order_id: str) -> WorkOrderCard | None:
    row = db.get(models.WorkOrder, order_id)
    if row is None:
        return None
    history = db.scalars(select(models.WorkOrderHistory)
                         .where(models.WorkOrderHistory.order_id == order_id)
                         .order_by(models.WorkOrderHistory.at, models.WorkOrderHistory.id))
    return WorkOrderCard(
        **_item_fields(row, Refs(db)),
        rationale=list(row.rationale or []), pickets=list(row.pickets or []),
        channels=list(row.channels or []),
        history=[HistoryItem(from_status=h.from_status, to_status=h.to_status,
                             author=h.author, reason=h.reason, at=from_db(h.at))
                 for h in history])


def order_id_for(forecast_ids: list[str]) -> str:
    key = "|".join(sorted(set(forecast_ids)))
    return "WO-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def create(db: Session, body: WorkOrderCreate, user: CurrentUser) -> WorkOrderCard:
    forecasts = []
    for forecast_id in dict.fromkeys(body.forecast_ids):
        row = db.get(models.Forecast, forecast_id)
        if row is None:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        forecasts.append(row)
    if len({f.scenario for f in forecasts}) != 1 or len({f.obj_id for f in forecasts}) != 1:
        raise HTTPException(status_code=422, detail="work_order_requires_one_scenario_and_object")
    order_id = order_id_for(body.forecast_ids)
    if db.get(models.WorkOrder, order_id) is None:
        first = forecasts[0]
        now = now_utc()
        pickets = sorted({(f.address or {}).get("picket") for f in forecasts} - {None})
        db.add(models.WorkOrder(
            id=order_id, forecast_ids=[f.id for f in forecasts], scenario=first.scenario,
            obj_id=first.obj_id, priority=MANUAL_PRIORITY,
            work_type=f"Проверка по прогнозу: {vocab.scenario(first.scenario)['title']}",
            due_by=max(to_db(from_db(f.valid_to)) for f in forecasts), status="draft",
            rationale=[f"Прогноз {f.id} от {f.asof:%d.%m.%Y}, ранг {f.rank}" for f in forecasts],
            pickets=pickets,
            channels=sorted({f.channel_id for f in forecasts} - {None}),
            created_by=user.login, created_at=now, source="live"))
        db.flush()
        db.add(models.WorkOrderHistory(order_id=order_id, from_status=None, to_status="draft",
                                       author=user.login, reason="черновик вручную", at=now))
        db.commit()
    return get(db, order_id)


def transition(db: Session, order_id: str, body: WorkOrderTransition,
               user: CurrentUser) -> WorkOrderCard:
    row = db.get(models.WorkOrder, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="work_order_not_found")
    if row.status != body.expected_status:
        raise HTTPException(status_code=409, detail={"code": "status_conflict",
                                                     "current_status": row.status})
    allowed = vocab.load()["work_order_transitions"].get(row.status, [])
    if body.status not in allowed:
        raise HTTPException(status_code=422, detail="work_order_transition_not_allowed")
    permission = vocab.load()["work_order_transition_perm"].get(body.status)
    if permission and permission not in user.perms:
        raise HTTPException(status_code=403, detail="forbidden")
    db.add(models.WorkOrderHistory(order_id=order_id, from_status=row.status,
                                   to_status=body.status, author=user.login,
                                   reason=body.reason, at=now_utc()))
    row.status = body.status
    db.commit()
    return get(db, order_id)
