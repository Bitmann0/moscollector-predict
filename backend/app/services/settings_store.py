"""Сигнатура сервиса (владелец BE-05). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.misc import SettingsIn, SettingsOut
from ..security import CurrentUser


def get(db: Session) -> SettingsOut:
    raise NotImplementedError("каркас: задача 3")


def put(db: Session, body: SettingsIn, user: CurrentUser) -> SettingsOut:
    raise NotImplementedError("каркас: задача 3")
