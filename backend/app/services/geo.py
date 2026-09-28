"""Условная схема коллекторов (ТЗ §7: GeoJSON и WKT).

Координат заказчик не дал, поэтому система координат условная — под Leaflet CRS.Simple
и любой просмотрщик WKT, ось y смотрит вверх:
- x — пикет, одна единица на пикет;
- y — строка комплекса: комплексы по возрастанию id (цифровые id сравниваются как
  числа), n-й лежит на y = −(n − 1)·row_step; k-й объект комплекса (по id) — на
  дорожке y = строка − k·LANE_STEP, его каналы — на той же дорожке.

Комплекс — LineString вдоль своей строки от наименьшего до наибольшего пикета своих
каналов. Объект — Point в середине диапазона пикетов своих каналов, сам диапазон — в
picket_min и picket_max; объект без единого канала с пикетом стоит в начале строки
комплекса с placement="complex_start". Канал — Point на своём пикете. Канал без пикета
не рисуется, а считается в channels_without_picket объекта, комплекса и коллекции.

Каналы отдаются только с фильтром complex: в реальном справочнике их 11 485, а без
фильтра ответ — только комплексы и объекты (16 и 78). Координаты от фильтра не
зависят: строка и дорожка считаются по всему справочнику.

open_forecasts — число открытых прогнозов по helpers.open_forecast_clauses, тому же
правилу, что у дашборда. risk_level: "unknown" — не было ни одного расчёта, "high" —
есть открытый прогноз, "none" — нет. Порога по risk нет: у sensor_link, fire_risk и
flood_risk это вероятность, у equipment_diag и guard_weekly — относительный приоритет,
общей шкалы нет. Прогнозы без канала (недельная очередь, пожарный риск участка,
подтопление) считаются в open_forecasts объекта и комплекса, но не канала.
"""
from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.reference import FeatureCollection, GeoFeature
from . import settings_store
from .helpers import open_forecast_clauses

NOTE = "условная схема, не географические координаты"
COMPLEX_LEVEL = 2
ROW_STEP = 100.0
LANE_STEP = 6.0

Shape = tuple[str, list, dict]  # тип геометрии, координаты, properties
Stats = tuple[int, int, float | None, float | None]  # каналов, из них без пикета, мин, макс
NO_STATS: Stats = (0, 0, None, None)


def _order(obj_id: str) -> tuple:
    return (0, int(obj_id), "") if obj_id.isdigit() else (1, 0, obj_id)


