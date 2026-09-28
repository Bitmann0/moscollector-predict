"""Класс события СМВУ, группа аварии и подсказка «вероятно, ППР или ТО» (C5).

Термины и группы — из ответов заказчика 28.09.2026 (analysis/qa_customer_2026-09-28.md):
запись с флагом «тревожное» диспетчеры называют тревожным сообщением (ответ 1); авария —
угроза жизни человека, пять групп (ответ 2); серии срабатываний — ППР или ТО (ответ 5).

Порядок правил classify — раздел C5 плана команды с поправками по этим ответам:
1. Неисправность датчика: метан меньше 0 или в насыщении 327,68, температура вне
   −60…150, «дата» 01.01.1970 03:00:0x вместо значения, текст с «неисправ» или «ошиб».
2. Метан по доле объёма: от 1 % — alarm, от 5 % — critical. Группы у порогов нет.
3. Тревожное сообщение из группы аварий (incident_group) — critical и группа.
   «Потеря всей связи» и «потеря всего электроснабжения» заказчик назвал инцидентами,
   а не авариями, поэтому «Обесточен», «Отключено устройство», «Питание от батарей»
   остаются alarm.
4. «пожар» или «затоп» без флага — critical без группы: группа — вид тревожного
   сообщения, а без флага его нет. В журнале стенда 02.05–30.06 таких записей 0.
5. Остальные тревожные сообщения — alarm.
6. «Обнаружен дым», «Обнаружен газ» и «Температура выше 40ºC» без флага — warning
   (#42): так пришло 74,2 % записей «Обнаружен дым» (121 701 из 164 128,
   ml/reports/sensor_semantics_audit.json). Уведомлений предупреждение не создаёт.
7. Остальное — normal.

Пороги метана, окна подсказок и пороги серий настраивает администратор (ML2-13, экран
«Настройки», backend/app/services/parameters.py): classify и series_hints получают их
в Rules. Константы ниже — проверенные значения, они же значения Rules по умолчанию.

Подсказка класс не меняет: диспетчер и дежурный инженер всегда проверяют событие сами,
прежде чем отнести его к инцидентам, профилактическим работам или ошибкам (ответ 1).
Все подсказки о плановых работах начинаются с PPR_HINT — по этому префиксу их считает
KPI дашборда. Их две:
- «Обнаружен газ» в будни с 9:00 до 14:59 МСК: на это окно приходится 88,6 % таких
  записей (19 307 из 21 784, план команды, C5), заказчик подтвердил поверки баллонами;
- серия срабатываний (series_hints): её видно только по нескольким событиям сразу,
  поэтому считает её приём пачки (ingest.py), а не classify.
"""
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from typing import NamedTuple

from .helpers import MSK

GAS = "Газовый датчик"
TEMPERATURE = "Датчик температуры"
# Физические пределы — те же, что VALUE_LIMITS в ml/src/mkl/config.py: газ упирается
# в 327,68 = 2^15/100, температура приходит с -3276 и 999 при переполнении.
GAS_LIMITS = (0.0, 327.67)
TEMPERATURE_LIMITS = (-60.0, 150.0)
GAS_ALARM = 1.0
GAS_CRITICAL = 5.0
EPOCH_PREFIX = "01.01.1970 03:00:0"
PPR_HINT = "вероятно, ППР или ТО"
WORKDAYS = frozenset(range(5))  # пн–пт; 0 — понедельник, как у datetime.weekday()
# Окно газа [9, 15) — с 9:00 до 14:59 МСК; замер окна — в docstring модуля.
GAS_WINDOW_HOURS = (9, 15)
NO_ALARM_HINT = "в СМВУ без признака тревоги"
HAZARD_TEXT = ("обнаружен дым", "обнаружен газ", "температура выше")

# Группы аварий (ответ 2). Типы датчиков и тексты сверены с журналом стенда mkinteg
# за 02.05–30.06.2026 (10 428 318 событий; число — тревожные сообщения этого вида):
# - пожар: «Обнаружен дым» у датчика дыма 4 714, «Рычаг сдернут» у «Состояние УИР-Р»
#   618, «Не замкнут» у теплового датчика 236 и у ручного извещателя 45;
# - наводнение: «Затоплен» у «Состояние насоса» 1 256, «Не замкнут» у датчика затопления 19;
# - газ: «Обнаружен газ» 505, только у газового датчика;
# - проникновение (у заказчика «террор, проникновение нарушителя»): «Не замкнут» у КД АВ
#   3 998, КД Дверь 489, Стекло 218, КД Люк 73; «Обнаружено движение» у датчика движения
#   1 358 и один раз у КД Дверь — ответ 2 относит к проникновению любую сработку охраны;
# - аномальная температура: «Температура ниже 3ºC» 100, «Температура выше 40ºC» 7.
# Тексты, однозначные сами по себе, группу дают при любом типе датчика: у 1 142 каналов
# журнала нет строки в справочнике (docs/submission/03-data-processing.md), и тип
# у них неизвестен. «Не замкнут» без типа не различить: это и пожар, и наводнение,
# и проникновение. «Не замкнут» у датчика дыма (3 записи) и у «Состояние УИР-Р» (34)
# в группу не входит: в ответе 2 сработка этих извещателей — «Обнаружен дым» и
# «Рычаг сдернут».
GROUP_BY_TEXT = {
    "обнаружен дым": "fire",
    "рычаг сдернут": "fire",
    "затоплен": "flood",
    "обнаружен газ": "gas",
    "обнаружено движение": "intrusion",
}
GROUP_BY_PREFIX = (("температура выше", "temperature"), ("температура ниже", "temperature"))
OPEN_CONTACT = "не замкнут"
GROUP_BY_OPEN_CONTACT = {
    "Тепловой датчик": "fire",
    "Ручной извещатель": "fire",
    "Датчик затопления": "flood",
    "КД Дверь": "intrusion",
    "КД Люк": "intrusion",
    "КД АВ": "intrusion",
    "Стекло": "intrusion",
}

