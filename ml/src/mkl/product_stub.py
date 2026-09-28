"""Синтетический генератор ответов C1 для ML_MODE=stub.

Ответы детерминированы по входу: тот же запрос даёт тот же JSON, поэтому
фикстуры contracts/fixtures/ml_*.json пересобираются без диффа. Каналы и
объекты берутся из contracts/synthetic_reference.json — его же грузит seed
backend, и адреса в прогнозах совпадают с деревом объектов в БД. Сущности
называются «Объект-заглушка N», в ответах source="stub", версия модели
"stub-<голова>-v0".

Импорты — только stdlib, pydantic и mkl.contract (формулы alert_id и
case_key): сервис в режиме заглушки стартует без данных, моделей и polars.

Реальные ответы строят функции _real_* в mkl.product_api. Этот модуль
остаётся для ML_MODE=stub, CI и фикстур; у каждой публичной функции в шапке
указан владелец real-режима.
"""
import datetime as dt
import functools
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import contract
from .product_contract import (
    AddressOut,
    AlertOut,
    CoverageOut,
    DirectionHead,
    DirectionItem,
    FactorOut,
    HeadStatus,
    OutcomeQuery,
    OutcomeResult,
    ReadyResponse,
    ScoreRequest,
    ScoreResponse,
    WeeklyPriority,
    WeeklyResponse,
    WorkOrderOut,
)

# Демо-окно заканчивается 2026-06-30 (DEMO_TODAY backend). Позже данных нет.
DATA_LAST_DAY = dt.date(2026, 6, 30)
# День без данных из плана команды (ML1-04: asof=2026-06-01 → no_data).
# Заглушка отвечает на него так же, как будет отвечать real-режим.
NO_DATA_DAY = dt.date(2026, 6, 1)


@dataclass(frozen=True)
class HeadSpec:
    head: str
    direction: str
    title: str
    horizon_hours: int
    budget_per_day: int
    cooldown_days: int
    budget_per_object: bool
    model_lag_days: int  # задержка порога заглушки; предел — max_model_lag_days


# Копия ml/configs/heads.yaml: direction, title, horizon_days × 24,
# budget_per_day, cooldown_days, budget_per_object. Заглушка конфиги не читает;
# test_product_api.py сверяет эти значения с файлом.
HEADS = {
    "A_link": HeadSpec("A_link", "sensor_failure", "Потеря связи с каналом",
                       horizon_hours=24, budget_per_day=20, cooldown_days=7,
                       budget_per_object=False, model_lag_days=7),
    "D": HeadSpec("D", "infrastructure_wear", "Проверка повторяющихся сигналов оборудования",
                  horizon_hours=168, budget_per_day=3, cooldown_days=7,
                  budget_per_object=True, model_lag_days=14),
}

# Копия mkl.config.EQUIPMENT_STYPES: цель D определена только для оборудования,
# serve.score отбрасывает остальные каналы до отсечки бюджета.
EQUIPMENT_STYPES = frozenset({
    "Состояние насоса", "Состояние вентилятора", "ИБП",
    "Датчик затопления", "КД Люк", "9-секционный люк",
})

# Копия mkl.address.OBJ_KIND_RU и нужной части mkl.workorders.WORK_TYPE.
OBJ_KIND_RU = {"controlHouse": "диспетчерский пункт", "guardObject": "охранная зона"}
WORK_TYPE = {
    "sensor_failure": "Проверка и обслуживание датчика",
    "infrastructure_wear": "Плановая диагностика агрегата",
}

# Порог рабочей точки заглушки. В real-режиме порог лежит в артефакте модели.
STUB_THRESHOLD = 0.3
# Уровень риска дня: risk канала = уровень × U[0, 1). У D нижняя граница ниже
# порога, поэтому часть дней D целиком под порогом — это empty_valid.
RISK_LEVEL = {"A_link": (0.45, 1.0), "D": (0.15, 1.0)}
# Доля каналов, приславших данные за сутки: остальные не скорятся и
# уменьшают coverage.entities_scored.
REPORT_SHARE = 0.93

