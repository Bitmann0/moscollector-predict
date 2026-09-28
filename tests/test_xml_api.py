"""XML в REST API (ТЗ §7): ответы по Accept, приём пачек, XSD, защита разбора.

Ответ в XML сверяется с JSON того же запроса поле за полем и проверяется по XSD из
contracts/xml/. Приём в XML сверяется с JSON по счётчикам партии и по отпечатку строк:
повтор тех же строк в другом формате уходит в дубли.
"""
import io
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import xmlschema
from app import xml_api
from app.main import create_app
from app.services.ml_client import get_ml_client
from conftest import DEMO_PASSWORD
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"
XML = {"Accept": "application/xml"}
XML_BODY = {"Content-Type": "application/xml"}
NIL = xml_api.NIL
DAY = "2026-06-30"

# Газовый датчик 9000004 синтетического справочника; значения — как в журнале СМВУ.
EVENTS = [
    {"ид_события": 1, "ид_канала_данных": 9000004, "дата": DAY, "время": "10:00:00",
     "тревожное": "t", "значение_датчика": "Обнаружен газ"},
    {"ид_события": 1, "ид_канала_данных": 9000004, "дата": DAY, "время": "10:00:00",
     "тревожное": "t", "значение_датчика": "Обнаружен газ"},          # дубль в пачке
    {"ид_события": 2, "ид_канала_данных": 9000004, "дата": DAY, "время": "10:05:00",
     "тревожное": "может быть", "значение_датчика": "Норма"},         # отклонена
    {"ид_события": 3, "ид_канала_данных": 9000004, "дата": "2026-07-02", "время": "10:00:00",
     "тревожное": "f", "значение_датчика": "Норма"},                  # вне демо-окна
    {"ид_события": 4, "ид_канала_данных": 9000001, "дата": DAY, "время": "11:00:00",
     "тревожное": "f", "значение_датчика": None},
    {"ид_события": 5, "ид_канала_данных": 9000001, "дата": DAY, "время": "11:30:00",
     "тревожное": "false", "значение_датчика": "0,4"},
]
ODS = [
    {"ts": "2026-06-29T10:00:00", "obj_id": "9101", "record_type": "осмотр",
     "decision": "dispatch_crew", "reason": "эмуляция ОДС: бригада на месте"},
    {"ts": "2026-06-29T10:00:00", "obj_id": "9101", "record_type": "осмотр",
     "decision": "dispatch_crew", "reason": "эмуляция ОДС: бригада на месте"},
    {"ts": "2026-06-29T12:30:00+03:00", "obj_id": None, "record_type": "закрытие"},
]
COUNTERS = ("kind", "rows_total", "accepted", "duplicates", "rejected", "outside_demo_window",
            "status")


@pytest.fixture(scope="module")
def responses_xsd() -> xmlschema.XMLSchema:
    return xmlschema.XMLSchema(str(ROOT / xml_api.RESPONSES_XSD))


@pytest.fixture(scope="module")
def ingest_xsd() -> xmlschema.XMLSchema:
    return xmlschema.XMLSchema(str(ROOT / xml_api.INGEST_XSD))


def rows_xml(model, rows: list[dict]) -> bytes:
    """Пачка JSON → тело XML по правилам приёма: корень <Модель>List, строки <Модель>."""
    top = ET.Element(xml_api.list_root(model))
    for row in rows:
        item = ET.SubElement(top, xml_api.schema_name(model))
        for key, value in row.items():
            field = ET.SubElement(item, key)
            if value is None:
                field.set(NIL, "true")
            else:
                field.text = str(value)
    return ET.tostring(top, encoding="utf-8", xml_declaration=True)


def counters(resp) -> dict:
    assert resp.status_code == 201, resp.text
    return {key: resp.json()[key] for key in COUNTERS}


def assert_same(element: ET.Element, data: dict, path: str = "") -> None:
    """Каждое поле JSON есть в XML в том же порядке и с тем же значением."""
    expected = [key for key, value in data.items()
                for _ in (value if isinstance(value, list) else [value])]
    assert [child.tag for child in element] == expected, path
    children = iter(element)
    for key, value in data.items():
        for item in value if isinstance(value, list) else [value]:
            child, where = next(children), f"{path}/{key}"
            if item is None:
                assert child.get(NIL) == "true" and len(child) == 0 and not child.text, where
            elif isinstance(item, dict):
                assert_same(child, item, where)
            elif isinstance(item, bool):
                assert child.text == ("true" if item else "false"), where
            elif isinstance(item, int | float):
                assert float(child.text) == item, where
            else:
                assert (child.text or "") == item, where