# Серии ППР и ТО (ответ 5): за 10 минут срабатывают несколько извещателей подряд.
# Замер порогов и часов — analysis/incident_series_audit.py на журнале стенда
# 02.05–30.06.2026, результат — analysis/incident_series_audit.json.
SERIES_WINDOW = timedelta(minutes=10)
# Пожар: 5 и более извещателей одного объекта — порог из вопроса 5, заказчик его
# подтвердил. Газ: газоанализаторы одного комплекса (ref_objects.parent_id) «подряд
# вдоль коллектора», порог 4 (раздел gas): при пороге 3 в 8 из 23 серий датчики стоят
# не больше чем на двух пикетах — это соседние датчики одного места, так выглядит и
# настоящая утечка; при пороге 4 таких серий 2 из 15, при пороге 5 серий остаётся 5.
SERIES_MIN = {"fire": 5, "gas": 4}
SERIES_NOUN = {"fire": "извещателей", "gas": "газоанализаторов"}
# Рабочее время — будни с 8:00 до 17:59 МСК (раздел fire). Из 46 серий от 5 пожарных
# извещателей за 10 минут в эти часы начались 40, в 8:00–13:59 — 37: узкое окно теряет
# серии 04.06 в 15:14 и 17:14 на объекте 3356, где ППР шёл с 09:46, и 11.06 в 15:27 на
# объекте 5567 (20 извещателей). Вне окна 6 серий: 4 в выходные и 2 ночью 10.06 на
# объекте 3356 (00:15 и 01:14, 29 и 33 извещателя) — для них подсказки нет.
# Рабочее время проверяется и у газа: в вопросе 5 оно стоит в одном предложении
# с газоанализаторами. При пороге 4 оно снимает одну серию из 16 — суббота 02.05 14:07.
# Окно проверяется по первому событию: события до 18:10 ещё входят в окно, начатое
# до 18:00.
WORK_HOURS = (8, 18)
WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _days_text(days: frozenset[int]) -> str:
    if days == WORKDAYS:
        return "в будни"
    if days == frozenset(range(7)):
        return "ежедневно"
    if days == frozenset({5, 6}):
        return "в выходные"
    return "в " + ", ".join(WEEKDAY_SHORT[d] for d in sorted(days))


