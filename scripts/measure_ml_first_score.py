"""Замер ML2-10: три /score подряд после рестарта ML, первый против следующих.

Запускается внутри контейнера api сразу после того, как ml стал healthy:

    docker restart <контейнер ml>     # дождаться healthy
    docker compose -f compose.yaml -f compose.real.yaml exec -T api python - < scripts/measure_ml_first_score.py

Запрос такой же, как в scripts/measure_ml.py: головы из HEADS (по умолчанию A_link, D, B,
E; замеры до 29.09 — HEADS=A_link,D) с факторами, пустой журнал выданного,
history_complete_from — за 7 суток до дня расчёта. ASOF задаёт день
(по умолчанию 2026-06-29). WAIT_READY=1 — сначала опрашивать /ready, пока в detail
написано «идёт прогрев», и только потом слать /score.
"""
import json
import os
import time
import urllib.request
from datetime import date, timedelta

ML = os.environ.get("ML_URL", "http://ml:8001").rstrip("/")
ASOF = date.fromisoformat(os.environ.get("ASOF", "2026-06-29"))
WAIT_READY = os.environ.get("WAIT_READY") == "1"
HEADS = os.environ.get("HEADS", "A_link,D,B,E").split(",")


def call(method: str, path: str, body: dict | None = None,
         timeout: float = 600) -> tuple[float, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ML + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return time.perf_counter() - t0, payload


if WAIT_READY:
    t0 = time.perf_counter()
    while True:
        seconds, out = call("GET", f"/ready?asof={ASOF.isoformat()}", timeout=60)
        print(json.dumps({"call": "ready", "seconds": round(seconds, 2),
                          "status": out.get("status"), "detail": out.get("detail")},
                         ensure_ascii=False), flush=True)
        if "идёт прогрев" not in (out.get("detail") or ""):
            break
        time.sleep(1)
    print(json.dumps({"waited_ready_s": round(time.perf_counter() - t0, 1)}), flush=True)

body = {"asof": ASOF.isoformat(), "heads": HEADS,
        "issued_histories": {h: [] for h in HEADS},
        "history_complete_from": (ASOF - timedelta(days=7)).isoformat(),
        "with_factors": True}
for n in (1, 2, 3):
    seconds, out = call("POST", "/api/v1/score", body)
    heads = {h: s.get("result_status") for h, s in (out.get("heads") or {}).items()}
    print(json.dumps({"call": f"score#{n}", "asof": ASOF.isoformat(),
                      "seconds": round(seconds, 2), "heads": heads,
                      "alerts": len(out.get("alerts") or [])}, ensure_ascii=False), flush=True)
