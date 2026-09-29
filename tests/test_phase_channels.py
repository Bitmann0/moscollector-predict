"""Каналы «Состояние фазы»: расшифровка названия и «Что проверить» в заявке по фидеру.

Названия в тестах — настоящие, из справочника каналов (ref_channels стенда mkinteg).
Это названия каналов, не данные журнала. Правила — ответы заказчика от 28.09.2026,
analysis/qa_customer_2026-09-28.md.
"""
import csv

import pytest
from app import models
from app.config import get_settings
from app.main import create_app
from app.schemas.ml import WorkOrderOut
from app.services.phase_channels import (
    FEEDER_CHECKLIST,
    PHASE,
    decode,
    decode_channel,
)
from conftest import MONDAY, build_score

API = "/api/v1"

DECODED = [
    # фидеры: скобки — что питает (ответ 4)
    ("ФВ2 (В23)", "фидер вентиляции 2, питает вентилятор В23"),
    ("Фидер ФВ2 (В23)", "фидер вентиляции 2, питает вентилятор В23"),
    ("ФВ1 (В14 - В17, В35)", "фидер вентиляции 1, питает вентиляторы В14–В17, В35"),
    ("Фидер ФВ2 (В6-В7)", "фидер вентиляции 2, питает вентиляторы В6–В7"),
    ("ФВ1 ПК249 (В9-В13,В15)", "фидер вентиляции 1, питает ПК 249, вентиляторы В9–В13, В15"),
    ("ФРО1 (ГРО1-6)", "фидер рабочего освещения 1, питает группы рабочего освещения ГРО1–6"),
    ("ФРО1 (РО1, РО2)", "фидер рабочего освещения 1, питает рабочее освещение РО1, РО2"),
    ("ФРО2 (РО3 - РО5)", "фидер рабочего освещения 2, питает рабочее освещение РО3–РО5"),
    ("ФАО1 (ПК0-28)", "фидер аварийного освещения 1, питает ПК 0–28"),
    ("Фидер ФАО2 (ПК228-144)", "фидер аварийного освещения 2, питает ПК 228–144"),
    ("ФАНС1 (АНС1 ПК13)",
     "фидер автоматической насосной станции 1, питает насосную станцию АНС1 на ПК 13"),
    ("Фидер ФАНС1 (АНС ПК26, ПК77)",
     "фидер автоматической насосной станции 1, питает насосную станцию АНС на ПК 26, ПК 77"),
    ("Фидер АНС Щитовая ДП", "фидер автоматической насосной станции"),
    ("Фидер ФТС", "фидер теплосети"),
    ("ФТС1 ПК1089", "фидер теплосети 1, питает ПК 1089"),
    # ПК в названии фидера — тоже то, что он питает (ответ 4)
    ("ФВ1 ПК305 щит", "фидер вентиляции 1, питает ПК 305"),
    ("ФРО2 щитовая 2 ПК321", "фидер рабочего освещения 2, питает ПК 321"),
    ("ФВ1 ПК168+30 щит.", "фидер вентиляции 1, питает ПК 168+30"),
    # пикет галереи — не пикет коллектора
    ("ФВ1 ПК48 Г1 ПК5", "фидер вентиляции 1, питает ПК 48"),
    ("ФАО1 ПК172 Гал.ПК43", "фидер аварийного освещения 1, питает ПК 172"),
    # скобки не про нагрузку и неоднозначный «ФВ1-В5» не расшифровываются
    ("ФАО Э/щ ДП ПС Никулино (26БК)", "фидер аварийного освещения"),
    ("ФВ1-В5 ПК148 Проф", "фидер вентиляции, питает ПК 148"),
    ("1ФАО1", "фидер аварийного освещения 1"),
    # не фидеры: ПК — место, без «питает»
    ("Группа ГРО1 ПК0-11", "группа рабочего освещения 1, ПК 0–11"),
    ("ГРО10 ПК249-ПК272", "группа рабочего освещения 10, ПК 249–272"),
    ("ГРО11 ПК24л-ПК51л", "группа рабочего освещения 11, ПК 24л–51л"),
    ("ГРО 8\r\n(ПК140-162) ПК162", "группа рабочего освещения 8, ПК 140–162, ПК 162"),
    ("РО1 ПК0-8", "рабочее освещение РО1, ПК 0–8"),
    ("10РО ПК739-765", "рабочее освещение 10РО, ПК 739–765"),
    ("Группа РО2 ПК14-42", "рабочее освещение, группа РО2, ПК 14–42"),
    ("ЩАП-23 Ввод1 ПК385", "щит аварийного питания ЩАП-23 с АВР, ввод 1, ПК 385"),
    ("ЩАП вв.2 ПК399", "щит аварийного питания с АВР, ввод 2, ПК 399"),
    ("Ввод1 ПК157 ЩАП-12", "щит аварийного питания ЩАП-12 с АВР, ввод 1, ПК 157"),
    ("Межсекционный АВ ПК28", "секционный автомат между вводами, ПК 28"),
    ("ОЗК В1 ПК0 закр.",
     "огнезадерживающий клапан вентилятора В1, ПК 0, положение «закрыт»"),
    ("ОЗК1 откр. ПК730", "огнезадерживающий клапан 1, ПК 730, положение «открыт»"),
    ("Питание ПУИ2 в ДП", "питание пульта управления индикацией 2"),
]

# Обозначений нет в ответах заказчика — расшифровки нет, а не догадка.
NOT_DECODED = ["АВР Ввод1 ПК0", "АВР Секц.перекл. ПК60 Э/щ №2", "Фрез.1 ПК0", "ФРез1 ПК254 щит .",
               "ФР2 ПК28", "ФВР1 ПК1089", "ППУ ввод А ПК28", "ОК1 Закрыт ПК175",
               "ФОЗК щит. ПК730", "Ф.Резерв 1", "Наличие 12В", "Ввод 1 ДП Н.Черемушки"]


