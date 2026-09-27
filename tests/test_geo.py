"""Условная схема (ML1-11): геометрия, WKT, каналы без пикета, открытые прогнозы.

Справочник — синтетический из seed: комплексы 9100 и 9200 по три объекта, у каждого
объекта пять каналов с пикетами 0, 8, …, 112 по порядку.
"""
import re

from app import models
from app.db import get_engine
from app.services import geo
from sqlalchemy import event

API = "/api/v1"
NOTE = "условная схема, не географические координаты"
WKT_PART = r"(?:POINT \(-?[\d.]+ -?[\d.]+\)|LINESTRING \(-?[\d.]+ -?[\d.]+(?:, -?[\d.]+ -?[\d.]+)+\))"


def _by_id(collection: dict) -> dict:
    return {(f["properties"]["feature"], str(f["properties"]["id"])): f
            for f in collection["features"]}


def _wkt_numbers(wkt: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?[\d.]+", wkt)]


def _add_unplaced(db) -> None:
    """Канал без пикета у 9101 и объект 9104, у которого пикетов нет вовсе."""
    db.add(models.RefObject(id="9104", level=3, parent_id="9100", kind="guardObject",
                            name="Объект-заглушка без пикета"))
    db.add_all([
        models.RefChannel(id=9000031, obj_id="9101", sensor_type="ИБП", name="ИБП (заглушка)"),
        models.RefChannel(id=9000032, obj_id="9104", sensor_type="КД Дверь",
                          name="КД Дверь (заглушка)"),
    ])
    db.commit()


def test_geojson_layout_and_note(admin):
    geo_json = admin.get(f"{API}/schema.geojson").json()
    props = geo_json["properties"]
    assert props["note"] == NOTE
    assert (props["complexes"], props["objects"], props["channels"]) == (2, 6, 30)
    assert props["with_channels"] is False and props["channels_drawn"] == 0
    features = _by_id(geo_json)
    assert {kind for kind, _ in features} == {"complex", "object"}
    for (kind, _), feature in features.items():
        geometry, wkt = feature["geometry"], feature["properties"]["wkt"]
        assert geometry["type"] == ("LineString" if kind == "complex" else "Point")
        assert wkt.startswith(geometry["type"].upper())
        coords = geometry["coordinates"]
        assert _wkt_numbers(wkt) == (coords if kind == "object" else
                                     [c for point in coords for c in point])
    # Комплексы — строки y = 0 и y = −100, x — пикет; объект — середина своих пикетов.
    assert features[("complex", "9100")]["geometry"]["coordinates"] == [[0, 0], [112, 0]]
    assert features[("complex", "9200")]["geometry"]["coordinates"] == [[0, -100], [112, -100]]
    first = features[("object", "9101")]
    assert first["geometry"]["coordinates"] == [16, -6]
    assert (first["properties"]["picket_min"], first["properties"]["picket_max"]) == (0, 32)
    assert first["properties"]["placement"] == "pickets"
    assert features[("object", "9103")]["geometry"]["coordinates"] == [96, -18]


def test_wkt_endpoint_matches_features(admin):
    text = admin.get(f"{API}/schema.wkt").text
    assert re.fullmatch(rf"GEOMETRYCOLLECTION \({WKT_PART}(?:, {WKT_PART})*\)", text), text
    features = admin.get(f"{API}/schema.geojson").json()["features"]
    assert text == "GEOMETRYCOLLECTION (" + ", ".join(
        f["properties"]["wkt"] for f in features) + ")"
    assert admin.get(f"{API}/schema.wkt", params={"complex": "нет"}).text == (
        "GEOMETRYCOLLECTION EMPTY")


def test_channels_without_picket_are_counted_not_drawn(seeded, admin):
    _add_unplaced(seeded)
    full = admin.get(f"{API}/schema.geojson").json()
    assert full["properties"]["channels_without_picket"] == 2
    features = _by_id(full)
    assert features[("complex", "9100")]["properties"]["channels_without_picket"] == 2
    assert features[("complex", "9200")]["properties"]["channels_without_picket"] == 0
    assert features[("object", "9101")]["properties"]["channels_without_picket"] == 1
    lonely = features[("object", "9104")]
    assert lonely["properties"]["placement"] == "complex_start"
    assert lonely["properties"]["picket_min"] is None
    assert lonely["geometry"]["coordinates"] == [0, -24]  # начало строки, четвёртая дорожка

    one = admin.get(f"{API}/schema.geojson", params={"complex": "9100"}).json()
    assert one["properties"]["with_channels"] is True
    assert (one["properties"]["complexes"], one["properties"]["objects"]) == (1, 4)
    channels = [f for f in one["features"] if f["properties"]["feature"] == "channel"]
    assert one["properties"]["channels_drawn"] == len(channels) == 15
    assert {f["properties"]["id"] for f in channels} == set(range(9000001, 9000016))
    lanes = {f["properties"]["id"]: f["geometry"]["coordinates"][1]
             for f in one["features"] if f["properties"]["feature"] == "object"}
    for f in channels:
        x, y = f["geometry"]["coordinates"]
        assert x == f["properties"]["picket"] and y == lanes[f["properties"]["obj_id"]]
        assert f["properties"]["wkt"] == f"POINT ({x:g} {y:g})"
    # Координаты от фильтра не зависят.
    same = _by_id(one)
    for key in [("complex", "9100"), ("object", "9101"), ("object", "9104")]:
        assert same[key]["geometry"] == features[key]["geometry"]
    wkt = admin.get(f"{API}/schema.wkt", params={"complex": "9100"}).text
    assert wkt.count("POINT") == 4 + 15 and wkt.count("LINESTRING") == 1


def test_risk_and_open_forecasts_follow_dashboard(admin, ran):
    before = _by_id(admin.get(f"{API}/schema.geojson").json())
    assert before[("object", "9101")]["properties"]["risk_level"] == "none"  # окна кончились
    assert admin.put(f"{API}/settings", json={"demo_today": "2026-06-16"}).status_code == 200
    collection = admin.get(f"{API}/schema.geojson").json()
    features = _by_id(collection)

    def opened(kind: str, obj: str) -> tuple:
        props = features[(kind, obj)]["properties"]
        return props["open_forecasts"], props["risk_level"]

    # A_link по 9101 и 9102, D по 9201, недельная очередь по 9201 и 9101; 9103 вне бюджета.
    assert opened("object", "9101") == (2, "high")
    assert opened("object", "9102") == (1, "high")
    assert opened("object", "9103") == (0, "none")
    assert opened("object", "9201") == (2, "high")
    assert opened("complex", "9100") == (3, "high")
    assert opened("complex", "9200") == (2, "high")
    kpi = admin.get(f"{API}/dashboard/summary").json()["scenarios"]
    assert collection["properties"]["open_forecasts"] == sum(s["open_forecasts"] for s in kpi)
    assert collection["properties"]["demo_today"] == "2026-06-16"

    channels = _by_id(admin.get(f"{API}/schema.geojson", params={"complex": "9100"}).json())
    assert channels[("channel", "9000001")]["properties"]["open_forecasts"] == 1
    assert channels[("channel", "9000002")]["properties"]["risk_level"] == "none"

    items = admin.get(f"{API}/forecasts", params={"obj": "9102"}).json()["items"]
    admin.post(f"{API}/forecasts/{items[0]['id']}/decisions",
               json={"action": "defer", "reason_code": "await_data"})
    after = _by_id(admin.get(f"{API}/schema.geojson").json())
    assert after[("object", "9102")]["properties"]["risk_level"] == "none"
    assert after[("complex", "9100")]["properties"]["open_forecasts"] == 2


def test_risk_is_unknown_before_first_run(admin):
    props = _by_id(admin.get(f"{API}/schema.geojson").json())[("object", "9101")]["properties"]
    assert (props["open_forecasts"], props["risk_level"]) == (0, "unknown")


def test_query_count_does_not_grow_with_reference(seeded):
    """Запросов столько же, сколько таблиц, при любом числе объектов и каналов."""
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        def queries(complex_id):
            seeded.expire_all()
            statements.clear()
            geo.schema_geojson(seeded, complex_id)
            return len(statements)

        small = queries(None), queries("9100")
        for n in range(4):
            seeded.add(models.RefObject(id=str(9110 + n), level=3, parent_id="9100",
                                        kind="guardObject", name=f"Объект {n}"))
            seeded.add_all(models.RefChannel(id=9100000 + 100 * n + i, obj_id=str(9110 + n),
                                             picket=float(i)) for i in range(50))
        seeded.commit()
        assert (queries(None), queries("9100")) == small
    finally:
        event.remove(engine, "before_cursor_execute", record)
