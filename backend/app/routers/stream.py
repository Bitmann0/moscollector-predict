"""SSE: /api/v1/stream. Живое.

Сессию БД держим только на время проверки входа: соединение живёт долго.
"""
import asyncio
import json

from fastapi import APIRouter, Request
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
            authenticate(request, db)

    await asyncio.to_thread(check)
    queue = broker.subscribe()

    async def events():
        try:
            yield "retry: 5000\n\n"
            yield _frame("hello", {})
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield _frame(event["kind"], event)
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
