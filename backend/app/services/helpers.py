"""Общее для сервисов: время, страницы, ссылки на объект и канал. Живое.

Время. Даты без таймзоны в C1 означают Europe/Moscow. В БД пишем UTC: SQLite при
записи отбрасывает таймзону, и только единое правило «в БД всегда UTC» даёт один и
тот же ответ на SQLite (тесты) и PostgreSQL (compose, CI). Наружу отдаём +03:00.
Смещение фиксированное: переходов на летнее время в Москве нет с 2014 года, а
zoneinfo на Windows без пакета tzdata не работает.
"""
import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..schemas.common import ChannelRef, ObjectRef, Page
from .phase_channels import decode_channel

MSK = timezone(timedelta(hours=3), "MSK")


def now_utc() -> datetime:
    return datetime.now(UTC)


def assume_msk(value: datetime) -> datetime:
    """Время из ML и от клиента: без таймзоны — это Москва (правило C1)."""
    return value.replace(tzinfo=MSK) if value.tzinfo is None else value


def to_db(value: datetime) -> datetime:
    """Перед записью в БД и перед сравнением в запросе: всегда UTC."""
    if value.tzinfo is None:
        raise ValueError("время без таймзоны: сначала assume_msk() или msk_midnight()")
    return value.astimezone(UTC)


def from_db(value: datetime | None) -> datetime | None:
    """Прочитанное из БД — наружу в +03:00. Без таймзоны (SQLite) — это UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK)


def msk_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), MSK)


def page_of[T](model: type[T], items: list[T], total: int, page: int,
               page_size: int) -> Page[T]:
    return Page[model](items=items, total=total, page=page, page_size=page_size)


def count(db: Session, stmt) -> int:
    return db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0


def open_forecast_clauses(today: date) -> tuple:
    """Открытый прогноз: в бюджете, без решения, окно не кончилось к полуночи demo_today.

    Одно правило на дашборд и схему сети, чтобы число открытых на двух экранах совпадало.
    """
    return (models.Forecast.in_budget.is_(True),
            models.Forecast.id.not_in(select(models.Decision.forecast_id)),
            models.Forecast.valid_to >= to_db(msk_midnight(today)))


@lru_cache
def synthetic_reference() -> dict:
    """contracts/synthetic_reference.json: 1 район → 2 комплекса → 6 объектов → 30 каналов."""
    path = get_settings().contracts_dir / "synthetic_reference.json"
    return json.loads(path.read_text(encoding="utf-8"))


def picket_label(picket: float | None) -> str | None:
    return None if picket is None else f"ПК {picket:g}"


class Refs:
    """Справочник объектов целиком и нужные каналы — для сборки ObjectRef и ChannelRef.

    Объектов в реальном справочнике около сотни, поэтому они читаются целиком;
    каналов 11 485, поэтому читаются только запрошенные.
    """

    def __init__(self, db: Session, channel_ids: set[int] | None = None) -> None:
        self.objects = {o.id: o for o in db.scalars(select(models.RefObject))}
        ids = {c for c in (channel_ids or set()) if c is not None}
        self.channels = ({c.id: c for c in db.scalars(
            select(models.RefChannel).where(models.RefChannel.id.in_(ids)))} if ids else {})

    def object_ref(self, obj_id: str | None, address: dict | None = None) -> ObjectRef:
        address = address or {}
        obj_id = obj_id or address.get("obj")
        row = self.objects.get(obj_id) if obj_id else None
        complex_id = address.get("obj_parent") or (row.parent_id if row else None)
        parent = self.objects.get(complex_id) if complex_id else None
        return ObjectRef(
            id=obj_id,
            name=address.get("obj_name") or (row.name if row else None),
            complex_id=complex_id,
            complex_name=address.get("obj_parent_name") or (parent.name if parent else None),
            kind_ru=address.get("obj_kind_ru"),
        )

    def channel_ref(self, channel_id: int | None,
                    address: dict | None = None) -> ChannelRef | None:
        address = address or {}
        channel_id = channel_id if channel_id is not None else address.get("channel")
        if channel_id is None:
            return None
        row = self.channels.get(channel_id)
        picket = address.get("picket")
        if picket is None and row is not None:
            picket = row.picket
        name = address.get("sensor_name") or (row.name if row else None)
        sensor_type = address.get("sensor_type") or (row.sensor_type if row else None)
        return ChannelRef(
            id=channel_id, name=name, sensor_type=sensor_type,
            picket_label=address.get("picket_label") or picket_label(picket),
            name_decoded=decode_channel(sensor_type, name),
        )
