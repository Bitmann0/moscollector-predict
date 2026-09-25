"""Заявки на превентивное обслуживание.

Заявка формируется на объект, а не на канал: бригада выезжает по адресу.
Заявка на каждый алерт породила бы ровно ту лавину, от которой ТЗ и просит
избавиться — сегодня диспетчер тонет в ложных срабатываниях, и заменить их
потоком автозаявок значит переименовать задачу, а не решить.
"""
import datetime as dt

from mkl import contract, workorders
from mkl.contract import Address, Alert

DAY = dt.date(2026, 6, 30)


def _a(head, obj, risk, direction="unauthorised_access", horizon=24, ch=None,
       picket=None, factors=()):
    ent = {"obj": obj} if ch is None else {"obj": obj, "ch": ch}
    start = dt.datetime.combine(DAY, dt.time()) + dt.timedelta(days=1)
    return Alert(
        alert_id=contract.make_alert_id(head, ent, DAY),
        case_key=contract.make_case_key(head, ent),
        schema_version=contract.SCHEMA_VERSION, head=head, direction=direction,
        direction_title=contract.DIRECTIONS[direction], title="t", asof=DAY,
        valid_from=start, valid_to=start + dt.timedelta(hours=horizon),
        horizon_hours=horizon, risk=risk, rank=1, in_budget=True,
        above_threshold=True,
        address=Address(obj=obj, channel=ch, picket=picket),
        factors=list(factors))


def test_alerts_on_one_object_become_one_order():
    orders = workorders.build([_a("C", "5122", 0.9, ch=1, picket=10.0),
                               _a("C", "5122", 0.8, ch=2, picket=12.0),
                               _a("C", "5122", 0.7, ch=3, picket=14.0)])
    assert len(orders) == 1
    assert orders[0].n_alerts == 3
    assert orders[0].channels == [1, 2, 3]
    assert orders[0].pickets == [10.0, 12.0, 14.0]


def test_different_directions_on_one_object_stay_separate():
    """Разные направления требуют разных работ и разных бригад."""
    orders = workorders.build([
        _a("C", "5122", 0.9),
        _a("D", "5122", 0.9, direction="infrastructure_wear", horizon=168),
    ])
    assert len(orders) == 2
    assert {o.work_type for o in orders} == {
        workorders.WORK_TYPE["unauthorised_access"],
        workorders.WORK_TYPE["infrastructure_wear"]}


def test_order_id_is_deterministic():
    """Повторный расчёт суток обновляет заявку, а не создаёт вторую по тому же
    поводу."""
    one = workorders.build([_a("C", "5122", 0.9)])[0]
    two = workorders.build([_a("C", "5122", 0.9)])[0]
    assert one.order_id == two.order_id


def test_long_horizon_is_not_urgent_at_the_same_risk():
    """Голова износа смотрит на неделю вперёд: её алерт с тем же риском — это
    очередь на плановое обслуживание, а не выезд сегодня."""
    urgent = workorders.build([_a("C", "1", 0.95)])[0]
    planned = workorders.build([
        _a("D", "2", 0.95, direction="infrastructure_wear", horizon=168)])[0]
    assert urgent.priority == workorders.PRIORITY_URGENT
    assert planned.priority == workorders.PRIORITY_PLANNED


def test_low_risk_becomes_watch_not_a_visit():
    assert workorders.build([_a("C", "1", 0.2)])[0].priority == workorders.PRIORITY_WATCH


def test_rationale_comes_from_factors_and_is_not_invented():
    """Если вкладов нет, обоснование пустое: выдумывать причину нельзя."""
    with_f = workorders.build([_a("C", "1", 0.9, factors=[
        {"feature": "n_intrusion", "label": "срабатывания", "contribution": 0.5},
        {"feature": "dow", "label": "день недели", "contribution": 0.1}])])[0]
    assert with_f.rationale == ["срабатывания", "день недели"]
    without = workorders.build([_a("C", "2", 0.9)])[0]
    assert without.rationale == []


def test_orders_are_sorted_by_urgency_then_risk():
    orders = workorders.build([
        _a("C", "1", 0.3), _a("C", "2", 0.95), _a("C", "3", 0.6)])
    assert [o.priority for o in orders] == [
        workorders.PRIORITY_URGENT, workorders.PRIORITY_PLANNED,
        workorders.PRIORITY_WATCH]


def test_due_by_matches_the_forecast_window():
    """Срок берётся из горизонта модели, а не из регламента: регламентных
    сроков ТО в выгрузке нет, и придумывать их нельзя."""
    o = workorders.build([_a("D", "1", 0.9, direction="infrastructure_wear",
                             horizon=168)])[0]
    assert (o.due_by - dt.datetime.combine(DAY, dt.time())).days == 8
