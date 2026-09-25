"""Сигнатура сервиса (владелец ML1-11). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.reference import FeatureCollection


def schema_geojson(db: Session, complex_id: str | None) -> FeatureCollection:
    raise NotImplementedError("каркас: задача 3")


def schema_wkt(db: Session, complex_id: str | None) -> str:
    raise NotImplementedError("каркас: задача 3")
