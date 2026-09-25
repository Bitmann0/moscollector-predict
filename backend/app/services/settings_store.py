"""Демо-настройки: demo_today, режим, скорость воспроизведения.

ЗАГЛУШКА — владелец BE-05 (C2).
Заменить: проверку, что demo_today лежит в окне данных (июнь 2026), и переключение
режима replay вместе с replay.py (ML2-03).
Контракт: get/put и SettingsOut не меняются, при DEMO_SETTINGS_LOCKED=1 put отвечает
403 settings_locked; тесты tests/test_auth.py и tests/test_endpoints_shape.py должны
остаться зелёными.

Значение лежит в таблице settings как {"value": ...}: колонка JSON объявлена словарём.
Строки создаёт seed; пока строки нет, действует значение по умолчанию.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..schemas.misc import SettingsIn, SettingsOut
from ..security import CurrentUser


def defaults() -> dict:
    return {"demo_today": get_settings().demo_today.isoformat(), "mode": "archive",
            "replay_speed": 60}


def get(db: Session) -> SettingsOut:
    values = defaults()
    for key in values:
        row = db.get(models.Setting, key)
        if row is not None and isinstance(row.value, dict) and "value" in row.value:
            values[key] = row.value["value"]
    return SettingsOut(demo_today=date.fromisoformat(str(values["demo_today"])),
                       mode=values["mode"], replay_speed=int(values["replay_speed"]),
                       locked=get_settings().demo_settings_locked)


def demo_today(db: Session) -> date:
    return get(db).demo_today


def put(db: Session, body: SettingsIn, user: CurrentUser) -> SettingsOut:
    if get_settings().demo_settings_locked:
        raise HTTPException(status_code=403, detail="settings_locked")
    changes = body.model_dump(mode="json", exclude_none=True)
    for key, value in changes.items():
        row = db.get(models.Setting, key)
        if row is None:
            db.add(models.Setting(key=key, value={"value": value}))
        else:
            row.value = {"value": value}
    db.commit()
    return get(db)
