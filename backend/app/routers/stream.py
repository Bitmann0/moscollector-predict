"""SSE: /api/v1/stream. Живое.

Сессию БД держим только на время проверки входа: соединение живёт долго.
Вход проверяется при подключении и затем раз в HEARTBEAT_S: после выхода поток,
открытый по этой cookie или по её копии, закрывается, а не идёт по отозванной сессии
до обрыва. Переподключение EventSource получит 401.
"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..db import session_factory
from ..security import authenticate
from ..services.notifications import broker

router = APIRouter(tags=["stream"])

HEARTBEAT_S = 15


def _frame(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/stream", response_class=StreamingResponse)
async def stream(request: Request) -> StreamingResponse:
    def check() -> None:
        with session_factory()() as db:
            user = authenticate(request, db)
        if "view" not in user.perms:  # integration и прочие машинные роли — без ленты
            raise HTTPException(status_code=403, detail="forbidden")

    await asyncio.to_thread(check)
    queue = broker.subscribe()

    async def events():
        try:
            yield "retry: 5000\n\n"
            yield _frame("hello", {})
            recheck_at = time.monotonic() + HEARTBEAT_S
            while not await request.is_disconnected():
                # По времени, а не по ping: в воспроизведении события могут идти чаще
                # раза в HEARTBEAT_S, и ping тогда не наступит.
                if time.monotonic() >= recheck_at:
                    try:
                        await asyncio.to_thread(check)
                    except HTTPException:
                        return
                    recheck_at = time.monotonic() + HEARTBEAT_S
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    # Событие, а не SSE-комментарий: поток не простаивает, прокси его не рвёт. Фронт ping не слушает.
                    yield _frame("ping", {})
                    continue
                yield _frame(event["kind"], event)
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