# Имена и подписи признаков — из mkl.explain.FEATURE_LABELS.
FACTOR_POOL = {
    "A_link": [("silence_z", "необычно долгое молчание"),
               ("gap_vs_own_rhythm", "разрыв против обычного ритма"),
               ("prev_gap_days_mean_w30", "обычный ритм опроса"),
               ("n_active_days_w7", "активных суток за неделю")],
    "D": [("n_bad_w30", "неисправности за месяц"),
          ("time_in_bad_s", "время в неисправном состоянии"),
          ("days_since_last_bad", "суток с последней неисправности"),
          ("n_battery_power", "переходы на питание от батарей")],
}

# Приоритеты заявок — как mkl.workorders._priority.
PRIORITY_URGENT, PRIORITY_PLANNED, PRIORITY_WATCH = "срочная", "плановая", "наблюдение"
_PRIORITY_ORDER = {PRIORITY_URGENT: 0, PRIORITY_PLANNED: 1, PRIORITY_WATCH: 2}

# Недельная очередь — константы mkl.guard_weekly (MIN_ALARM_DAYS, BUDGET,
# COOLDOWN_DAYS).
WEEKLY_HEAD = "guard_weekly"
WEEKLY_TITLE = "Недельная очередь осмотра охранного контура"
WEEKLY_HORIZON_HOURS = 168  # окно D+2..D+9, правая граница не входит
WEEKLY_POLICY = {"alarm_days_in_last_7_at_least": 4,
                 "max_objects_per_week": 4,
                 "same_object_cooldown_days": 14}
# Ротация: объект охранной зоны с номером i — кандидат в недели, где
# номер недели ≡ i (mod 3). Повтор не раньше чем через 21 сутки, то есть
# пауза 14 суток соблюдается без восстановления прошлых выборов.
WEEKLY_ROTATION = 3
WEEKLY_ANCHOR = NO_DATA_DAY  # понедельник отсчёта недель
WEEKLY_SHARE = 0.7           # доля кандидатов недели, прошедших порог 4 из 7


def reference_path() -> Path:
    """contracts/synthetic_reference.json: CONTRACTS_DIR или <корень репо>/contracts.

    В образе ml корень — /srv, файл копируется в /srv/contracts (ml/Dockerfile).
    """
    base = os.environ.get("CONTRACTS_DIR")
    root = Path(base) if base else Path(__file__).resolve().parents[3] / "contracts"
    return root / "synthetic_reference.json"


@functools.lru_cache(maxsize=1)
def _reference() -> tuple[dict[str, dict], list[dict]]:
    path = reference_path()
    if not path.exists():
        raise FileNotFoundError(
            f"нет синтетического справочника {path}: задайте CONTRACTS_DIR")
    data = json.loads(path.read_text(encoding="utf-8"))
    objects = {str(o["id"]): o for o in data["objects"]}
    channels = sorted(data["channels"], key=lambda c: int(c["id"]))
    return objects, channels


