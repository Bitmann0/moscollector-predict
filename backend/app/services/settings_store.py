"""Демо-дата, архивный/replay-режим и скорость воспроизведения."""
from datetime import date

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..schemas.misc import SettingsIn, SettingsOut
from ..security import CurrentUser

DEMO_START = date(2026, 6, 1)
DEMO_END = date(2026, 6, 30)


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
    if body.demo_today is not None and not DEMO_START <= body.demo_today <= DEMO_END:
        raise HTTPException(status_code=422, detail="demo_today_outside_available_window")
    changes = body.model_dump(mode="json", exclude_none=True)
    for key, value in changes.items():
        row = db.get(models.Setting, key)
        if row is None:
            db.add(models.Setting(key=key, value={"value": value}))
        else:
            row.value = {"value": value}
    db.commit()
    return get(db)
