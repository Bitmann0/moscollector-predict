"""Условная схема коллекторов (ТЗ §7: GeoJSON и WKT).

ЗАГЛУШКА — владелец ML1-11 (C2).
Заменить: геометрию из схемы коллекторов заказчика — сейчас координаты условные:
x = номер комплекса × 100 (комплексы по возрастанию id, с 1), y = пикет. Комплекс —
LineString от минимального до максимального пикета его каналов, объект — Point на
минимальном пикете своих каналов (0, если каналов нет).
Контракт: schema_geojson и schema_wkt не меняются, в properties коллекции остаётся
note; тест tests/test_endpoints_shape.py должен остаться зелёным.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.reference import FeatureCollection, GeoFeature
from .reference import channel_stats

NOTE = "условная схема, не географические координаты"
COMPLEX_LEVEL = 2
X_STEP = 100


def _layout(db: Session, complex_id: str | None) -> list[tuple[str, list[float], dict]]:
    """(тип геометрии, координаты, properties) по комплексам и их объектам."""
    objects = list(db.scalars(select(models.RefObject).order_by(models.RefObject.id)))
    stats = channel_stats(db)
    complexes = [o for o in objects if o.level == COMPLEX_LEVEL]
    shapes = []
    for number, cx in enumerate(complexes, start=1):
        if complex_id is not None and cx.id != complex_id:
            continue
        x = float(number * X_STEP)
        members = [o for o in objects if o.parent_id == cx.id]
        pickets = [v for o in [cx, *members] for v in stats.get(o.id, (0, None, None))[1:]
                   if v is not None]
        lo, hi = (min(pickets), max(pickets)) if pickets else (0.0, 0.0)
        shapes.append(("LineString", [[x, lo], [x, hi]],
                       {"id": cx.id, "name": cx.name, "kind": cx.kind, "level": cx.level,
                        "complex_number": number, "source": "stub"}))
        for o in members:
            n, low, _ = stats.get(o.id, (0, None, None))
            shapes.append(("Point", [x, low if low is not None else 0.0],
                           {"id": o.id, "name": o.name, "kind": o.kind, "level": o.level,
                            "complex_id": cx.id, "channels": n, "source": "stub"}))
    return shapes


def schema_geojson(db: Session, complex_id: str | None) -> FeatureCollection:
    features = [GeoFeature(geometry={"type": kind, "coordinates": coords}, properties=props)
                for kind, coords, props in _layout(db, complex_id)]
    return FeatureCollection(features=features,
                             properties={"note": NOTE, "source": "stub", "complex": complex_id})


def _wkt_point(xy: list[float]) -> str:
    return f"{xy[0]:g} {xy[1]:g}"


def schema_wkt(db: Session, complex_id: str | None) -> str:
    parts = []
    for kind, coords, _ in _layout(db, complex_id):
        if kind == "LineString":
            parts.append("LINESTRING (" + ", ".join(_wkt_point(p) for p in coords) + ")")
        else:
            parts.append(f"POINT ({_wkt_point(coords)})")
    return f"GEOMETRYCOLLECTION ({', '.join(parts)})" if parts else "GEOMETRYCOLLECTION EMPTY"
