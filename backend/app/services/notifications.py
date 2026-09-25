"""Уведомления и SSE-брокер.

Брокер — живое: in-process рассылка событий подписчикам /api/v1/stream.
Публиковать можно из любого потока (дневной цикл работает в threadpool).
Хранение уведомлений и их список — ЗАГЛУШКА, владелец BE-08 (C2).
Заменить: запись в таблицу notifications при publish(), выборку и отметку «прочитано».
Контракт: сигнатуры не меняются; tests/test_endpoints_shape.py должен остаться зелёным.
"""
import asyncio
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.misc import NotificationItem
from ..security import CurrentUser


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


def list_notifications(db: Session, user: CurrentUser, page: int,
                       page_size: int) -> Page[NotificationItem]:
    raise NotImplementedError("каркас: задача 3")


def mark_read(db: Session, notification_id: int, user: CurrentUser) -> None:
    raise NotImplementedError("каркас: задача 3")
