"""Каналы «Состояние фазы»: расшифровка названия и что проверить бригаде на фидере.

Источник — ответы заказчика от 28.09.2026, analysis/qa_customer_2026-09-28.md:
- ответ 3 подтвердил обозначения: РО и АО — рабочее и аварийное освещение (так в
  СП 265.1325800.2016), ФРО и ФАО — их фидеры, ГРО — группа рабочего освещения,
  ФВ — фидер вентиляции, В23 — вентилятор, ФАНС — фидер автоматической насосной
  станции, ОЗК — огнезадерживающий клапан, ЩАП — щит аварийного питания с АВР,
  «Межсекционный» — секционный автомат между вводами, ФТС — фидер теплосети,
  ПУИ — пульт управления индикацией;
- ответ 4: в скобках названия фидера — то, что он питает, ПК в названии фидера тоже
  про то, что он питает; от чего запитан сам фидер, в названии не указано. Поэтому
  расшифровка фидера пишет «питает …» и никогда «запитан от …»;
- ответ 6: перечень проверок по фидеру «автомат, кабель, контактор, модуль связи,
  питание шкафа» заказчик назвал достаточным.

Обозначений, которых в ответах нет, расшифровка не касается: АВР, Фрез, ФРез, ППУ,
ФР, ФВР, ФОЗК, ОК, Г1, Гал., Э/щ, ДП остаются только в исходном названии, его экран
показывает рядом. У «ФВ1-В5» и «ФРО1-РО7» номер и нагрузка не расшифровываются: это
может быть и фидер 1 для В5, и фидер для В1–В5. Из такого названия берутся только вид
фидера и ПК. ПК после обозначения галереи («Г1 ПК5», «Гал.ПК43») пропускается: это пикет галереи,
а не коллектора.

Замер 28.09.2026 на справочнике стенда mkinteg (ref_channels; те же названия в
Materials/справочник_каналов_датчиков.csv): каналов «Состояние фазы» 957, все
названия разные. decode() даёт описание для 844 из них, это 88,2 %. Не разобраны 113:
АВР — 73, Фрез и ФРез — 14, ФР — 5, ППУ, ФВР, ОК и «Ввод …» без ЩАП — по 4,
«Ф.Резерв» и «Ф пит. Резерв» — 3, ФОЗК и «Наличие 12В» — по 1. Замер повторяет
tests/test_phase_channels.py::test_reference_coverage, когда справочник лежит в
raw_data_dir.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

PHASE = "Состояние фазы"

# Ф + обозначение нагрузки — фидер этой нагрузки (ответ 3).
FEEDERS = {
    "ФАНС": "фидер автоматической насосной станции",
    "ФРО": "фидер рабочего освещения",
    "ФАО": "фидер аварийного освещения",
    "ФТС": "фидер теплосети",
    "ФВ": "фидер вентиляции",
}
# «Фидер АО Щитовая ДП» — то же, что ФАО.
FEEDER_WORD = {"АНС": "ФАНС", "РО": "ФРО", "АО": "ФАО", "ТС": "ФТС", "В": "ФВ"}

_FEEDER_HEAD = re.compile(r"^\d*(ФАНС|ФРО|ФАО|ФТС|ФВ)(\d+)?(-[А-Я]+\d+)?(?=$|[\s(.,;])")
_FEEDER_WORD = re.compile(r"^Фидер\s+(?:(АНС|АО|РО|ТС|В)(?=$|\s)\s*)?")
_PAREN = re.compile(r"\(([^()]*)\)")
_NUM = r"\d+(?:\+\d+)?л?"
_PK = re.compile(rf"(?<![А-Яа-яЁёA-Za-z.\d])[Пп][Кк]\s*({_NUM})"
                 rf"(?:\s*-\s*(?:[Пп][Кк]\s*)?({_NUM}))?(?![А-Яа-яЁёA-Za-z\d])")
# Слово перед ПК, после которого пикет — галерейный: «Г1 ПК5», «Г.ДП ПК0-5», «Гал. ПК0».
_GALLERY = re.compile(r"(?:^|[\s(,;])(?:Г\d+|Г\.\S*|[Гг]ал\.?\S*)\s+$")
_ANS = re.compile(r"(?<![А-Яа-яЁё])АНС(\d+)")
_ITEMS = (
    ("fan", re.compile(r"^В(\d+)(?:\s*-\s*В?(\d+))?$")),
    ("gro", re.compile(r"^ГРО(\d+)(?:\s*-\s*(?:ГРО)?(\d+))?$")),
    ("ro", re.compile(r"^РО(\d+)(?:\s*-\s*(?:РО)?(\d+))?$")),
    ("ans", re.compile(r"^АНС(\d*)(?:\s+(.+))?$")),
)


def _pk_text(match: re.Match) -> str:
    start, end = match.group(1), match.group(2)
    return f"ПК {start}–{end}" if end else f"ПК {start}"


def _pickets(text: str) -> list[tuple[int, str]]:
    """ПК коллектора в тексте с позицией; галерейные пропускаются."""
    return [(m.start(), _pk_text(m)) for m in _PK.finditer(text)
            if not _GALLERY.search(text[:m.start()])]


def _item(text: str) -> tuple[str, str] | None:
    """Одна нагрузка из скобок: («fan», «В1–В3»), («pk», «ПК 0–28») или None."""
    text = text.strip()
    for kind, pattern in _ITEMS:
        m = pattern.match(text)
        if m is None:
            continue
        a, b = m.group(1), m.group(2)
        if kind == "fan":
            return kind, f"В{a}–В{b}" if b else f"В{a}"
        if kind == "gro":
            return kind, f"ГРО{a}–{b}" if b else f"ГРО{a}"
        if kind == "ro":
            return kind, f"РО{a}–РО{b}" if b else f"РО{a}"
        pk = _PK.fullmatch(b) if b else None
        if b and pk is None:
            return None
        return kind, f"АНС{a}" + (f" на {_pk_text(pk)}" if pk else "")
    pk = _PK.fullmatch(text)
    return ("pk", _pk_text(pk)) if pk else None


_NOUN = {
    "fan": ("вентилятор", "вентиляторы"),
    "gro": ("группу рабочего освещения", "группы рабочего освещения"),
    "ro": ("рабочее освещение", "рабочее освещение"),
    "ans": ("насосную станцию", "насосные станции"),
}


def _loads(rest: str) -> list[str]:
    """Что питает фидер: нагрузки из скобок и ПК вне скобок, в порядке названия."""
    found: list[tuple[int, str, str]] = []
    for group in _PAREN.finditer(rest):
        items = re.split(r"[,;]", group.group(1))
        first = _item(items[0])
        if first is None:  # «(26БК)», «(Г3 ПК0-18)», «(каб.)» — не нагрузка
            continue
        found += [(group.start(), *parsed) for parsed in map(_item, items) if parsed]
    outside = _PAREN.sub(lambda g: " " * len(g.group(0)), rest)
    found += [(pos, "pk", text) for pos, text in _pickets(outside)]
    found += [(m.start(), "ans", f"АНС{m.group(1)}") for m in _ANS.finditer(outside)]
    found.sort(key=lambda item: item[0])
    runs: list[tuple[str, list[str]]] = []
    for _, kind, text in found:
        if any(text in items for _, items in runs):
            continue
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(text)
        else:
            runs.append((kind, [text]))
    out = []
    for kind, items in runs:
        joined = ", ".join(items)
        if kind == "pk":
            out.append(joined)
            continue
        one, many = _NOUN[kind]
        single = len(items) == 1 and "–" not in items[0]
        out.append(f"{one if single else many} {joined}")
    return out


def _feeder(name: str) -> str | None:
    word = _FEEDER_WORD.match(name)
    if word:
        rest = name[word.end():]
        name = (FEEDER_WORD[word.group(1)] + " " + rest) if word.group(1) else rest
    m = _FEEDER_HEAD.match(name)
    if m is None:
        return None
    code, number, ambiguous = m.groups()
    head = FEEDERS[code] + (f" {number}" if number and not ambiguous else "")
    loads = _loads(name[m.end():])
    return head + (", питает " + ", ".join(loads) if loads else "")


def _with_pickets(head: str, rest: str) -> str:
    """Не фидер: ПК — место, а не нагрузка, поэтому без «питает»."""
    pickets = list(dict.fromkeys(text for _, text in _pickets(rest)))
    return ", ".join([head, *pickets])


def _other(name: str) -> str | None:
    if m := re.match(r"^(?:Группа\s+)?ГРО\s*(\d+)", name):
        return _with_pickets(f"группа рабочего освещения {m.group(1)}", name[m.end():])
    if m := re.match(r"^Группа\s+РО(\d+)", name):
        return _with_pickets(f"рабочее освещение, группа РО{m.group(1)}", name[m.end():])
    if (m := re.match(r"^(\d+)?РО(\d+)?(?=$|[\s(])", name)) and (m.group(1) or m.group(2)):
        label = f"{m.group(1)}РО" if m.group(1) else f"РО{m.group(2)}"
        return _with_pickets(f"рабочее освещение {label}", name[m.end():])
    if m := re.search(r"ЩАП(?:[\s-]*(\d+))?", name):
        head = "щит аварийного питания " + (f"ЩАП-{m.group(1)} " if m.group(1) else "") + "с АВР"
        feed = re.search(r"(?i)(?:ввод|вв\.)\s*-?\s*(\d+)", name)
        if feed:
            head += f", ввод {feed.group(1)}"
        return _with_pickets(head, name)
    if m := re.match(r"^Межсекционный(?:\s+АВ)?", name):
        return _with_pickets("секционный автомат между вводами", name[m.end():])
    if m := re.match(r"^ОЗК(\d+)?(?=$|[\s.])", name):
        rest = name[m.end():]
        head = "огнезадерживающий клапан" + (f" {m.group(1)}" if m.group(1) else "")
        if fan := re.search(r"(?<![А-Яа-яЁё])В(\d+)(?![А-Яа-яЁё\d])", rest):
            head += f" вентилятора В{fan.group(1)}"
        text = _with_pickets(head, rest)
        for short, full in (("закр", "закрыт"), ("откр", "открыт")):
            if re.search(rf"(?<![А-Яа-яЁё]){short}", rest):
                text += f", положение «{full}»"
        return text
    if m := re.match(r"^Питание\s+ПУИ\s*(\d+)?", name):
        return "питание пульта управления индикацией" + (f" {m.group(1)}" if m.group(1) else "")
    return None


@lru_cache(maxsize=4096)
def decode(name: str | None) -> str | None:
    """Человеческое описание названия канала «Состояние фазы» или None, если не разобрано.

    «ФВ2 (В23)» → «фидер вентиляции 2, питает вентилятор В23»;
    «ФРО1 (ГРО1-6)» → «фидер рабочего освещения 1, питает группы рабочего освещения ГРО1–6».
    Исходное название описание не заменяет: не расшифрованное в нём остаётся только там.
    """
    if not name:
        return None
    text = " ".join(name.split())
    return _feeder(text) or _other(text)


def decode_channel(sensor_type: str | None, name: str | None) -> str | None:
    """Расшифровка только для «Состояние фазы»: у других типов свои обозначения."""
    return decode(name) if sensor_type == PHASE else None


@dataclass(frozen=True)
class Checklist:
    equipment: str
    items: tuple[str, ...]
    note: str | None
    basis: str


FEEDER_CHECKLIST = Checklist(
    equipment="Фидер",
    items=("Автомат", "Кабель", "Контактор", "Модуль связи", "Питание шкафа"),
    # Оговорка из того же ответа 6: сбой бывает и вне коллектора.
    note="Сбой бывает и вне коллектора: у ресурсоснабжающей организации или повреждение "
         "кабеля в земле.",
    basis="Перечень подтвердил заказчик 28.09.2026",
)

# Тип датчика → перечень. Для насоса, вентилятора и ИБП ответа заказчика нет,
# поэтому перечня у них нет: выдуманный список бригада приняла бы за регламент.
CHECKLISTS = {PHASE: FEEDER_CHECKLIST}