def _num(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _wkt(kind: str, coords: list) -> str:
    if kind == "LineString":
        return "LINESTRING (" + ", ".join(f"{_num(x)} {_num(y)}" for x, y in coords) + ")"
    return f"POINT ({_num(coords[0])} {_num(coords[1])})"


def _shape(kind: str, coords: list, props: dict) -> Shape:
    return kind, coords, {**props, "wkt": _wkt(kind, coords), "source": "live"}


def _stats_by_object(db: Session) -> dict[str, Stats]:
    rows = db.execute(select(models.RefChannel.obj_id, func.count(),
                             func.count(models.RefChannel.picket),
                             func.min(models.RefChannel.picket),
                             func.max(models.RefChannel.picket))
                      .group_by(models.RefChannel.obj_id))
    return {obj_id: (n, n - placed, lo, hi) for obj_id, n, placed, lo, hi in rows}


def _stats_from_rows(channels: dict[str, list]) -> dict[str, Stats]:
    out = {}
    for obj_id, rows in channels.items():
        pickets = [ch.picket for ch in rows if ch.picket is not None]
        out[obj_id] = (len(rows), len(rows) - len(pickets), min(pickets, default=None),
                       max(pickets, default=None))
    return out


def _open_counts(db: Session) -> tuple[Counter, Counter, str]:
    """Открытые прогнозы по объекту и по каналу одним запросом и demo_today правила."""
    today = settings_store.demo_today(db)
    by_obj: Counter = Counter()
    by_channel: Counter = Counter()
    rows = db.execute(select(models.Forecast.obj_id, models.Forecast.channel_id, func.count())
                      .where(*open_forecast_clauses(today))
                      .group_by(models.Forecast.obj_id, models.Forecast.channel_id))
    for obj_id, channel_id, n in rows:
        if obj_id is not None:
            by_obj[obj_id] += n
        if channel_id is not None:
            by_channel[channel_id] += n
    return by_obj, by_channel, today.isoformat()


def _layout(db: Session, complex_id: str | None) -> tuple[list[Shape], dict]:
    """Фигуры схемы и счётчики для properties коллекции.

    По одному запросу к ref_objects, ref_channels, forecasts и forecast_runs: число
    запросов от размера справочника не зависит (tests/test_geo.py).
    """
    objects = sorted(db.scalars(select(models.RefObject)), key=lambda o: _order(o.id))
    complexes = [o for o in objects if o.level == COMPLEX_LEVEL]
    members: dict[str | None, list[models.RefObject]] = defaultdict(list)
    for o in objects:
        members[o.parent_id].append(o)
    row_step = max(ROW_STEP, LANE_STEP * (1 + max((len(members[c.id]) for c in complexes),
                                                  default=0)))
    chosen = [(n, c) for n, c in enumerate(complexes, start=1)
              if complex_id is None or c.id == complex_id]
    with_channels = complex_id is not None
    channels: dict[str, list] = defaultdict(list)
    if with_channels:
        ids = [o.id for _, c in chosen for o in [c, *members[c.id]]]
        for ch in db.execute(
                select(models.RefChannel.id, models.RefChannel.obj_id, models.RefChannel.name,
                       models.RefChannel.sensor_type, models.RefChannel.picket)
                .where(models.RefChannel.obj_id.in_(ids)).order_by(models.RefChannel.id)):
            channels[ch.obj_id].append(ch)
        stats = _stats_from_rows(channels)
    else:
        stats = _stats_by_object(db)
    by_obj, by_channel, today = _open_counts(db)
    computed = db.scalar(select(models.ForecastRun.id).limit(1)) is not None

    def risk(n: int) -> str:
        return "high" if n else ("none" if computed else "unknown")

    shapes: list[Shape] = []
    meta = dict.fromkeys(["complexes", "objects", "channels", "channels_without_picket",
                          "channels_drawn", "open_forecasts"], 0)
    for number, cx in chosen:
        row_y = (1 - number) * row_step
        group = [cx, *members[cx.id]]
        lane = {cx.id: row_y} | {o.id: row_y - k * LANE_STEP
                                 for k, o in enumerate(members[cx.id], start=1)}
        own = [stats.get(o.id, NO_STATS) for o in group]
        lows = [s[2] for s in own if s[2] is not None]
        highs = [s[3] for s in own if s[3] is not None]
        start, end = (min(lows), max(highs)) if lows else (0.0, 0.0)
        total, unplaced = sum(s[0] for s in own), sum(s[1] for s in own)
        open_total = sum(by_obj[o.id] for o in group)
        shapes.append(_shape("LineString", [[start, row_y], [end, row_y]], {
            "feature": "complex", "id": cx.id, "name": cx.name, "kind": cx.kind,
            "level": cx.level, "complex_number": number, "channels": total,
            "channels_without_picket": unplaced, "picket_min": start if lows else None,
            "picket_max": end if lows else None, "open_forecasts": open_total,
            "risk_level": risk(open_total)}))
        for o in members[cx.id]:
            n, no_picket, lo, hi = stats.get(o.id, NO_STATS)
            placed = lo is not None
            shapes.append(_shape("Point", [(lo + hi) / 2 if placed else start, lane[o.id]], {
                "feature": "object", "id": o.id, "name": o.name, "kind": o.kind,
                "level": o.level, "complex_id": cx.id, "complex_number": number,
                "channels": n, "channels_without_picket": no_picket, "picket_min": lo,
                "picket_max": hi, "placement": "pickets" if placed else "complex_start",
                "open_forecasts": by_obj[o.id], "risk_level": risk(by_obj[o.id])}))
        for o in group:
            for ch in channels[o.id]:
                if ch.picket is None:
                    continue
                shapes.append(_shape("Point", [ch.picket, lane[o.id]], {
                    "feature": "channel", "id": ch.id, "name": ch.name,
                    "sensor_type": ch.sensor_type, "picket": ch.picket, "obj_id": o.id,
                    "complex_id": cx.id, "open_forecasts": by_channel[ch.id],
                    "risk_level": risk(by_channel[ch.id])}))
                meta["channels_drawn"] += 1
        meta["complexes"] += 1
        meta["objects"] += len(members[cx.id])
        meta["channels"] += total
        meta["channels_without_picket"] += unplaced
        meta["open_forecasts"] += open_total
    return shapes, {**meta, "with_channels": with_channels, "demo_today": today}


def schema_geojson(db: Session, complex_id: str | None) -> FeatureCollection:
    shapes, meta = _layout(db, complex_id)
    features = [GeoFeature(geometry={"type": kind, "coordinates": coords}, properties=props)
                for kind, coords, props in shapes]
    return FeatureCollection(features=features, properties={
        "note": NOTE, "source": "live", "complex": complex_id, **meta})


def schema_wkt(db: Session, complex_id: str | None) -> str:
    shapes, _ = _layout(db, complex_id)
    parts = [props["wkt"] for _, _, props in shapes]
    return f"GEOMETRYCOLLECTION ({', '.join(parts)})" if parts else "GEOMETRYCOLLECTION EMPTY"