@dataclass(frozen=True)
class Rules:
    """Настраиваемая часть правил C5. Часы — [начало, конец) МСК: (9, 15) — с 9:00 до 14:59."""
    gas_alarm: float = GAS_ALARM
    gas_critical: float = GAS_CRITICAL
    gas_window_hours: tuple[int, int] = GAS_WINDOW_HOURS
    gas_window_days: frozenset[int] = WORKDAYS
    series_window: timedelta = SERIES_WINDOW
    series_min: Mapping[str, int] = field(default_factory=lambda: dict(SERIES_MIN))
    work_hours: tuple[int, int] = WORK_HOURS
    work_days: frozenset[int] = WORKDAYS

    @cached_property  # текст считается один раз на набор параметров, а не на событие
    def gas_window_hint(self) -> str:
        start, end = self.gas_window_hours
        return (f"{PPR_HINT}: газ {_days_text(self.gas_window_days)} "
                f"с {start}:00 до {end - 1}:59")

    def series_hint(self, group: str, detectors: int) -> str:
        minutes = int(self.series_window.total_seconds() // 60)
        return (f"{PPR_HINT}: серия из {detectors} {SERIES_NOUN[group]} "
                f"за {minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}")


DEFAULT_RULES = Rules()
GAS_WINDOW_HINT = DEFAULT_RULES.gas_window_hint


class Verdict(NamedTuple):
    event_class: str
    hint: str | None
    incident_group: str | None


@dataclass(frozen=True)
class SeriesEvent:
    """Событие группы fire или gas для series_hints. ref — ключ результата у вызывающего."""
    ref: Hashable
    group: str
    key: str | None      # объект канала для пожара, комплекс для газа
    channel_id: int
    ts: datetime         # с таймзоной


def incident_group(sensor_type: str | None, val_raw: str | None) -> str | None:
    """Группа аварии по тексту состояния; код — из vocabularies.json, incident_group."""
    text = (val_raw or "").strip().casefold()
    if text in GROUP_BY_TEXT:
        return GROUP_BY_TEXT[text]
    for prefix, group in GROUP_BY_PREFIX:
        if text.startswith(prefix):
            return group
    if text == OPEN_CONTACT:
        return GROUP_BY_OPEN_CONTACT.get(sensor_type or "")
    return None


def _gas_window(text: str, ts: datetime | None, rules: Rules) -> bool:
    if ts is None or "обнаружен газ" not in text:
        return False
    local = ts.astimezone(MSK) if ts.tzinfo else ts
    start, end = rules.gas_window_hours
    return local.weekday() in rules.gas_window_days and start <= local.hour < end


def classify(sensor_type: str | None, val_raw: str | None, val_num: float | None,
             alarm: bool, *, ts: datetime | None = None,
             rules: Rules = DEFAULT_RULES) -> Verdict:
    """(event_class, hint, incident_group); коды — из vocabularies.json.

    ts — время регистрации; без него подсказка о газе в рабочие часы не ставится.
    Подсказку серии classify не ставит: см. series_hints.
    """
    text = (val_raw or "").strip()
    lowered = text.casefold()
    if text.startswith(EPOCH_PREFIX):
        return Verdict("fault", None, None)
    if sensor_type == GAS and val_num is not None:
        if not GAS_LIMITS[0] <= val_num <= GAS_LIMITS[1]:
            return Verdict("fault", None, None)
        if val_num >= rules.gas_critical:
            return Verdict("critical", None, None)
        if val_num >= rules.gas_alarm:
            return Verdict("alarm", None, None)
    if (sensor_type == TEMPERATURE and val_num is not None
            and not TEMPERATURE_LIMITS[0] <= val_num <= TEMPERATURE_LIMITS[1]):
        return Verdict("fault", None, None)
    if "неисправ" in lowered or "ошиб" in lowered or "fault" in lowered:
        return Verdict("fault", None, None)
    window = rules.gas_window_hint if _gas_window(lowered, ts, rules) else None
    if alarm and (group := incident_group(sensor_type, text)):
        return Verdict("critical", window, group)
    if "пожар" in lowered or "затоп" in lowered:
        return Verdict("critical", None, None)
    if alarm:
        return Verdict("alarm", None, None)
    if any(marker in lowered for marker in HAZARD_TEXT):
        return Verdict("warning", window or NO_ALARM_HINT, None)
    return Verdict("normal", None, None)


def series_key(group: str | None, obj_id: str | None, complex_id: str | None) -> str | None:
    """Ключ серии: пожар — объект канала, газ — комплекс (родитель объекта)."""
    if group == "fire":
        return obj_id
    if group == "gas":
        return complex_id
    return None


def series_hint(group: str, detectors: int, rules: Rules = DEFAULT_RULES) -> str:
    return rules.series_hint(group, detectors)


def _working_time(ts: datetime, rules: Rules = DEFAULT_RULES) -> bool:
    local = ts.astimezone(MSK)
    start, end = rules.work_hours
    return local.weekday() in rules.work_days and start <= local.hour < end


def series_hints(events: Iterable[SeriesEvent],
                 rules: Rules = DEFAULT_RULES) -> dict[Hashable, str]:
    """Подсказка каждому событию, входящему хотя бы в одно окно-серию.

    Окно — события одного ключа за rules.series_window (проверено 10 минут),
    заканчивающиеся на каком-либо событии. Окно — серия, если в нём не меньше
    rules.series_min разных каналов и первое событие окна пришлось на рабочее время.
    Событию достаётся наибольшее число каналов из окон-серий, в которые оно входит.

    Подсказка события в момент t зависит только от событий [t − окно, t + окно]:
    окна, в которые оно входит, кончаются не позже t + окно и начинаются не раньше
    t − окно. Поэтому она точна у тех событий, для которых вызывающий передал всё
    из этого отрезка, — на краях выборки число может оказаться меньше настоящего.
    """
    window = rules.series_window
    by_key: dict[tuple[str, str], list[SeriesEvent]] = defaultdict(list)
    for event in events:
        if event.group in rules.series_min and event.key is not None:
            by_key[(event.group, event.key)].append(event)
    hints: dict[Hashable, str] = {}
    for (group, _), items in by_key.items():
        items.sort(key=lambda e: e.ts)
        best = [0] * len(items)
        channels: Counter[int] = Counter()
        start = 0
        for end, item in enumerate(items):
            channels[item.channel_id] += 1
            while items[start].ts < item.ts - window:
                gone = items[start].channel_id
                channels[gone] -= 1
                if not channels[gone]:
                    del channels[gone]
                start += 1
            detectors = len(channels)
            if (detectors >= rules.series_min[group]
                    and _working_time(items[start].ts, rules)):
                for i in range(start, end + 1):
                    best[i] = max(best[i], detectors)
        for item, detectors in zip(items, best, strict=True):
            if detectors:
                hints[item.ref] = rules.series_hint(group, detectors)
    return hints