@pytest.fixture
def world(admin, ran, integration) -> tuple[TestClient, dict]:
    """Прогон 15.06, события 30.06 с кириллицей и null, id прогноза и заявки."""
    assert integration.post(f"{API}/ingest/events", json=EVENTS).status_code == 201
    forecast = admin.get(f"{API}/forecasts").json()["items"][0]["id"]
    order = admin.get(f"{API}/work-orders").json()["items"][0]["id"]
    return admin, {"forecast": forecast, "order": order}


ROUTES = [
    ("/forecasts", {"page_size": 20}, "Page_ForecastItem_"),
    ("/forecasts/summary", {}, "ForecastSummary"),
    ("/forecasts/{forecast}", {}, "ForecastCard"),
    ("/work-orders", {}, "Page_WorkOrderItem_"),
    ("/work-orders/{order}", {}, "WorkOrderCard"),
    ("/events", {"page_size": 50}, "Page_EventItem_"),
    ("/system/status", {}, "SystemStatus"),
    ("/quality", {"scenario": "sensor_link"}, "QualityOut"),
]


@pytest.mark.parametrize(("path", "params", "root"), ROUTES, ids=[r[0] for r in ROUTES])
def test_get_in_xml_is_valid_and_carries_json_data(world, responses_xsd, path, params, root):
    client, ids = world
    url = API + path.format(**ids)
    as_json = client.get(url, params=params)
    as_xml = client.get(url, params=params, headers=XML)
    assert as_json.status_code == as_xml.status_code == 200
    assert as_xml.headers["content-type"] == "application/xml; charset=utf-8"
    assert as_xml.content.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    responses_xsd.validate(io.BytesIO(as_xml.content))
    element = ET.fromstring(as_xml.content)
    assert element.tag == root
    assert_same(element, as_json.json())


def test_xml_spot_fields_and_readable_cyrillic(world):
    client, ids = world
    journal = client.get(f"{API}/forecasts", headers=XML)
    first = client.get(f"{API}/forecasts").json()["items"][0]
    item = ET.fromstring(journal.content).find("items")
    assert item.findtext("id") == first["id"]
    assert item.findtext("valid_from") == first["valid_from"]           # ISO 8601 как в JSON
    assert item.findtext("object/name") == first["object"]["name"]
    card = ET.fromstring(client.get(f"{API}/forecasts/{ids['forecast']}", headers=XML).content)
    assert card.find("decision").get(NIL) == "true"                     # null → xsi:nil
    events = client.get(f"{API}/events", params={"from": DAY, "to": DAY}, headers=XML)
    assert "Обнаружен газ".encode() in events.content and b"&#" not in events.content
    rows = ET.fromstring(events.content).findall("items")
    assert {row.findtext("sensor_event") for row in rows} >= {"Обнаружен газ", "0,4"}
    assert any(row.find("sensor_event").get(NIL) == "true" for row in rows)


@pytest.fixture
def plain_client(seeded, fake_ml):
    """То же приложение без слоя XML — эталон ответа JSON до правки."""
    app = create_app()
    app.user_middleware = [m for m in app.user_middleware if m.cls is not xml_api.XmlMiddleware]
    app.dependency_overrides[get_ml_client] = lambda: fake_ml
    with TestClient(app) as client:
        resp = client.post(f"{API}/auth/login", json={"login": "admin", "password": DEMO_PASSWORD})
        assert resp.status_code == 200
        yield client


@pytest.mark.parametrize("accept", [None, "*/*", "application/json",
                                    "text/html,application/xhtml+xml,*/*;q=0.8",
                                    "application/json, application/xml",
                                    "application/xml;q=0.5, application/json"])
def test_json_stays_default_and_unchanged(world, plain_client, accept):
    client, ids = world
    headers = {} if accept is None else {"Accept": accept}
    for path, params, _ in ROUTES:
        url = API + path.format(**ids)
        resp = client.get(url, params=params, headers=headers)
        assert resp.headers["content-type"] == "application/json"
        assert resp.content == plain_client.get(url, params=params).content, path
        assert "Accept" in resp.headers["vary"]


@pytest.mark.parametrize(("accept", "chosen"), [
    (None, None), ("", None), ("*/*", None), ("application/json", None),
    ("application/xml", "application/xml"), ("text/xml", "text/xml"),
    ("APPLICATION/XML; charset=utf-8", "application/xml"),
    ("application/xml, */*;q=0.8", "application/xml"),
    ("application/json, application/xml", None),
    ("application/json;q=0.5, application/xml", "application/xml"),
    ("application/xml;q=0", None), ("application/xml;q=abc", None),
])
def test_negotiation(accept, chosen):
    assert xml_api.negotiate(accept) == chosen


def test_errors_stay_json(world):
    client, _ = world
    resp = client.get(f"{API}/forecasts/nope", headers=XML)
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {"detail": "forecast_not_found"}


