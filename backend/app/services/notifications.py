"""Уведомления и SSE-брокер.

Брокер — живое: in-process рассылка событий подписчикам /api/v1/stream.
Публиковать можно из любого потока (дневной цикл работает в threadpool).
Хранение уведомлений и их список — ЗАГЛУШКА, владелец BE-08 (C2).
Заменить: запись в таблицу notifications при publish(), выборку и отметку «прочитано».
Сейчас список читает таблицу notifications, но в неё никто не пишет: лента пуста.
Контракт: сигнатуры не меняются; tests/test_endpoints_shape.py должен остаться зелёным.
"""
import asyncio
import logging
import threading
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.common import Page
from ..schemas.misc import NotificationItem
from ..security import CurrentUser
from .helpers import count, from_db, page_of

log = logging.getLogger(__name__)


class Broker:
    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(loop, q) for loop, q in self._subs if q is not queue]

    def publish(self, kind: str, payload: dict, *, severity: str = "info",
                title: str = "") -> dict:
        event = {"kind": kind, "severity": severity, "title": title,
                 "ts": datetime.now(UTC).isoformat(), "payload": payload}
        with self._lock:
            subs = list(self._subs)
        for loop, queue in subs:
            loop.call_soon_threadsafe(_put_nowait, queue, event)
        return event


def _put_nowait(queue: asyncio.Queue, event: dict) -> None:
    if not queue.full():
        queue.put_nowait(event)


broker = Broker()


def publish_safe(kind: str, payload: dict, *, severity: str = "info", title: str = "") -> None:
    """publish() для фоновых путей: сбой рассылки не должен откатывать уже записанное.

    Подписчик, чей цикл событий закрыт (оборванный поток SSE), даёт RuntimeError в
    call_soon_threadsafe — прогноз уже в БД, поэтому ошибку только пишем в лог.
    """
    try:
        broker.publish(kind, payload, severity=severity, title=title)
    except RuntimeError:
        log.exception("SSE publish failed: %s", kind)


def _item(row: models.Notification, user: CurrentUser) -> NotificationItem:
    return NotificationItem(id=row.id, ts=from_db(row.ts), kind=row.kind,
                            severity=row.severity, title=row.title, payload=row.payload or {},
                            read=user.login in (row.read_by or []))


def list_notifications(db: Session, user: CurrentUser, page: int,
                       page_size: int) -> Page[NotificationItem]:
    stmt = select(models.Notification)
    total = count(db, stmt)
    rows = db.scalars(stmt.order_by(models.Notification.ts.desc(), models.Notification.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return page_of(NotificationItem, [_item(r, user) for r in rows], total, page, page_size)


def mark_read(db: Session, notification_id: int, user: CurrentUser) -> None:
    row = db.get(models.Notification, notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="notification_not_found")
    if user.login not in (row.read_by or []):
        row.read_by = [*(row.read_by or []), user.login]  # новый список: JSON без мутаций
        db.commit()
