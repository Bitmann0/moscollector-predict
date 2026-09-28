"""Замер ML2-04: время ответа ML-сервиса на каждый день демо-окна.

Запускается внутри контейнера api — у него есть сеть до ml:8001, порт ml наружу закрыт:

    docker compose -f compose.yaml -f compose.real.yaml exec -T api python - < scripts/measure_ml.py

Для каждого дня 01.06–30.06 — POST /api/v1/score по A_link и D с факторами и пустым
журналом выданного (как первый расчёт дня в run_daily), для понедельников — ещё
GET /api/v1/guard-weekly-inspections. Время — от отправки до полного ответа, запросы
строго по одному, как их шлёт backend (ML считает под одной блокировкой). Итог — JSON
в stdout: по запросу время и статусы голов, в конце min / median / max.

Памяти скрипт не видит; её снимают снаружи, например docker stats во время прогона.
"""
import json
import os
import statistics
import time
import urllib.request
from datetime import date, timedelta

ML = os.environ.get("ML_URL", "http://ml:8001").rstrip("/")
DAYS = [date(2026, 6, 1) + timedelta(days=n) for n in range(30)]


def call(method: str, path: str, body: dict | None = None) -> tuple[float, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ML + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as resp:
        payload = json.load(resp)
    return time.perf_counter() - t0, payload


rows = []
for day in DAYS:
    seconds, out = call("POST", "/api/v1/score", {
        "asof": day.isoformat(), "heads": ["A_link", "D"], "issued_histories": {"A_link": [], "D": []},
        "history_complete_from": (day - timedelta(days=7)).isoformat(), "with_factors": True})
    heads = {h: s.get("result_status") for h, s in (out.get("heads") or {}).items()}
    rows.append({"call": "score", "asof": day.isoformat(), "seconds": round(seconds, 2),
                 "heads": heads, "alerts": len(out.get("alerts") or [])})
    if day.weekday() == 0:
        seconds, out = call("GET", f"/api/v1/guard-weekly-inspections?asof={day.isoformat()}")
        rows.append({"call": "guard_weekly", "asof": day.isoformat(),
                     "seconds": round(seconds, 2), "status": out.get("result_status"),
                     "items": sum(len(v) for v in out.values() if isinstance(v, list))})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

summary = {}
for kind in ("score", "guard_weekly"):
    times = [r["seconds"] for r in rows if r["call"] == kind]
    if times:
        summary[kind] = {"n": len(times), "min": min(times),
                         "median": round(statistics.median(times), 2), "max": max(times)}
print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
