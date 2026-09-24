"""REST API — то, через что веб-интерфейс диспетчера получает прогнозы.

Тонкий слой над mkl.service: вся логика живёт там, здесь только маршруты,
фильтры и кэш. Так API можно заменить или продублировать другим транспортом,
не трогая расчёт.

Расчёт суточной выдачи занимает около двух с половиной секунд на все головы —
мало для норматива ТЗ (< 5 минут), но много, если дашборд опрашивает сервер
каждые несколько секунд. Поэтому результат кэшируется по суткам: в пределах
суток он не меняется, потому что не меняются и данные, по которым считан.
"""
import datetime as dt
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from . import contract, guard_queue, service, workorders
from .serve import load_heads

API_PREFIX = "/api/v1"

app = FastAPI(
    title="Москоллектор: предиктивная аналитика коллекторов",
    version=contract.SCHEMA_VERSION,
    description=(
        "Прогноз отказов и рисков по журналу событий СМВУ. "
        "Схема выдачи описана в mkl.contract и версионируется отдельно от "
        "моделей: переобучение головы её не меняет."
    ),
)

_cache: dict[Any, Any] = {}


def _alerts(asof: dt.date | None, only_in_budget: bool) -> list:
    key = ("alerts", asof, only_in_budget)
    if key not in _cache:
        _cache[key] = service.daily_alerts(asof=asof, only_in_budget=only_in_budget)
    return _cache[key]


def reset_cache() -> None:
    """Сбросить кэш — после пересборки фичестора или переобучения."""
    _cache.clear()


@app.get("/health")
def health() -> dict:
    """Готовность сервиса и какие головы обучены."""
    heads = load_heads()
    from .serve import model_path
    trained = [h for h in heads if model_path(h).exists()]
    return {
        "status": "ok" if trained else "no_models",
        "schema_version": contract.SCHEMA_VERSION,
        "heads_configured": len(heads),
        "heads_trained": trained,
    }


@app.get(f"{API_PREFIX}/directions")
def directions() -> list[dict]:
    """Направления прогнозирования по ТЗ и головы, которые их закрывают."""
    heads = load_heads()
    out = []
    for key, title in contract.DIRECTIONS.items():
        out.append({
            "direction": key,
            "title": title,
            "heads": [{"head": h, "title": c["title"],
                       "horizon_hours": int(c["horizon_days"]) * 24,
                       "budget_per_day": c["budget_per_day"]}
                      for h, c in heads.items() if c.get("direction") == key],
        })
    return out


@app.get(f"{API_PREFIX}/alerts")
def alerts(
    asof: dt.date | None = Query(None, description="сутки расчёта; по умолчанию последние доступные"),
    direction: str | None = Query(None, description="направление ТЗ"),
    head: str | None = Query(None),
    only_in_budget: bool = Query(True, description="только то, что предлагается к выезду"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    """Суточная выдача. Отсортирована по риску."""
    got = _alerts(asof, only_in_budget)
    if direction:
        got = [a for a in got if a.direction == direction]
    if head:
        got = [a for a in got if a.head == head]
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "count": len(got),
        "returned": min(len(got), limit),
        "alerts": [a.to_dict() for a in got[:limit]],
    }


@app.get(f"{API_PREFIX}/alerts/{{alert_id}}")
def alert(alert_id: str, asof: dt.date | None = None) -> dict:
    """Карточка алерта: адрес, окно прогноза и вклад признаков в ЭТУ строку."""
    for a in _alerts(asof, False):
        if a.alert_id == alert_id:
            return a.to_dict()
    raise HTTPException(status_code=404, detail="алерт не найден")


@app.get(f"{API_PREFIX}/coverage")
def coverage(asof: dt.date | None = None) -> list[dict]:
    """По скольким сущностям голова вообще может отвечать.

    Нужно интерфейсу, чтобы показать «по этому объекту прогноз не строится»
    вместо пустого места, которое читается как «всё спокойно».
    """
    return [c.to_dict() for c in service.coverage(asof)]


@app.get(f"{API_PREFIX}/guard-signal-priorities")
def guard_signal_priorities(
    asof: dt.date | None = Query(None, description="день признаков; по умолчанию последний доступный"),
    budget: int = Query(4, description="проверок в сутки: 1 или 4"),
) -> dict:
    """Manual-review ranking for a recorded guarded SMVU alarm tomorrow.

    Separate from legacy C and from the work-order endpoint. The priority
    score is ordinal, not a calibrated intrusion or incident probability.
    """
    try:
        return guard_queue.daily_priorities(asof, budget)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(f"{API_PREFIX}/work-orders")
def work_orders(
    asof: dt.date | None = None,
    priority: str | None = Query(None, description="срочная, плановая или наблюдение"),
    direction: str | None = None,
) -> dict:
    """Заявки на превентивное обслуживание.

    Формируются на объект, а не на канал: бригада выезжает по адресу, и
    несколько алертов по одному объекту — это один выезд.
    """
    got = workorders.build(_alerts(asof, True))
    if priority:
        got = [w for w in got if w.priority == priority]
    if direction:
        got = [w for w in got if w.direction == direction]
    return {"schema_version": workorders.SCHEMA_VERSION, "count": len(got),
            "orders": [w.to_dict() for w in got]}


@app.get(f"{API_PREFIX}/work-orders/{{order_id}}")
def work_order(order_id: str, asof: dt.date | None = None) -> dict:
    for w in workorders.build(_alerts(asof, True)):
        if w.order_id == order_id:
            return w.to_dict()
    raise HTTPException(status_code=404, detail="заявка не найдена")
