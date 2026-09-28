"""XML в REST API (ТЗ §7): ответы по Accept, приём пачек в XML, XSD. Живое.

Весь XML — в этом модуле, роутеры о нём не знают. XmlMiddleware стоит внутри
BodyLimitMiddleware и AuditMiddleware (backend/app/main.py): предел тела и аудит
работают для XML так же, как для JSON.

Ответ. Маршрут из XML_ROUTES отвечает XML, если в Accept у application/xml или text/xml
вес больше, чем у явно названного application/json. Без Accept, с */* и при равных
весах ответ — JSON, как раньше. FastAPI собирает ответ обычным путём, слой разбирает
готовый JSON и пишет те же значения элементами: даты и время — те же строки ISO 8601,
числа — те же лексемы, что в JSON. Ответы не 2xx остаются JSON {"detail": …}.

Правила отображения; им же следуют XSD в contracts/xml/:
- корень — имя схемы модели ответа в OpenAPI: ForecastSummary, Page_ForecastItem_;
- поле модели — дочерний элемент с именем поля, в порядке полей;
- список — элемент с именем поля, повторённый по разу на значение; у пустого списка
  элементов нет;
- null — пустой элемент с xsi:nil="true"; пустая строка — пустой элемент без него;
- логические значения — true и false.

Приём. POST /ingest/events и /ingest/ods-journal с Content-Type application/xml или
text/xml: корень EventRowInList (OdsRowInList), в нём повторяющиеся EventRowIn (OdsRowIn),
в них элементы с теми же именами, что ключи JSON. defusedxml запрещает DTD, сущности и
внешние ссылки: XXE и «billion laughs» получают 400 до разбора строк. Строки
становятся JSON-массивом, и дальше запрос идёт путём JSON: Body(max_length=5000),
валидация pydantic, дедупликация в services/ingest.py.
"""
import json
import re
import types
from datetime import date, datetime
from typing import Literal, Union, get_args, get_origin
from xml.etree import ElementTree as ET

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders
from starlette.routing import compile_path

from .limits import _send_json
from .routers.ingest import MAX_BATCH_ROWS
from .schemas.common import Page
from .schemas.events import EventItem, EventRowIn, IngestBatchOut, OdsRowIn
from .schemas.forecasts import ForecastCard, ForecastItem, ForecastSummary
from .schemas.misc import QualityOut
from .schemas.system import SystemStatus
from .schemas.work_orders import WorkOrderCard, WorkOrderItem

API = "/api/v1"
# (метод, путь) → модель ответа, как response_model в роутере; тест сверяет её с OpenAPI.
# Порядок важен: /forecasts/summary раньше /forecasts/{forecast_id}, как в роутере.
XML_ROUTES: dict[tuple[str, str], type[BaseModel]] = {
    ("GET", f"{API}/forecasts"): Page[ForecastItem],
    ("GET", f"{API}/forecasts/summary"): ForecastSummary,
    ("GET", f"{API}/forecasts/{{forecast_id}}"): ForecastCard,
    ("GET", f"{API}/work-orders"): Page[WorkOrderItem],
    ("GET", f"{API}/work-orders/{{order_id}}"): WorkOrderCard,
    ("GET", f"{API}/events"): Page[EventItem],
    ("GET", f"{API}/system/status"): SystemStatus,
    ("GET", f"{API}/quality"): QualityOut,
    ("POST", f"{API}/ingest/events"): IngestBatchOut,
    ("POST", f"{API}/ingest/ods-journal"): IngestBatchOut,
}
XML_INGEST: dict[str, type[BaseModel]] = {f"{API}/ingest/events": EventRowIn,
                                          f"{API}/ingest/ods-journal": OdsRowIn}
XML_TYPES = ("application/xml", "text/xml")
RESPONSES_XSD = "contracts/xml/api_v1_responses.xsd"
INGEST_XSD = "contracts/xml/api_v1_ingest.xsd"

XS = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
NIL = f"{{{XSI}}}nil"
ET.register_namespace("xs", XS)
ET.register_namespace("xsi", XSI)
# Символы, которых нет в XML 1.0 даже как ссылок: управляющие и одиночные суррогаты.
_NOT_XML = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def schema_name(model: type[BaseModel]) -> str:
    """Имя схемы модели в OpenAPI: pydantic меняет «[» и «]» на «_», Page[EventItem] →
    Page_EventItem_. Тест сверяет его с $ref в OpenAPI."""
    return re.sub(r"[^a-zA-Z0-9.\-_]", "_", model.__name__)


