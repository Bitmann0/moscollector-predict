"""scripts/reclassify_events.py: история, загруженная до 28.09, получает новые класс,
подсказку и группу; повтор ничего не меняет; БД без миграции 0002 получает колонку."""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from app import models
from app.db import get_engine
from app.services.helpers import MSK, to_db
from sqlalchemy import inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reclassify_events  # scripts/ — не пакет: скрипты запускаются файлами

OLD_HINT = "вероятно, плановая проверка"


@pytest.fixture
def history(db):
    """Как на стенде до пересчёта: классы и подсказки по правилам до 28.09, группы нет."""
    db.add(models.RefObject(id="R-C", level=2, parent_id=None, kind="controlHouse", name="К"))
    db.add(models.RefObject(id="R-1", level=3, parent_id="R-C", kind="controlHouse", name="О"))
    for n in range(5):
        db.add(models.RefChannel(id=993000 + n, obj_id="R-1", sensor_type="Датчик дыма"))
    db.add(models.RefChannel(id=993010, obj_id="R-1", sensor_type="Газовый датчик"))
    db.add(models.RefChannel(id=993011, obj_id="R-1", sensor_type="ИБП"))
    rows = [(n, 993000 + n, datetime(2026, 6, 29, 10, n, tzinfo=MSK), "Обнаружен дым",
             "alarm", None) for n in range(5)]
    rows += [(10, 993010, datetime(2026, 6, 30, 11, 0, tzinfo=MSK), "Обнаружен газ",
              "alarm", OLD_HINT),
             (11, 993011, datetime(2026, 6, 30, 11, 0, tzinfo=MSK), "Питание от батарей",
              "alarm", None)]
    for event_id, channel, ts, value, cls, hint in rows:
        db.add(models.Event(event_id=event_id, channel_id=channel, ts=to_db(ts), alarm=True,
                            val_raw=value, event_class=cls, hint=hint,
                            row_hash=f"rc-{event_id}"))
    db.commit()
    return db


def stored(db) -> dict[int, tuple]:
    db.expire_all()
    return {e.event_id: (e.event_class, e.hint, e.incident_group)
            for e in db.scalars(select(models.Event))}


def test_history_gets_groups_and_ppr_hints_once(history):
    engine = get_engine()
    first = reclassify_events.reclassify(engine, out=lambda _: None)
    fire = "вероятно, ППР или ТО: серия из 5 извещателей за 10 минут"
    assert stored(history) == {
        **{n: ("critical", fire, "fire") for n in range(5)},
        10: ("critical", "вероятно, ППР или ТО: газ в будни с 9:00 до 14:59", "gas"),
        11: ("alarm", None, None),
    }
    assert (first.rows, first.changed, first.classes[("alarm", "critical")]) == (7, 6, 6)
    again = reclassify_events.reclassify(engine, out=lambda _: None)
    assert (again.rows, again.changed) == (7, 0)


def test_dry_run_writes_nothing(history):
    before = stored(history)
    totals = reclassify_events.reclassify(get_engine(), dry_run=True, out=lambda _: None)
    assert totals.changed == 6 and stored(history) == before


def test_database_without_migration_0002_gets_column(history):
    """Стенд на образе до миграции 0002: колонку и индекс добавляет скрипт."""
    engine = get_engine()
    history.close()
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_events_incident_group"))
        conn.execute(text("ALTER TABLE events DROP COLUMN incident_group"))
    assert "incident_group" not in {c["name"] for c in inspect(engine).get_columns("events")}
    totals = reclassify_events.reclassify(engine, out=lambda _: None)
    insp = inspect(engine)
    assert "incident_group" in {c["name"] for c in insp.get_columns("events")}
    assert "ix_events_incident_group" in {i["name"] for i in insp.get_indexes("events")}
    assert totals.groups["fire"] == 5