def test_text_outside_xml_charset_is_replaced(integration, admin):
    row = dict(EVENTS[0], ид_события=9, значение_датчика="сб\x01о\x08й")
    assert integration.post(f"{API}/ingest/events", json=[row]).json()["accepted"] == 1
    resp = admin.get(f"{API}/events", headers=XML)
    assert ET.fromstring(resp.content).findtext("items/sensor_event") == "сб\ufffdо\ufffdй"


def test_openapi_lists_xml_next_to_json():
    schema = create_app().openapi()
    for (method, path), model in xml_api.XML_ROUTES.items():
        operation = schema["paths"][path][method.lower()]
        code = next(code for code in operation["responses"] if code.startswith("2"))
        content = operation["responses"][code]["content"]
        ref = f"#/components/schemas/{xml_api.schema_name(model)}"
        assert content["application/json"]["schema"] == {"$ref": ref}, path
        assert content["application/xml"] == content["application/json"], path
    for path, model in xml_api.XML_INGEST.items():
        body = schema["paths"][path]["post"]["requestBody"]["content"]
        assert body["application/xml"]["schema"]["xml"]["name"] == xml_api.list_root(model)
        assert body["application/xml"]["schema"]["items"]["$ref"] == \
            body["application/json"]["schema"]["items"]["$ref"]


def test_committed_xsd_match_models():
    """contracts/xml/*.xsd пересобраны после правки моделей: python scripts/export_contracts.py."""
    for name, content in [(xml_api.RESPONSES_XSD, xml_api.response_xsd()),
                          (xml_api.INGEST_XSD, xml_api.ingest_xsd())]:
        committed = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        assert committed == content, name


# --- приём ---------------------------------------------------------------------

def test_xml_events_give_json_counters_and_same_rows(integration, admin, ingest_xsd):
    body = rows_xml(xml_api.XML_INGEST[f"{API}/ingest/events"], EVENTS)
    ingest_xsd.validate(io.BytesIO(body))
    from_json = counters(integration.post(f"{API}/ingest/events", json=EVENTS))
    stored_json = admin.get(f"{API}/events", params={"from": DAY, "to": DAY}).json()["items"]
    assert integration.delete(f"{API}/ingest/day/{DAY}").json()["deleted_events"] == 3

    from_xml = counters(integration.post(f"{API}/ingest/events", content=body,
                                         headers=XML_BODY))
    assert from_xml == from_json == {
        "kind": "smvu", "rows_total": 6, "accepted": 3, "duplicates": 1, "rejected": 1,
        "outside_demo_window": 1, "status": "partial"}
    stored_xml = admin.get(f"{API}/events", params={"from": DAY, "to": DAY}).json()["items"]
    drop_id = [{k: v for k, v in row.items() if k != "id"} for row in stored_json]
    assert [{k: v for k, v in row.items() if k != "id"} for row in stored_xml] == drop_id

    # Те же строки в JSON после XML — дубли: отпечаток строки от формата не зависит.
    again = counters(integration.post(f"{API}/ingest/events", json=EVENTS))
    assert (again["accepted"], again["duplicates"]) == (0, 4)


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_ods_counters_same_in_both_formats(integration, ingest_xsd, fmt):
    body = rows_xml(xml_api.XML_INGEST[f"{API}/ingest/ods-journal"], ODS)
    ingest_xsd.validate(io.BytesIO(body))
    kwargs = {"json": ODS} if fmt == "json" else {"content": body, "headers": XML_BODY}
    first = counters(integration.post(f"{API}/ingest/ods-journal", **kwargs))
    assert first == {"kind": "ods", "rows_total": 3, "accepted": 2, "duplicates": 1,
                     "rejected": 0, "outside_demo_window": 0, "status": "accepted"}
    other = {"content": body, "headers": XML_BODY} if fmt == "json" else {"json": ODS}
    second = counters(integration.post(f"{API}/ingest/ods-journal", **other))
    assert (second["accepted"], second["duplicates"]) == (0, 3)


def test_xml_ingest_answers_xml_on_request(integration, responses_xsd):
    body = rows_xml(xml_api.XML_INGEST[f"{API}/ingest/events"], EVENTS[:1])
    resp = integration.post(f"{API}/ingest/events", params={"notify": "false"}, content=body,
                            headers={"Content-Type": "text/xml; charset=utf-8", **XML})
    assert resp.status_code == 201
    responses_xsd.validate(io.BytesIO(resp.content))
    assert ET.fromstring(resp.content).findtext("accepted") == "1"


def test_example_file_is_accepted(integration, ingest_xsd):
    example = ROOT / "scripts" / "examples" / "ingest_events.xml"
    ingest_xsd.validate(str(example))
    resp = integration.post(f"{API}/ingest/events", params={"notify": "false"},
                            content=example.read_bytes(), headers=XML_BODY)
    assert counters(resp)["accepted"] == counters(resp)["rows_total"] > 0


