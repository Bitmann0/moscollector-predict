"""Сигнатура сервиса (владелец BE-03). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.reference import ReasonCodeOut, SyncReport, TreeNode
from ..security import CurrentUser


def reason_codes(db: Session) -> list[ReasonCodeOut]:
    raise NotImplementedError("каркас: задача 3")


def tree(db: Session) -> list[TreeNode]:
    raise NotImplementedError("каркас: задача 3")


def sync(db: Session, user: CurrentUser) -> SyncReport:
    raise NotImplementedError("каркас: задача 3")