@pytest.mark.parametrize(("name", "expected"), DECODED)
def test_decode_real_names(name, expected):
    assert decode(name) == expected


@pytest.mark.parametrize("name", NOT_DECODED)
def test_unknown_designations_stay_undecoded(name):
    assert decode(name) is None


def test_feeder_source_is_never_claimed():
    """Ответ 4: от чего запитан сам фидер, в названии нет — расшифровка этого не пишет."""
    assert not [name for name, _ in DECODED if "запитан" in (decode(name) or "")]


def test_decode_only_for_phase_channels():
    assert decode_channel(PHASE, "ФВ2 (В23)") == "фидер вентиляции 2, питает вентилятор В23"
    assert decode_channel("Состояние вентилятора", "ФВ2 (В23)") is None
    assert decode_channel(PHASE, None) is None


def test_reference_coverage():
    """Доля из докстринга phase_channels: 844 из 957. Справочника нет в репозитории —
    тест идёт, когда он лежит в raw_data_dir (RAW_DATA_DIR=…/Materials)."""
    path = get_settings().raw_data_dir / "справочник_каналов_датчиков.csv"
    if not path.is_file():
        pytest.skip(f"нет справочника каналов: {path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        names = [row["название_датчика"] for row in csv.DictReader(stream)
                 if row["тип_датчика"] == PHASE]
    assert (len(names), sum(decode(n) is not None for n in names)) == (957, 844)


FEEDER = 9100001
FEEDER_NAME = "Фидер ФВ2 (В23)"


@pytest.fixture
def feeder_run(seeded, fake_ml, admin):
    """Прогон понедельника, где к выдаче добавлен алерт и заявка по фидеру.

    ML присылает смешанную заявку: фидер и газовый канал 9000001 одного объекта.
    По газовому каналу уже есть другая заявка, поэтому backend оставляет в WO-FEEDER
    только свободный прогноз фидера. Перечень относится только к нему.
    """
    seeded.add(models.RefChannel(id=FEEDER, obj_id="9101", system="Диспетчерский контроль",
                                 sensor_type=PHASE, name=FEEDER_NAME, picket=None))
    seeded.commit()

    def score(request):
        resp = build_score(request)
        base = resp.alerts[0]
        alert = base.model_copy(update={
            "alert_id": "feeder-alert", "case_key": "feeder-case",
            "address": base.address.model_copy(update={
                "channel": FEEDER, "sensor_name": FEEDER_NAME, "sensor_type": PHASE,
                "picket": None, "picket_label": None})})
        order = WorkOrderOut(order_id="WO-FEEDER", created_for=request.asof,
                             due_by=alert.valid_to, priority="срочная",
                             work_type="Проверка и обслуживание датчика",
                             direction="sensor_failure", direction_title="Отказ", obj="9101",
                             alert_ids=[base.alert_id, alert.alert_id],
                             channels=[base.address.channel, FEEDER])
        return resp.model_copy(update={"alerts": [*resp.alerts, alert],
                                       "work_orders": [*resp.work_orders, order]})

    fake_ml.score = score
    resp = admin.post(f"{API}/admin/run-daily", json={"asof": MONDAY.isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_feeder_order_lists_checks_for_feeder_only(admin, feeder_run):
    card = admin.get(f"{API}/work-orders/WO-FEEDER").json()
    assert card["channels"] == [FEEDER]
    assert card["forecast_ids"] == ["feeder-alert"]
    active = [order for order in admin.get(f"{API}/work-orders").json()["items"]
              if order["status"] in {"draft", "confirmed", "in_progress"}]
    assert sum(9000001 in admin.get(f"{API}/work-orders/{order['id']}").json()["channels"]
               for order in active) == 1
    [group] = card["checklist"]
    assert group["items"] == ["Автомат", "Кабель", "Контактор", "Модуль связи",
                              "Питание шкафа"]
    assert group["basis"] == FEEDER_CHECKLIST.basis
    assert [c["id"] for c in group["channels"]] == [FEEDER]
    assert group["channels"][0]["name"] == FEEDER_NAME
    assert group["channels"][0]["name_decoded"] == "фидер вентиляции 2, питает вентилятор В23"


def test_order_without_feeder_has_no_checklist(admin, feeder_run):
    """Для насоса, вентилятора и ИБП ответа заказчика нет — перечня нет."""
    orders = admin.get(f"{API}/work-orders").json()["items"]
    others = [o["id"] for o in orders if o["id"] != "WO-FEEDER"]
    assert others
    for order_id in others:
        assert admin.get(f"{API}/work-orders/{order_id}").json()["checklist"] == []


def test_forecast_card_decodes_feeder_channel(admin, feeder_run):
    card = admin.get(f"{API}/forecasts/feeder-alert").json()
    assert card["channel"]["name_decoded"] == "фидер вентиляции 2, питает вентилятор В23"
    other = admin.get(f"{API}/forecasts").json()["items"]
    assert all(f["channel"]["name_decoded"] is None for f in other
               if f["channel"] and f["channel"]["id"] != FEEDER)


def test_contract_has_checklist_and_decoded_name():
    schemas = create_app().openapi()["components"]["schemas"]
    assert "checklist" in schemas["WorkOrderCard"]["properties"]
    assert set(schemas["ChecklistOut"]["required"]) == {"equipment", "channels", "items",
                                                        "basis"}
    assert "name_decoded" in schemas["ChannelRef"]["properties"]
