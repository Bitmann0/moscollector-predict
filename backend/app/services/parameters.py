"""Настраиваемые параметры продукта (ТЗ §18, ML2-13): хранение, кеш и применение.

Хранятся строкой settings с ключом parameters: {"value": Parameters, "version": n,
"updated_at", "updated_by"}. Строки нет — действуют проверенные значения (VERIFIED),
version 0. Сохранённое значение, которое перестало проходить схему (поле добавили или
сузили диапазон), тоже заменяется проверенным, с предупреждением в лог.

Кеш. Приём зовёт current() на каждую пачку, а не на событие, и читает БД не чаще
раза в CACHE_TTL_S секунд. PUT в том же процессе сбрасывает кеш сразу; api работает
одним процессом uvicorn (daily_run.py), остальным процессам — scripts/reclassify_events.py
читает параметры сам — новое значение приходит не позже чем через CACHE_TTL_S.

Кто что применяет:
- rules → semantics.classify и series_hints при приёме (ingest.py) и при пересчёте
  истории (reclassify.py): уже принятые события без пересчёта хранят прежний класс;
- notifies → уведомления event.alarm при приёме;
- limit_by_head → дневной расчёт (daily_run.py): показ, уведомления alert.new,
  черновики заявок и журнал выданного — первые N рекомендаций в бюджете ML.
"""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, vocab
from ..config import get_settings
from ..schemas.parameters import (
    VERIFIED_LIMITS,
    Bound,
    GasThresholds,
    LimitParams,
    NotifyParams,
    Parameters,
    ParametersIn,
    ParametersOut,
    SeriesParams,
    TimeWindow,
)
from . import semantics

log = logging.getLogger(__name__)

KEY = "parameters"
CACHE_TTL_S = 5.0

_D = semantics.DEFAULT_RULES
VERIFIED = Parameters(
    gas=GasThresholds(alarm_pct=_D.gas_alarm, critical_pct=_D.gas_critical),
    gas_window=TimeWindow(hour_from=_D.gas_window_hours[0], hour_to=_D.gas_window_hours[1],
                          days=sorted(_D.gas_window_days)),
    series=SeriesParams(window_min=int(_D.series_window.total_seconds() // 60),
                        fire_min=_D.series_min["fire"], gas_min=_D.series_min["gas"],
                        work=TimeWindow(hour_from=_D.work_hours[0], hour_to=_D.work_hours[1],
                                        days=sorted(_D.work_days))),
    notify=NotifyParams(classes=["alarm", "critical"],
                        groups=list(vocab.codes("incident_group"))),
    limits=LimitParams(**VERIFIED_LIMITS),
)


@dataclass(frozen=True)
class State:
    values: Parameters
    rules: semantics.Rules
    version: int
    updated_at: datetime | None
    updated_by: str | None

    def notifies(self, event_class: str | None, group: str | None) -> bool:
        notify = self.values.notify
        return event_class in notify.classes and (group is None or group in notify.groups)

    def limit_by_head(self) -> dict[str, int]:
        """Лимит по голове ML: A_link, D, guard_weekly."""
        limits = self.values.limits.model_dump()
        return {vocab.scenario(code)["head"]: n for code, n in limits.items()}


def to_rules(values: Parameters) -> semantics.Rules:
    series = values.series
    return semantics.Rules(
        gas_alarm=values.gas.alarm_pct, gas_critical=values.gas.critical_pct,
        gas_window_hours=(values.gas_window.hour_from, values.gas_window.hour_to),
        gas_window_days=frozenset(values.gas_window.days),
        series_window=timedelta(minutes=series.window_min),
        series_min={"fire": series.fire_min, "gas": series.gas_min},
        work_hours=(series.work.hour_from, series.work.hour_to),
        work_days=frozenset(series.work.days))


def load(db: Session, *, for_update: bool = False) -> State:
    """Прямое чтение из БД, без кеша. for_update — блокировка строки до commit (PUT)."""
    row = db.get(models.Setting, KEY, with_for_update=for_update)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    values = VERIFIED
    version = int(stored.get("version") or 0)
    if "value" in stored:
        try:
            values = Parameters.model_validate(stored["value"])
        except ValidationError as exc:
            log.warning("settings.parameters v%s не проходит схему, действуют проверенные: %s",
                        version, exc.errors(include_url=False))
    updated_at = stored.get("updated_at")
    return State(values=values, rules=to_rules(values), version=version,
                 updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
                 updated_by=stored.get("updated_by"))


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: State | None = None
        self.loaded_at = 0.0

    def get(self, db: Session) -> State:
        now = time.monotonic()
        with self.lock:
            if self.state is not None and now - self.loaded_at < CACHE_TTL_S:
                return self.state
        state = load(db)
        with self.lock:
            self.state, self.loaded_at = state, now
        return state

    def clear(self) -> None:
        with self.lock:
            self.state = None


_cache = _Cache()


def current(db: Session) -> State:
    return _cache.get(db)


def invalidate() -> None:
    _cache.clear()


def _bounds() -> dict[str, Bound]:
    """Диапазоны из ограничений ge/le схемы; у лимитов верх — проверенное значение."""
    out: dict[str, Bound] = {}

    def walk(model: type, prefix: str) -> None:
        for name, info in model.model_fields.items():
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation, f"{prefix}{name}.")
                continue
            low = next((m.ge for m in info.metadata if hasattr(m, "ge")), None)
            high = next((m.le for m in info.metadata if hasattr(m, "le")), None)
            if name in VERIFIED_LIMITS and model is LimitParams:
                high = VERIFIED_LIMITS[name]
            if low is not None and high is not None:
                out[f"{prefix}{name}"] = Bound(min=low, max=high)

    walk(Parameters, "")
    return out


BOUNDS = _bounds()


def out(state: State) -> ParametersOut:
    return ParametersOut(values=state.values, verified=VERIFIED, bounds=BOUNDS,
                         locked=get_settings().demo_settings_locked, version=state.version,
                         updated_at=state.updated_at, updated_by=state.updated_by)


def _flat(values: Parameters) -> dict[str, object]:
    flat: dict[str, object] = {}

    def walk(data: dict, prefix: str) -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            else:
                flat[f"{prefix}{key}"] = value

    walk(values.model_dump(mode="json"), "")
    return flat


def changes(old: Parameters, new: Parameters) -> dict[str, list]:
    """{путь поля: [было, стало]} — для журнала действий."""
    before, after = _flat(old), _flat(new)
    return {key: [before.get(key), value] for key, value in after.items()
            if before.get(key) != value}


def require_unlocked() -> None:
    if get_settings().demo_settings_locked:
        raise HTTPException(status_code=403, detail="settings_locked")


def put(db: Session, body: ParametersIn, login: str) -> tuple[ParametersOut, dict]:
    """Сохраняет параметры; второе значение — что изменилось, для журнала действий.

    Два сохранения с одной expected_version: второе ждёт блокировку строки и получает
    409; на пустой таблице второй INSERT падает на ключе и тоже получает 409."""
    require_unlocked()
    state = load(db, for_update=True)
    if body.expected_version != state.version:
        db.rollback()
        raise HTTPException(status_code=409, detail="parameters_version_conflict")
    diff = changes(state.values, body.values)
    row = db.get(models.Setting, KEY)
    value = {"value": body.values.model_dump(mode="json"), "version": state.version + 1,
             "updated_at": datetime.now(UTC).isoformat(), "updated_by": login}
    if row is None:
        db.add(models.Setting(key=KEY, value=value))
    else:
        row.value = value
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="parameters_version_conflict") from None
    invalidate()
    return out(current(db)), diff