def list_root(model: type[BaseModel]) -> str:
    return f"{schema_name(model)}List"


def negotiate(accept: str | None) -> str | None:
    """Медиатип XML, если клиент предпочёл его JSON, иначе None — ответ JSON.

    */* и application/* за JSON не считаются: клиент, назвавший XML явно, получает XML.
    При равных весах XML и application/json выбирается JSON."""
    xml_q, xml_type, json_q = 0.0, None, 0.0
    for part in (accept or "").split(","):
        media, *params = (piece.strip() for piece in part.split(";"))
        q = 1.0
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        media = media.lower()
        if media in XML_TYPES and q > xml_q:
            xml_q, xml_type = q, media
        elif media == "application/json":
            json_q = max(json_q, q)
    return xml_type if xml_q > json_q else None


def _media(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


# --- JSON → XML ------------------------------------------------------------------

def _to_bytes(element: ET.Element) -> bytes:
    ET.indent(element)
    text = ET.tostring(element, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{text}\n'.encode()


def _text(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return _NOT_XML.sub("\ufffd", str(value))


def _fill(element: ET.Element, value) -> None:
    if value is None:
        element.set(NIL, "true")
    elif isinstance(value, dict):
        for key, item in value.items():
            for one in item if isinstance(item, list) else [item]:
                if isinstance(one, list):
                    raise TypeError(f"{key}: список списков в XML не отображается")
                _fill(ET.SubElement(element, key), one)
    else:
        element.text = _text(value)


def json_to_xml(body: bytes, root: str) -> bytes:
    """Готовый JSON-ответ → XML. Числа не переформатируются: parse_int и parse_float
    оставляют лексему из JSON."""
    data = json.loads(body, parse_int=str, parse_float=str)
    if not isinstance(data, dict):
        raise TypeError("корень ответа для XML — объект JSON")
    element = ET.Element(root)
    _fill(element, data)
    return _to_bytes(element)


# --- XML → JSON ------------------------------------------------------------------

class XmlRejected(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_rows(body: bytes, model: type[BaseModel]) -> list[dict]:
    """Пачка в XML → список словарей с ключами JSON. Значение — текст элемента как
    есть; типы, обязательность и длину проверяет pydantic, как у JSON."""
    try:
        root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:  # DTD, сущность, внешняя ссылка
        raise XmlRejected(400, "xml_forbidden") from exc
    except ParseError as exc:
        raise XmlRejected(400, "xml_malformed") from exc
    item_name = schema_name(model)
    if root.tag != list_root(model):
        raise XmlRejected(422, f"xml_root_expected:{list_root(model)}")
    rows = []
    for item in root:
        if item.tag != item_name:
            raise XmlRejected(422, f"xml_item_expected:{item_name}")
        row: dict[str, str | None] = {}
        for field in item:
            if len(field):
                raise XmlRejected(422, f"xml_nested_field:{field.tag}")
            if field.tag in row:
                raise XmlRejected(422, f"xml_duplicate_field:{field.tag}")
            row[field.tag] = None if field.get(NIL) in ("true", "1") else (field.text or "")
        rows.append(row)
    return rows


# --- ASGI-слой -------------------------------------------------------------------

async def _read_body(receive) -> bytes | None:
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(scope, receive, body: bytes):
    """Тело запроса подменяется JSON-массивом; дальше receive отдаёт исходный поток."""
    headers = MutableHeaders(scope=scope)
    headers["content-type"] = "application/json"
    headers["content-length"] = str(len(body))
    if "transfer-encoding" in headers:
        del headers["transfer-encoding"]
    sent = False

    async def replay():
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _sender(send, xml_type: str | None, root: str):
    """Добавляет Vary: Accept; при выбранном XML копит ответ 2xx в JSON и отдаёт XML."""
    start: dict | None = None
    chunks: list[bytes] = []

    async def wrapped(message) -> None:
        nonlocal start
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers.add_vary_header("Accept")
            if xml_type and 200 <= message["status"] < 300 \
                    and _media(headers.get("content-type")) == "application/json":
                start = message
                return
            await send(message)
            return
        if start is None or message["type"] != "http.response.body":
            await send(message)
            return
        chunks.append(message.get("body", b""))
        if message.get("more_body", False):
            return
        body = json_to_xml(b"".join(chunks), root)
        headers = MutableHeaders(scope=start)
        headers["content-type"] = f"{xml_type}; charset=utf-8"
        headers["content-length"] = str(len(body))
        await send(start)
        await send({"type": "http.response.body", "body": body})

    return wrapped


_PATTERNS = [(method, path, compile_path(path)[0]) for method, path in XML_ROUTES]


def match_route(method: str, path: str) -> str | None:
    """Шаблон пути из XML_ROUTES, которому отвечает запрос, или None."""
    for known_method, template, regex in _PATTERNS:
        if known_method == method and regex.match(path):
            return template
    return None


class XmlMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        template = match_route(scope["method"], scope["path"])
        if template is None:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        model = XML_INGEST.get(template) if scope["method"] == "POST" else None
        if model is not None and _media(headers.get("content-type")) in XML_TYPES:
            body = await _read_body(receive)
            if body is None:  # BodyLimitMiddleware уже ответил 413, или клиент ушёл
                return
            try:
                rows = parse_rows(body, model)
            except XmlRejected as exc:
                return await _send_json(send, exc.status, exc.detail)
            receive = _replay(scope, receive, json.dumps(rows, ensure_ascii=False).encode())
        root = schema_name(XML_ROUTES[(scope["method"], template)])
        await self.app(scope, receive, _sender(send, negotiate(headers.get("accept")), root))


# --- OpenAPI ---------------------------------------------------------------------

def add_openapi(schema: dict) -> None:
    """application/xml рядом с application/json у маршрутов XML_ROUTES."""
    for method, path in XML_ROUTES:
        operation = schema["paths"][path][method.lower()]
        for code, response in operation["responses"].items():
            content = response.get("content", {})
            if code.startswith("2") and "application/json" in content:
                content["application/xml"] = {"schema": content["application/json"]["schema"]}
        note = f"XML: заголовок `Accept: application/xml`, схема `{RESPONSES_XSD}`."
        model = XML_INGEST.get(path)
        if model is not None:
            body = operation["requestBody"]["content"]
            array = dict(body["application/json"]["schema"])
            array["xml"] = {"name": list_root(model), "wrapped": True}
            array["items"] = {**array["items"], "xml": {"name": schema_name(model)}}
            body["application/xml"] = {"schema": array}
            note = (f"Тело в XML: `Content-Type: application/xml`, корень `{list_root(model)}`, "
                    f"схема `{INGEST_XSD}`. Ответ в XML — `Accept: application/xml`.")
        operation["description"] = "\n\n".join(filter(None, [operation.get("description"),
                                                             note]))


# --- XSD -------------------------------------------------------------------------

def _xs(tag: str) -> str:
    return f"{{{XS}}}{tag}"


_SCALARS = [(bool, "xs:boolean"), (int, "xs:long"), (float, "xs:double"),
            (datetime, "xs:dateTime"), (date, "xs:date"), (str, "xs:string")]


def _optional(annotation) -> tuple[object, bool]:
    """X | None → (X, True). Объединение нескольких типов, как str | bool у «тревожное»,
    в XML всё равно текст: xs:string."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        rest = [a for a in args if a is not type(None)]
        return (rest[0] if len(rest) == 1 else str), len(rest) < len(args)
    return annotation, False


def _document(parent: ET.Element, text: str) -> None:
    note = ET.SubElement(parent, _xs("annotation"))
    ET.SubElement(note, _xs("documentation")).text = text


class _Xsd:
    """Типы XSD из pydantic-моделей. output=True — ответ: все поля есть всегда, порядок
    полей фиксирован (xs:sequence). output=False — приём: поле со значением по умолчанию
    можно опустить, порядок любой (xs:all)."""

    def __init__(self, *, output: bool) -> None:
        self.output = output
        self.types: dict[str, ET.Element] = {}
        self.models: dict[str, type] = {}

    def complex_type(self, model: type[BaseModel]) -> str:
        name = schema_name(model)
        if name in self.models:
            if self.models[name] is not model:
                raise ValueError(f"две модели с именем схемы {name}")
            return name
        self.models[name] = model
        node = ET.Element(_xs("complexType"), name=name)
        self.types[name] = node
        group = ET.SubElement(node, _xs("sequence" if self.output else "all"))
        for field_name, field in model.model_fields.items():
            self._element(group, field.alias or field_name, field)
        return name

    def _element(self, group: ET.Element, name: str, field) -> None:
        annotation, nullable = _optional(field.annotation)
        many = get_origin(annotation) is list
        if many:
            if not self.output:
                raise TypeError(f"{name}: список в модели приёма xs:all не описывает")
            annotation = get_args(annotation)[0]
        element = ET.SubElement(group, _xs("element"), name=name)
        if many:
            element.set("minOccurs", "0")
            element.set("maxOccurs", "unbounded")
        elif not self.output and not field.is_required():
            element.set("minOccurs", "0")
        if nullable:
            element.set("nillable", "true")
        if field.description:
            _document(element, field.description)
        self._type(element, annotation, field.metadata)

    def _type(self, element: ET.Element, annotation, metadata: list) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            element.set("type", self.complex_type(annotation))
            return
        if get_origin(annotation) is Literal:
            restriction = self._restriction(element)
            for value in get_args(annotation):
                ET.SubElement(restriction, _xs("enumeration"), value=str(value))
            return
        base = next((xs for py, xs in _SCALARS
                     if isinstance(annotation, type) and issubclass(annotation, py)), None)
        if base is None:
            raise TypeError(f"{element.get('name')}: тип {annotation} в XSD не описан")
        # Field(max_length=…) у строки — ограничение длины, его проверяет и pydantic.
        facets = [(tag, str(getattr(item, attr))) for item in metadata
                  for attr, tag in (("max_length", "maxLength"), ("min_length", "minLength"))
                  if getattr(item, attr, None) is not None]
        if not facets:
            element.set("type", base)
            return
        restriction = self._restriction(element, base)
        for tag, value in facets:
            ET.SubElement(restriction, _xs(tag), value=value)

    @staticmethod
    def _restriction(element: ET.Element, base: str = "xs:string") -> ET.Element:
        simple = ET.SubElement(element, _xs("simpleType"))
        return ET.SubElement(simple, _xs("restriction"), base=base)


def _schema_document(title: str) -> ET.Element:
    schema = ET.Element(_xs("schema"), elementFormDefault="unqualified")
    _document(schema, title)
    return schema


def _serialize(schema: ET.Element, gen: _Xsd) -> bytes:
    for name in sorted(gen.types):
        schema.append(gen.types[name])
    return _to_bytes(schema)


def response_xsd() -> bytes:
    """XSD ответов маршрутов XML_ROUTES: корневой элемент на каждую модель ответа."""
    gen = _Xsd(output=True)
    operations: dict[str, list[str]] = {}
    models: dict[str, type[BaseModel]] = {}
    for (method, path), model in XML_ROUTES.items():
        operations.setdefault(schema_name(model), []).append(f"{method} {path}")
        models[schema_name(model)] = model
    schema = _schema_document(
        "Ответы REST API /api/v1 в XML по заголовку Accept: application/xml. Файл собирает "
        "scripts/export_contracts.py из pydantic-моделей backend/app/schemas; правила "
        "отображения JSON в XML — backend/app/xml_api.py.")
    for name in sorted(models):
        element = ET.SubElement(schema, _xs("element"), name=name)
        _document(element, ", ".join(operations[name]))
        element.set("type", gen.complex_type(models[name]))
    return _serialize(schema, gen)


def ingest_xsd() -> bytes:
    """XSD тел приёма: корень <Модель>List, в нём до MAX_BATCH_ROWS элементов <Модель>."""
    gen = _Xsd(output=False)
    schema = _schema_document(
        "Тела POST /api/v1/ingest/events и /api/v1/ingest/ods-journal в XML "
        "(Content-Type: application/xml). Имена элементов — ключи JSON-пачки. Файл собирает "
        "scripts/export_contracts.py; разбор — backend/app/xml_api.py.")
    for path, model in sorted(XML_INGEST.items()):
        root = ET.SubElement(schema, _xs("element"), name=list_root(model))
        _document(root, f"POST {path}")
        sequence = ET.SubElement(ET.SubElement(root, _xs("complexType")), _xs("sequence"))
        ET.SubElement(sequence, _xs("element"), name=schema_name(model),
                      type=gen.complex_type(model), minOccurs="0",
                      maxOccurs=str(MAX_BATCH_ROWS))
    return _serialize(schema, gen)
