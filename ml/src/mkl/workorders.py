"""Автоматическое формирование заявок на превентивное обслуживание.

Заявка формируется НА ОБЪЕКТ, а не на канал. Бригада выезжает по адресу, и три
алерта по трём датчикам одного объекта за сутки — это один выезд. Заявка на
каждый алерт породила бы ровно ту лавину, от которой ТЗ и просит избавиться:
сегодня диспетчер тонет в ложных срабатываниях, и заменить их потоком
автозаявок значит не решить задачу, а переименовать её.

Срок исполнения берётся из горизонта головы, а не из регламента: регламентных
сроков ТО в выгрузке нет, и придумывать их нельзя. Горизонт — это то, что
модель действительно обещает.
"""
import datetime as dt
import hashlib
from dataclasses import asdict, dataclass, field

from .contract import Alert

SCHEMA_VERSION = "1.0"

# Вид работ по направлению ТЗ. Формулировки диспетчерские, а не модельные.
WORK_TYPE = {
    "sensor_failure": "Проверка и обслуживание датчика",
    "fire_risk": "Внеплановый осмотр участка на пожарный риск",
    "unauthorised_access": "Проверка охранного контура объекта",
    "infrastructure_wear": "Техническое обслуживание агрегата",
    "flood_risk": "Осмотр насосов и дренажа объекта",
    "beyond_scope": "Осмотр объекта",
}

PRIORITY_URGENT = "срочная"
PRIORITY_PLANNED = "плановая"
PRIORITY_WATCH = "наблюдение"


def _priority(max_risk: float, horizon_hours: int) -> str:
    """Срочность по риску и по тому, насколько близко окно прогноза.

    Голова износа смотрит на неделю вперёд, и её алерт с тем же риском не
    требует выезда сегодня — это очередь на плановое обслуживание.
    """
    if horizon_hours <= 24 and max_risk >= 0.8:
        return PRIORITY_URGENT
    if max_risk >= 0.5:
        return PRIORITY_PLANNED
    return PRIORITY_WATCH


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    schema_version: str
    created_for: dt.date          # сутки, по которым сформирована
    due_by: dt.datetime           # к какому моменту имеет смысл успеть
    priority: str
    work_type: str
    direction: str
    direction_title: str
    # адрес выезда
    obj: str | None
    obj_parent: str | None
    obj_kind: str | None
    pickets: list[float] = field(default_factory=list)
    # что послужило основанием
    alert_ids: list[str] = field(default_factory=list)
    case_keys: list[str] = field(default_factory=list)
    n_alerts: int = 0
    max_risk: float = 0.0
    channels: list[int] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    status: str = "новая"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_for"] = self.created_for.isoformat()
        d["due_by"] = self.due_by.isoformat()
        return d


def _order_id(day: dt.date, direction: str, obj: str | None) -> str:
    """Детерминированный номер: повторный расчёт суток обновляет заявку,
    а не создаёт вторую по тому же поводу."""
    key = f"{day.isoformat()}|{direction}|{obj}"
    return "WO-" + hashlib.sha256(key.encode()).hexdigest()[:12].upper()


def _rationale(alerts: list[Alert], limit: int = 3) -> list[str]:
    """Человеческое обоснование из вкладов признаков в самые рискованные
    алерты. Без выдумок: если вклады пусты, обоснование пустое."""
    seen: dict[str, float] = {}
    for a in sorted(alerts, key=lambda x: -x.risk):
        for f in a.factors:
            seen[f["label"]] = max(seen.get(f["label"], 0.0), f["contribution"])
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]]


def build(alerts: list[Alert], only_in_budget: bool = True) -> list[WorkOrder]:
    """Собрать заявки из суточной выдачи.

    Группировка по (направление, объект): разные направления требуют разных
    работ и разных бригад, поэтому объединять их в одну заявку нельзя, даже
    если объект один.
    """
    use = [a for a in alerts if a.in_budget or not only_in_budget]
    groups: dict[tuple, list[Alert]] = {}
    for a in use:
        groups.setdefault((a.direction, a.address.obj, a.asof), []).append(a)

    out: list[WorkOrder] = []
    for (direction, obj, day), items in groups.items():
        top = max(items, key=lambda x: x.risk)
        horizon = min(x.horizon_hours for x in items)
        out.append(WorkOrder(
            order_id=_order_id(day, direction, obj),
            schema_version=SCHEMA_VERSION,
            created_for=day,
            due_by=max(x.valid_to for x in items),
            priority=_priority(top.risk, horizon),
            work_type=WORK_TYPE.get(direction, "Осмотр объекта"),
            direction=direction,
            direction_title=top.direction_title,
            obj=obj,
            obj_parent=top.address.obj_parent,
            obj_kind=top.address.obj_kind,
            pickets=sorted({x.address.picket for x in items
                            if x.address.picket is not None}),
            alert_ids=[x.alert_id for x in items],
            case_keys=sorted({x.case_key for x in items}),
            n_alerts=len(items),
            max_risk=round(float(top.risk), 4),
            channels=sorted({x.address.channel for x in items
                             if x.address.channel is not None}),
            rationale=_rationale(items),
        ))
    order = {PRIORITY_URGENT: 0, PRIORITY_PLANNED: 1, PRIORITY_WATCH: 2}
    out.sort(key=lambda w: (order.get(w.priority, 3), -w.max_risk))
    return out
