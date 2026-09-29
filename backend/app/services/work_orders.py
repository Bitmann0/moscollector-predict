"""Заявки: фильтры, идемпотентный черновик, история и строгий граф статусов."""
import hashlib

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.work_orders import (
    ChecklistOut,
    HistoryItem,
    WorkOrderCard,
    WorkOrderCreate,
    WorkOrderItem,
    WorkOrderTransition,
)
from ..security import CurrentUser
from .helpers import Refs, count, from_db, now_utc, page_of, to_db
from .notifications import publish_recorded, record
from .phase_channels import CHECKLISTS

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


def checklist(channel_ids: list[int], refs: Refs) -> list[ChecklistOut]:
    """Перечни проверок по типам датчиков заявки, в порядке каналов.

    Перечень считается при чтении по справочнику каналов, а не хранится в заявке: так он
    есть и у ручных заявок, и у черновиков, записанных до его появления. Цена — правка
    перечня видна и в уже подтверждённых заявках.
    """
    groups: dict[str, list] = {}
    for channel_id in channel_ids:
        ref = refs.channel_ref(channel_id)
        if ref is not None and ref.sensor_type in CHECKLISTS:
            groups.setdefault(ref.sensor_type, []).append(ref)
    out = []
    for sensor_type, channels in groups.items():
        spec = CHECKLISTS[sensor_type]
        out.append(ChecklistOut(equipment=spec.equipment, channels=channels,
                                items=list(spec.items), note=spec.note, basis=spec.basis))
    return out


def get(db: Session, order_id: str) -> WorkOrderCard | None:
    row = db.get(models.WorkOrder, order_id)
    if row is None:
        return None
    history = db.scalars(select(models.WorkOrderHistory)
                         .where(models.WorkOrderHistory.order_id == order_id)
                         .order_by(models.WorkOrderHistory.at, models.WorkOrderHistory.id))
    channels = list(row.channels or [])
    refs = Refs(db, set(channels))
    return WorkOrderCard(
        **_item_fields(row, refs),
        rationale=list(row.rationale or []), pickets=list(row.pickets or []),
        channels=channels, checklist=checklist(channels, refs),
        history=[HistoryItem(from_status=h.from_status, to_status=h.to_status,
                             author=h.author, reason=h.reason, at=from_db(h.at))
                 for h in history])


def order_id_for(forecast_ids: list[str], cycle: int = 1) -> str:
    """Ключ ручной заявки: набор прогнозов и номер цикла. У первого цикла суффикса нет,
    поэтому ключи уже заведённых заявок не меняются."""
    key = "|".join(sorted(set(forecast_ids)))
    base = "WO-" + hashlib.sha256(key.encode()).hexdigest()[:12]
    return base if cycle == 1 else f"{base}-{cycle}"


def _open_statuses() -> list[str]:
    """Незакрытые статусы — те, из которых граф C3 ещё ведёт дальше."""
    return [s for s, targets in vocab.load()["work_order_transitions"].items() if targets]


def _active_order_for(db: Session, forecast_ids: set[str]) -> str | None:
    """Самая ранняя незакрытая заявка, куда входит хотя бы один из прогнозов.

    Прогнозы заявки лежат JSON-списком, частичный уникальный индекс по нему одинаково
    на SQLite и PostgreSQL не построить, поэтому проверка здесь. Заявок в демо десятки:
    читаем все, как forecasts.work_order_ids.
    """
    for order_id, stored_ids in db.execute(
            select(models.WorkOrder.id, models.WorkOrder.forecast_ids)
            .where(models.WorkOrder.status.in_(_open_statuses()))
            .order_by(models.WorkOrder.created_at, models.WorkOrder.id)):
        if forecast_ids & set(stored_ids or []):
            return order_id
    return None


def lock_forecasts(db: Session, forecast_ids: set[str]) -> None:
    """Один прогноз — одна активная заявка и при разных наборах forecast_ids.

    PostgreSQL advisory transaction lock общий для ручного создания и дневного
    расчёта. Порядок ключей фиксирован, чтобы два пересекающихся набора не
    заблокировали друг друга навсегда. SQLite в тестах сериализует писателей.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    for forecast_id in sorted(forecast_ids):
        digest = hashlib.sha256(forecast_id.encode()).digest()[:8]
        key = int.from_bytes(digest, "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def create(db: Session, body: WorkOrderCreate, user: CurrentUser) -> WorkOrderCard:
    forecasts = []
    for forecast_id in dict.fromkeys(body.forecast_ids):
        row = db.get(models.Forecast, forecast_id)
        if row is None:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        forecasts.append(row)
    if len({f.scenario for f in forecasts}) != 1 or len({f.obj_id for f in forecasts}) != 1:
        raise HTTPException(status_code=422, detail="work_order_requires_one_scenario_and_object")
    lock_forecasts(db, {f.id for f in forecasts})
    # На прогноз одна активная заявка: если дневной расчёт или другой диспетчер её уже
    # завёл, отдаём её. Закрытые (completed, cancelled) здесь не учитываются.
    active_id = _active_order_for(db, {f.id for f in forecasts})
    if active_id is not None:
        return get(db, active_id)
    # Прежние заявки на этот набор, если есть, закрыты: новая получает следующий номер
    # цикла. Номер не хранится, а находится перебором, так что два одновременных запроса
    # выберут один ключ и разойдутся на вставке.
    cycle = 1
    while (taken := db.get(models.WorkOrder, order_id_for(body.forecast_ids, cycle))) is not None:
        if taken.status in _open_statuses():
            # Параллельный запрос завёл эту заявку уже после проверки выше.
            return get(db, taken.id)
        cycle += 1
    order_id = order_id_for(body.forecast_ids, cycle)
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
    try:
        db.flush()
    except IntegrityError:
        # Параллельный запрос вставил ту же заявку между проверкой и вставкой: ключ
        # детерминирован по прогнозам и циклу, значит это она и есть. Ловим на flush:
        # INSERT уходит здесь, до commit дело не доходит.
        db.rollback()
        existing = get(db, order_id)
        if existing is None:
            raise
        return existing
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
    # Проверки выше читали строку до записи: между ними статус мог сменить другой запрос.
    # Поэтому сравнение и запись — один UPDATE. На PostgreSQL второй из двух одновременных
    # UPDATE ждёт блокировку строки, после commit первого перепроверяет WHERE и меняет
    # 0 строк.
    moved = db.execute(update(models.WorkOrder)
                       .where(models.WorkOrder.id == order_id,
                              models.WorkOrder.status == body.expected_status)
                       .values(status=body.status))
    if moved.rowcount != 1:
        db.rollback()
        current = db.scalar(select(models.WorkOrder.status)
                            .where(models.WorkOrder.id == order_id))
        raise HTTPException(status_code=409, detail={"code": "status_conflict",
                                                     "current_status": current})
    db.add(models.WorkOrderHistory(order_id=order_id, from_status=body.expected_status,
                                   to_status=body.status, author=user.login,
                                   reason=body.reason, at=now_utc()))
    notification = record(db, "workorder.changed", {
        "id": row.id, "from_status": body.expected_status, "to_status": body.status,
    }, title=f"Заявка {row.id}")
    db.commit()
    publish_recorded(notification)
    return get(db, order_id)