XXE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE EventRowInList [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<EventRowInList><EventRowIn><ид_события>1</ид_события><ид_канала_данных>9000004</ид_канала_данных>
<дата>2026-06-30</дата><время>10:00:00</время><тревожное>t</тревожное>
<значение_датчика>&xxe;</значение_датчика></EventRowIn></EventRowInList>"""
LOL = "".join(f'<!ENTITY lol{i} "{f"&lol{i - 1};" * 10}">' for i in range(1, 10))
LAUGHS = ('<?xml version="1.0"?><!DOCTYPE EventRowInList [<!ENTITY lol0 "lol">'
          f"{LOL}]><EventRowInList>&lol9;</EventRowInList>")
HOSTILE = {
    "xxe_file": XXE,
    "billion_laughs": LAUGHS,
    "external_dtd": '<?xml version="1.0"?><!DOCTYPE EventRowInList SYSTEM '
                    '"http://127.0.0.1:9/evil.dtd"><EventRowInList/>',
    "parameter_entity": '<?xml version="1.0"?><!DOCTYPE EventRowInList [<!ENTITY % remote '
                        'SYSTEM "http://127.0.0.1:9/x.dtd"> %remote;]><EventRowInList/>',
    "internal_entity": '<?xml version="1.0"?><!DOCTYPE EventRowInList [<!ENTITY x "1">]>'
                       '<EventRowInList/>',
}


@pytest.mark.parametrize("path", sorted(xml_api.XML_INGEST))
@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_dtd_and_entities_are_rejected_before_rows(integration, admin, path, name):
    resp = integration.post(path, content=HOSTILE[name].encode(), headers=XML_BODY)
    assert resp.status_code == 400
    assert resp.json() == {"detail": "xml_forbidden"}
    assert admin.get(f"{API}/ingest/batches").json()["total"] == 0
    audit = admin.get(f"{API}/audit").json()["items"]
    assert any(row["path"] == path and row["status"] == 400 for row in audit)


@pytest.mark.parametrize(("body", "status", "detail"), [
    (b"<EventRowInList><EventRowIn>", 400, "xml_malformed"),
    ("<EventRowInList>ё</EventRowInList>".encode("cp1251"), 400, "xml_malformed"),
    (b"<OdsRowInList/>", 422, "xml_root_expected:EventRowInList"),
    (b"<EventRowInList><row/></EventRowInList>", 422, "xml_item_expected:EventRowIn"),
    (b"<EventRowInList><EventRowIn><a><b/></a></EventRowIn></EventRowInList>", 422,
     "xml_nested_field:a"),
    (b"<EventRowInList><EventRowIn><a>1</a><a>2</a></EventRowIn></EventRowInList>", 422,
     "xml_duplicate_field:a"),
])
def test_broken_xml_is_rejected(integration, body, status, detail):
    resp = integration.post(f"{API}/ingest/events", content=body, headers=XML_BODY)
    assert (resp.status_code, resp.json()) == (status, {"detail": detail})


def test_invalid_xml_row_fails_validation_like_json(integration):
    row = dict(EVENTS[0], ид_события="не число")
    as_json = integration.post(f"{API}/ingest/events", json=[row])
    as_xml = integration.post(f"{API}/ingest/events", headers=XML_BODY,
                              content=rows_xml(xml_api.XML_INGEST[f"{API}/ingest/events"],
                                               [row]))
    assert as_json.status_code == as_xml.status_code == 422
    assert as_xml.json()["detail"][0]["loc"] == as_json.json()["detail"][0]["loc"] == [
        "body", 0, "ид_события"]


def test_xml_batch_limit_is_5000_rows_like_json(integration):
    model = xml_api.XML_INGEST[f"{API}/ingest/events"]
    full = integration.post(f"{API}/ingest/events", content=rows_xml(model, EVENTS[:1] * 5000),
                            headers=XML_BODY, params={"notify": "false"})
    assert (counters(full)["accepted"], counters(full)["duplicates"]) == (1, 4999)
    over = integration.post(f"{API}/ingest/events", content=rows_xml(model, EVENTS[:1] * 5001),
                            headers=XML_BODY)
    assert over.status_code == 422


def test_declared_xml_body_over_limit_is_413(integration):
    resp = integration.post(f"{API}/ingest/events", content=b"<EventRowInList/>",
                            headers={**XML_BODY, "Content-Length": str(11 * 1024 * 1024)})
    assert resp.status_code == 413


def test_streamed_xml_body_over_limit_is_413(integration):
    def chunks():
        yield b"<EventRowInList>"
        for _ in range(11):
            yield b" " * (1024 * 1024)
        yield b"</EventRowInList>"

    resp = integration.post(f"{API}/ingest/events", content=chunks(), headers=XML_BODY)
    assert resp.status_code == 413