def _u(*parts) -> float:
    """Детерминированное U[0, 1) от ключа. hash() не годится: он солится на процесс."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _has_data(asof: dt.date) -> bool:
    return asof != NO_DATA_DAY and asof <= DATA_LAST_DAY


def _no_data_detail(asof: dt.date) -> str:
    if asof > DATA_LAST_DAY:
        return f"данные есть по {DATA_LAST_DAY.isoformat()}, за {asof.isoformat()} их нет"
    return f"нет данных за {asof.isoformat()}: день без данных в демо-окне"


def _population(head: str) -> list[dict]:
    channels = _reference()[1]
    if head == "D":
        return [c for c in channels if c["sensor_type"] in EQUIPMENT_STYPES]
    return list(channels)


def _address(ch: dict) -> AddressOut:
    """Адрес канала в форме service._address: сегмента у канальных голов нет."""
    objects = _reference()[0]
    obj = objects[str(ch["obj_id"])]
    parent = objects.get(str(obj["parent_id"])) if obj.get("parent_id") else None
    picket = ch.get("picket")
    return AddressOut(
        obj=obj["id"], obj_parent=obj.get("parent_id"), obj_kind=obj["kind"],
        channel=int(ch["id"]), segment=None,
        picket=None if picket is None else float(picket),
        obj_name=obj["name"], obj_parent_name=parent["name"] if parent else None,
        obj_kind_ru=OBJ_KIND_RU.get(obj["kind"]),
        sensor_name=ch["name"], sensor_type=ch["sensor_type"], tag=ch["tag"],
        picket_label=f"ПК {picket:g}" if picket is not None else "пикет неизвестен",
        segment_label=None, address_known=True)


def _budget(rows: list[dict], spec: HeadSpec) -> set[int]:
    """Каналы, попавшие в top-k, как serve._apply_budget.

    При budget_per_object сначала идут лучшие каналы каждого объекта, затем
    вторые и так далее: иначе весь бюджет D оседает на одном объекте.
    """
    order = rows
    if spec.budget_per_object:
        in_obj: dict[str, int] = {}
        keyed = []
        for r in rows:  # rows уже по убыванию риска
            obj = str(r["ch"]["obj_id"])
            n = in_obj.get(obj, 0)
            in_obj[obj] = n + 1
            keyed.append(((n, -r["risk"], obj, int(r["ch"]["id"])), r))
        order = [r for _, r in sorted(keyed, key=lambda kv: kv[0])]
    return {int(r["ch"]["id"]) for r in order[:spec.budget_per_day]}


def _blocked(spec: HeadSpec, req: ScoreRequest) -> set[int]:
    """Пауза как serve.apply_issued_cooldown: выданное за 1..cooldown суток до asof.

    Запись того же дня паузу не ставит, поэтому повторный расчёт дня даёт ту же
    выдачу. Освобождённые места бюджета не добираются следующими по риску.
    """
    return {e.channel for e in req.issued_histories.get(spec.head, [])
            if e.channel is not None
            and 0 < (req.asof - e.sent_day).days <= spec.cooldown_days}


def _journal_note(spec: HeadSpec, req: ScoreRequest) -> str | None:
    need = req.asof - dt.timedelta(days=spec.cooldown_days)
    if spec.head in req.issued_histories and req.history_complete_from is not None \
            and req.history_complete_from <= need:
        return None
    return (f"журнал выданного неполон: нужны issued_histories[{spec.head}] и "
            f"history_complete_from ≤ {need.isoformat()}. Заглушка считает без "
            f"него; mkl.service.alerts_for_head в real-режиме голову не посчитает")


def _factors(head: str, asof: dt.date, ch: int) -> list[FactorOut]:
    pool = sorted(FACTOR_POOL[head], key=lambda f: _u(asof, head, ch, f[0]))
    n = 2 + (_u(asof, head, ch, "n_factors") < 0.5)
    picked = pool[:n]
    contribs = sorted((round(0.2 + 1.3 * _u(asof, head, ch, f, "c"), 3) for f, _ in picked),
                      reverse=True)
    return [FactorOut(feature=f, label=label, contribution=c)
            for (f, label), c in zip(picked, contribs, strict=True)]


def _score_head(spec: HeadSpec, req: ScoreRequest
                ) -> tuple[HeadStatus, list[AlertOut], CoverageOut]:
    asof = req.asof
    population = _population(spec.head)
    model_version = f"stub-{spec.head}-v0"
    reason = ("только каналы оборудования: насосы, вентиляторы, ИБП, датчики "
              "затопления, люки" if spec.head == "D" else None)
    if not _has_data(asof):
        return (HeadStatus(result_status="no_data", model_version=model_version,
                           detail=_no_data_detail(asof)),
                [],
                CoverageOut(head=spec.head, direction=spec.direction,
                            entities_total=len(population), entities_scored=0,
                            reason="нет данных за сутки", fraction=0.0))

    lo, hi = RISK_LEVEL[spec.head]
    level = lo + (hi - lo) * _u(asof, spec.head, "level")
    scored = [c for c in population if _u(asof, "report", c["id"]) < REPORT_SHARE]
    rows = sorted(({"ch": c, "risk": round(level * _u(asof, spec.head, c["id"], "risk"), 4)}
                   for c in scored),
                  key=lambda r: (-r["risk"], int(r["ch"]["id"])))
    selected = _budget(rows, spec)
    blocked = _blocked(spec, req)
    start = dt.datetime.combine(asof + dt.timedelta(days=1), dt.time())
    alerts: list[AlertOut] = []
    for rank, row in enumerate(rows, start=1):
        ch, risk = row["ch"], row["risk"]
        ch_id = int(ch["id"])
        above = risk >= STUB_THRESHOLD
        in_budget = ch_id in selected and above and ch_id not in blocked
        # Сущность — канал и его объект, как строка скоринга в service.alerts_for_head.
        entity = {"ch": ch_id, "obj": str(ch["obj_id"])}
        alerts.append(AlertOut(
            alert_id=contract.make_alert_id(spec.head, entity, asof),
            case_key=contract.make_case_key(spec.head, entity),
            head=spec.head, direction=spec.direction,
            direction_title=contract.DIRECTIONS[spec.direction], title=spec.title,
            asof=asof, valid_from=start,
            valid_to=start + dt.timedelta(hours=spec.horizon_hours),
            horizon_hours=spec.horizon_hours,
            risk=risk, rank=rank, in_budget=in_budget, above_threshold=above,
            address=_address(ch), model_version=model_version,
            # Факторы только в бюджете: так будет и в real-режиме (ML1-04).
            factors=_factors(spec.head, asof, ch_id) if in_budget and req.with_factors else []))

    status = HeadStatus(
        result_status="ok" if any(a.in_budget for a in alerts) else "empty_valid",
        model_version=model_version,
        threshold_end=asof - dt.timedelta(days=spec.model_lag_days),
        model_lag_days=spec.model_lag_days, threshold_feasible=True,
        detail=_journal_note(spec, req))
    coverage = CoverageOut(head=spec.head, direction=spec.direction,
                           entities_total=len(population), entities_scored=len(scored),
                           reason=reason,
                           fraction=round(len(scored) / len(population), 4) if population else 0.0)
    return status, alerts, coverage


def _order_id(day: dt.date, direction: str, obj: str | None) -> str:
    """Формула mkl.workorders._order_id: повторный расчёт суток обновляет заявку."""
    key = f"{day.isoformat()}|{direction}|{obj}"
    return "WO-" + hashlib.sha256(key.encode()).hexdigest()[:12].upper()


def _priority(max_risk: float, horizon_hours: int) -> str:
    if horizon_hours <= 24 and max_risk >= 0.8:
        return PRIORITY_URGENT
    if max_risk >= 0.5:
        return PRIORITY_PLANNED
    return PRIORITY_WATCH


def _rationale(items: list[AlertOut], limit: int = 3) -> list[str]:
    seen: dict[str, float] = {}
    for a in sorted(items, key=lambda x: -x.risk):
        for f in a.factors:
            seen[f.label] = max(seen.get(f.label, 0.0), f.contribution)
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]]


def _work_orders(alerts: list[AlertOut]) -> list[WorkOrderOut]:
    """Заявки на объект по алертам в бюджете, как mkl.workorders.build."""
    groups: dict[tuple[str, str | None], list[AlertOut]] = {}
    for a in alerts:
        if a.in_budget:
            groups.setdefault((a.direction, a.address.obj), []).append(a)
    out: list[WorkOrderOut] = []
    for (direction, obj), items in groups.items():
        top = max(items, key=lambda x: x.risk)
        out.append(WorkOrderOut(
            order_id=_order_id(top.asof, direction, obj), created_for=top.asof,
            due_by=max(x.valid_to for x in items),
            priority=_priority(top.risk, min(x.horizon_hours for x in items)),
            work_type=WORK_TYPE.get(direction, "Осмотр объекта"),
            direction=direction, direction_title=top.direction_title,
            obj=obj, obj_parent=top.address.obj_parent, obj_kind=top.address.obj_kind,
            obj_name=top.address.obj_name, obj_parent_name=top.address.obj_parent_name,
            pickets=sorted({x.address.picket for x in items if x.address.picket is not None}),
            alert_ids=[x.alert_id for x in items],
            case_keys=sorted({x.case_key for x in items}),
            n_alerts=len(items), max_risk=round(float(top.risk), 4),
            channels=sorted({x.address.channel for x in items
                             if x.address.channel is not None}),
            rationale=_rationale(items)))
    out.sort(key=lambda w: (_PRIORITY_ORDER[w.priority], -w.max_risk, w.order_id))
    return out


def score(req: ScoreRequest) -> ScoreResponse:
    """Ответ для ML_MODE=stub: CI, фикстуры, машина без данных заказчика.
    В real-режиме ответ строит product_api._real_score() — поголовный
    вызов mkl.service.alerts_for_head, покрытие пилотных голов, workorders.build.
    Контракт: ScoreRequest → ScoreResponse не меняются; тест
    tests/test_product_api.py должен остаться зелёным.

    Синтетика: у A_link скорятся все каналы справочника, у D — только
    оборудование. Риск и ранг детерминированы по (asof, голова, канал).
    Паузы из issued_histories соблюдаются, как в real-режиме.
    """
    statuses: dict[str, HeadStatus] = {}
    alerts: list[AlertOut] = []
    coverage: list[CoverageOut] = []
    for head in dict.fromkeys(req.heads):
        status, got, cov = _score_head(HEADS[head], req)
        statuses[head] = status
        alerts.extend(got)
        coverage.append(cov)
    # Порядок как в service.daily_alerts: по риску, внутри головы — по рангу.
    alerts.sort(key=lambda a: (-a.risk, a.head, a.rank))
    return ScoreResponse(asof=req.asof, source="stub", heads=statuses,
                         data_snapshot={"data_last_day": DATA_LAST_DAY.isoformat()},
                         alerts=alerts, coverage=coverage,
                         work_orders=_work_orders(alerts))


def weekly(asof: dt.date) -> WeeklyResponse:
    """Ответ для ML_MODE=stub: CI, фикстуры, машина без данных заказчика.
    В real-режиме ответ строит product_api._real_weekly() — вызов
    mkl.guard_weekly.weekly_inspections, день без данных → no_data вместо 409.
    Контракт: понедельник → WeeklyResponse, не понедельник → ValueError (HTTP 422);
    тест tests/test_product_api.py должен остаться зелёным.

    Синтетика: 0–2 объекта охранной зоны в неделю по ротации WEEKLY_ROTATION.
    """
    if asof.weekday() != 0:
        raise ValueError(
            f"недельная очередь считается по понедельникам: {asof.isoformat()} — не понедельник")
    base = {"asof": asof,
            "valid_from": asof + dt.timedelta(days=2),
            "valid_to": asof + dt.timedelta(days=9),
            "next_run": asof + dt.timedelta(days=7),
            "model_version": f"stub-{WEEKLY_HEAD}-v0",
            "policy": dict(WEEKLY_POLICY),
            # как в guard_weekly: конец кэша событий + 1 сутки
            "event_cache_through": DATA_LAST_DAY + dt.timedelta(days=1),
            "data_snapshot": {"data_last_day": DATA_LAST_DAY.isoformat()},
            "data_last_day": DATA_LAST_DAY, "source": "stub"}
    if not _has_data(asof):
        return WeeklyResponse(**base, result_status="no_data")

    objects = _reference()[0]
    guard = sorted((o for o in objects.values() if o["kind"] == "guardObject"),
                   key=lambda o: str(o["id"]))
    week = (asof - WEEKLY_ANCHOR).days // 7
    picks = []
    for i, obj in enumerate(guard):
        oid = str(obj["id"])
        if i % WEEKLY_ROTATION != week % WEEKLY_ROTATION or \
                _u(asof, WEEKLY_HEAD, oid) >= WEEKLY_SHARE:
            continue
        days7 = WEEKLY_POLICY["alarm_days_in_last_7_at_least"] + int(4 * _u(asof, oid, "d7"))
        parent = objects.get(str(obj["parent_id"])) if obj.get("parent_id") else None
        picks.append({
            "obj": oid,
            "recommendation_id": contract.make_alert_id(
                WEEKLY_HEAD, {"obj": oid, "target": "D+2..D+8"}, asof),
            "case_key": contract.make_case_key(WEEKLY_HEAD, {"obj": oid}),
            "priority_score": round(0.3 + 0.7 * _u(asof, oid, "priority"), 4),
            "recent_alarm_days_7": days7,
            "recent_alarm_days_30": days7 + int(13 * _u(asof, oid, "d30")),
            "guard_state_age_days": int(60 * _u(asof, oid, "age")),
            "evidence": "alarm_on_at_least_4_of_previous_7_days",
            "obj_name": obj["name"],
            "obj_parent_name": parent["name"] if parent else None,
            "address_known": True})
    picks.sort(key=lambda p: (-p["priority_score"], p["obj"]))
    priorities = [WeeklyPriority(rank=rank, **p) for rank, p in enumerate(picks, start=1)]
    return WeeklyResponse(**base, result_status="ok" if priorities else "empty_valid",
                          priorities=priorities)


def outcomes(items: list[OutcomeQuery]) -> list[OutcomeResult]:
    """Ответ для ML_MODE=stub: CI, фикстуры, машина без данных заказчика.
    В real-режиме ответ строит product_api._real_outcomes() — факт по
    меткам бандла; строки вне наблюдаемых дней (labels._observable) → unknown.
    Контракт: по одному OutcomeResult на запрос в том же порядке; тест
    tests/test_product_api.py должен остаться зелёным.

    Синтетика: hit 35 %, miss 50 %, unknown 15 % — по хешу id.
    """
    out = []
    for item in items:
        u = _u("outcome", item.id)
        outcome = "hit" if u < 0.35 else "miss" if u < 0.85 else "unknown"
        out.append(OutcomeResult(id=item.id, outcome=outcome))
    return out


def ready(asof: dt.date | None = None) -> ReadyResponse:
    """Ответ для ML_MODE=stub: CI, фикстуры, машина без данных заказчика.
    В real-режиме ответ строит product_api._real_ready() — готовность
    данных бандла и артефактов голов на дату.
    Контракт: ReadyResponse не меняется; тест tests/test_product_api.py должен
    остаться зелёным.
    """
    if asof is not None and not _has_data(asof):
        return ReadyResponse(status="missing_data", asof=asof, data_last_day=DATA_LAST_DAY,
                             detail=_no_data_detail(asof), source="stub")
    return ReadyResponse(status="ready", asof=asof, data_last_day=DATA_LAST_DAY,
                         detail="заглушка: синтетические ответы, модели не загружены",
                         source="stub")


def directions() -> list[DirectionItem]:
    """Ответ для ML_MODE=stub: CI, фикстуры, машина без данных заказчика.
    В real-режиме ответ строит product_api._real_directions() — пилотные
    головы из configs/heads.yaml и недельная очередь.
    Контракт: list[DirectionItem] не меняется; тест tests/test_product_api.py
    должен остаться зелёным.
    """
    items = [DirectionItem(direction=s.direction, title=contract.DIRECTIONS[s.direction],
                           heads=[DirectionHead(head=s.head, title=s.title,
                                                horizon_hours=s.horizon_hours,
                                                budget_per_day=s.budget_per_day)])
             for s in HEADS.values()]
    items.append(DirectionItem(
        direction="unauthorised_access", title=contract.DIRECTIONS["unauthorised_access"],
        heads=[DirectionHead(head=WEEKLY_HEAD, title=WEEKLY_TITLE,
                             horizon_hours=WEEKLY_HORIZON_HOURS, budget_per_day=None)]))
    return items
