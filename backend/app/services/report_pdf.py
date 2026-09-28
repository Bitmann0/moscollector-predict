"""Отчёт руководству в PDF (ТЗ §8): вёрстка ReportData из report.py на fpdf2.

Шрифт — DejaVu Sans 2.35 из resources/fonts, рядом лицензия (Bitstream Vera с правками
DejaVu в общественном достоянии). Базовые шрифты PDF кириллицу не кодируют, а в образе
python:3.12-slim системных шрифтов нет; файл в пакете даёт один путь на Windows, в CI и
в образе. fpdf2 встраивает в документ только использованные глифы.

Диаграмма по дням — столбцы от нуля с подписанными осями и числом над столбцом (ТЗ §18,
«объективность диаграмм»). День без расчёта остаётся без столбца с пометкой, а не
рисуется нулём — то же правило, что у графика «Прогнозы по дням» на дашборде.
"""
import math
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from .report import ReportData

FONTS_DIR = Path(__file__).resolve().parents[1] / "resources" / "fonts"
FONT = "DejaVu"
ORG = "АО «Москоллектор»"
TITLE = "Отчёт руководству: прогнозы, заявки и тревожные сообщения"
DEMO_NOTE = "Данные демо-стенда: решения за июнь эмулированы."
METHOD_PATH = "docs/submission/04-ml.md"
METHOD_URL = f"https://github.com/Bitmann0/moscollector-predict/blob/main/{METHOD_PATH}"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

INK = (33, 37, 41)
MUTED = (108, 117, 125)
RULE = (206, 212, 218)
GRID = (233, 236, 239)
HEAD_FILL = (236, 240, 245)
NOTE_FILL = (255, 243, 205)
BAR = (52, 88, 166)

HEADINGS = FontFace(emphasis="BOLD", fill_color=HEAD_FILL)
BOLD = FontFace(emphasis="BOLD")


def num(value: int) -> str:
    """12 345 — разряды неразрывным пробелом, как Intl.NumberFormat("ru-RU") на фронте."""
    return f"{value:,}".replace(",", " ")


def pct(value: float | None) -> str:
    """37,5 % — не больше одного знака после запятой, как fmtPercent на фронте."""
    if value is None:
        return "—"
    text = f"{value * 100:.1f}".replace(".", ",").removesuffix(",0")
    return f"{text} %"


def dmy(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class _Pdf(FPDF):
    def footer(self) -> None:
        self.set_y(-13)
        self.set_draw_color(*RULE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_y(-11)
        self.set_font(FONT, size=7.5)
        self.set_text_color(*MUTED)
        lead = "Сформировано сервисом Москоллектор · Методика: "
        self.cell(self.get_string_width(lead), 4, lead)
        self.set_text_color(*BAR)
        self.cell(self.get_string_width(METHOD_PATH), 4, METHOD_PATH, link=METHOD_URL)
        self.set_text_color(*MUTED)
        self.cell(0, 4, f"Стр. {self.page_no()} из {{nb}}", align="R")


def render(data: ReportData) -> bytes:
    pdf = _Pdf(orientation="P", unit="mm", format="A4")
    pdf.add_font(FONT, "", FONTS_DIR / "DejaVuSans.ttf")
    pdf.add_font(FONT, "B", FONTS_DIR / "DejaVuSans-Bold.ttf")
    pdf.set_margins(15, 14, 15)
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title(TITLE)
    pdf.set_author(ORG)
    pdf.set_creator("Москоллектор")
    pdf.set_lang("ru")
    pdf.set_creation_date(data.generated_at)
    pdf.add_page()
    _header(pdf, data)
    _scenarios(pdf, data)
    _chart(pdf, data)
    _orders(pdf, data)
    _incidents(pdf, data)
    _objects(pdf, data)
    return bytes(pdf.output())


def _text(pdf: FPDF, text: str, *, size: float = 9, bold: bool = False,
          color: tuple = INK, height: float = 4.6) -> None:
    pdf.set_font(FONT, "B" if bold else "", size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, height, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _section(pdf: FPDF, title: str, need: float) -> None:
    """Заголовок раздела; если после него не влезает need мм, раздел уходит на новую страницу."""
    if pdf.get_y() + 9 + need > pdf.page_break_trigger:
        pdf.add_page()
    pdf.ln(4)
    _text(pdf, title, size=11.5, bold=True, height=6)
    pdf.ln(1)


def _note(pdf: FPDF, text: str) -> None:
    pdf.ln(1.2)
    _text(pdf, text, size=7.8, color=MUTED, height=3.8)


def _header(pdf: FPDF, data: ReportData) -> None:
    _text(pdf, ORG, size=10, bold=True, color=MUTED)
    pdf.ln(1)
    _text(pdf, TITLE, size=15, bold=True, height=7)
    pdf.ln(1.5)
    days = data.days_total
    _text(pdf, f"Период: {dmy(data.date_from)} — {dmy(data.date_to)} "
               f"({days} {plural(days, 'сутки', 'суток', 'суток')}; прогнозы и заявки — "
               "по дате расчёта, события — по времени события, МСК)")
    _text(pdf, f"Сформирован: {data.generated_at:%d.%m.%Y %H:%M} МСК; демо-дата стенда — "
               f"{dmy(data.demo_today)}")
    decided = sum(s.decided for s in data.scenarios)
    issued = sum(s.issued for s in data.scenarios)
    pdf.ln(2)
    pdf.set_fill_color(*NOTE_FILL)
    pdf.set_font(FONT, "B", 9)
    pdf.set_text_color(*INK)
    body = (f"{DEMO_NOTE} Решение диспетчера есть у {num(decided)} из {num(issued)} "
            f"прогнозов периода, у {num(data.emulated_decided)} из них оно эмулировано "
            "(source = emulated).")
    pdf.multi_cell(0, 5, body, fill=True, padding=(2, 3), align="L", new_x=XPos.LMARGIN,
                   new_y=YPos.NEXT)


def _table(pdf: FPDF, widths: tuple, align: tuple, rows: list[list[str]],
           total: list[str] | None = None) -> None:
    pdf.set_font(FONT, "", 8.5)
    pdf.set_text_color(*INK)
    pdf.set_draw_color(*RULE)
    # Ячейки без своей заливки fpdf2 красит текущим цветом заливки документа.
    pdf.set_fill_color(255, 255, 255)
    with pdf.table(width=sum(widths), col_widths=widths, text_align=align, align="LEFT",
                   line_height=4.6, headings_style=HEADINGS, borders_layout="HORIZONTAL_LINES",
                   padding=(1.2, 1.5), repeat_headings=1) as table:
        for values in rows:
            row = table.row()
            for value in values:
                row.cell(value)
        if total is not None:
            row = table.row()
            for value in total:
                row.cell(value, style=BOLD)


def _scenarios(pdf: FPDF, data: ReportData) -> None:
    _section(pdf, "1. Прогнозы по сценариям", need=40)
    head = ["Сценарий", "Выдано", "С фактом", "Попало", "Промахов", "Неизвестно",
            "Точность по известным"]
    rows = [[s.title, num(s.issued), num(s.known), num(s.hit), num(s.miss), num(s.unknown),
             pct(s.precision)] for s in data.scenarios]
    hit, miss = sum(s.hit for s in data.scenarios), sum(s.miss for s in data.scenarios)
    total = ["Все сценарии", num(sum(s.issued for s in data.scenarios)), num(hit + miss),
             num(hit), num(miss), num(sum(s.unknown for s in data.scenarios)),
             pct(hit / (hit + miss) if hit + miss else None)]
    _table(pdf, (58, 17, 20, 16, 21, 24, 24), ("LEFT", *["RIGHT"] * 6), [head, *rows], total)
    _note(pdf, "Выданы — прогнозы в пределах лимита сценария с датой расчёта в периоде. Факт — "
               "автоматический исход по журналу СМВУ: попадание или промах. «Неизвестно» — окно "
               "не закрыто или наблюдений не хватило; в точность не входит, промахом не "
               "считается. Точность = попало / (попало + промахов). Правила — те же, что у "
               "экрана «Качество» и итога журнала прогнозов (quality.tally).")


def _nice_step(peak: int) -> int:
    """Шаг сетки 1, 2 или 5 × 10^k, чтобы делений от нуля до пика было не больше пяти."""
    if peak <= 5:
        return 1
    raw = peak / 5
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)


def _chart(pdf: FPDF, data: ReportData) -> None:
    height, pad_top, below = 52.0, 6.0, 16.0
    _section(pdf, "2. Прогнозы по дням", need=pad_top + height + below + 10)
    days = data.days
    peak = max((d.forecasts for d in days), default=0)
    step = _nice_step(peak)
    ymax = step * max(1, math.ceil(peak / step))
    left = pdf.l_margin + 15
    width = pdf.w - pdf.r_margin - left
    top = pdf.get_y() + pad_top
    bottom = top + height
    slot = width / len(days)

    pdf.set_font(FONT, "", 7)
    pdf.set_line_width(0.2)
    for k in range(ymax // step + 1):
        value = k * step
        y = bottom - value / ymax * height
        pdf.set_draw_color(*(GRID if value else INK))
        pdf.line(left, y, left + width, y)
        pdf.set_text_color(*MUTED)
        label = num(value)
        pdf.text(left - 1.5 - pdf.get_string_width(label), y + 1.2, label)
    pdf.set_draw_color(*INK)
    pdf.line(left, top - 2, left, bottom)
    pdf.set_font(FONT, "", 7.5)
    axis_y = "Прогнозов за день, шт."
    with pdf.rotation(90, pdf.l_margin + 3, bottom):
        pdf.text(pdf.l_margin + 3 + (height - pdf.get_string_width(axis_y)) / 2,
                 bottom, axis_y)

    label_every = max(1, math.ceil(11 / slot))
    bar_w = min(slot * 0.62, 16)
    # Узкому столбцу подпись «нет расчёта» не влезает; значок над осью читался бы как
    # малое значение, поэтому такие дни помечены серой полосой под осью.
    wide = slot >= 11
    for i, point in enumerate(days):
        x = left + i * slot
        center = x + slot / 2
        if point.calculated:
            h = point.forecasts / ymax * height
            if h > 0:
                pdf.set_fill_color(*BAR)
                pdf.rect(center - bar_w / 2, bottom - h, bar_w, h, style="F")
            if slot >= 4.5:
                pdf.set_font(FONT, "B", 7 if slot >= 8 else 5.6)
                pdf.set_text_color(*INK)
                label = num(point.forecasts)
                pdf.text(center - pdf.get_string_width(label) / 2, bottom - h - 1.2, label)
        elif wide:
            pdf.set_font(FONT, "", 6.2)
            pdf.set_text_color(*MUTED)
            for n, word in enumerate(("нет", "расчёта")):
                pdf.text(center - pdf.get_string_width(word) / 2, bottom - 4.6 + 2.6 * n, word)
        else:
            pdf.set_fill_color(*RULE)
            pdf.rect(x, bottom + 0.5, slot, 1.2, style="F")
        if i % label_every == 0:
            pdf.set_font(FONT, "", 7)
            pdf.set_text_color(*INK)
            label = point.day.strftime("%d.%m")
            pdf.text(center - pdf.get_string_width(label) / 2, bottom + 4.6, label)
            if slot >= 9:
                pdf.set_text_color(*MUTED)
                wd = WEEKDAYS[point.day.weekday()]
                pdf.text(center - pdf.get_string_width(wd) / 2, bottom + 7.8, wd)
    pdf.set_font(FONT, "", 7.5)
    pdf.set_text_color(*MUTED)
    axis_x = "Дата расчёта прогноза, МСК"
    pdf.text(left + (width - pdf.get_string_width(axis_x)) / 2, bottom + 12, axis_x)
    pdf.set_y(bottom + below - 2)
    total = sum(d.forecasts for d in days)
    missing = sum(not d.calculated for d in days)
    marked = "подписаны «нет расчёта»" if wide else "отмечены серой полосой под осью"
    _note(pdf, f"Прогнозы всех сценариев в пределах лимита, всего {num(total)} — столько же, "
               "сколько в строке «Все сценарии» раздела 1. Ось начинается с нуля. "
               + (f"Дней без дневного расчёта — {missing}: столбца у них нет, это не ноль; "
                  f"на диаграмме они {marked}."
                  if missing else "Дневной расчёт был в каждый день периода."))


def _orders(pdf: FPDF, data: ReportData) -> None:
    _section(pdf, "3. Заявки по статусам", need=45)
    rows = [[title, num(n)] for title, n in data.orders_by_status]
    total = ["Всего", num(sum(n for _, n in data.orders_by_status))]
    _table(pdf, (60, 25), ("LEFT", "RIGHT"), [["Статус", "Заявок"], *rows], total)
    _note(pdf, "Заявки, сформированные по прогнозам с датой расчёта в периоде (по самому "
               "раннему прогнозу заявки); статус — на момент формирования отчёта.")


def _incidents(pdf: FPDF, data: ReportData) -> None:
    _section(pdf, "4. Тревожные сообщения по группам аварий", need=50)
    head = ["Группа аварии", "Тревожных сообщений", "С подсказкой ППР/ТО", "Доля ППР/ТО"]
    rows = [[r.title, num(r.alarms), num(r.planned_like),
             pct(r.planned_like / r.alarms if r.alarms else None)] for r in data.incidents]
    alarms = sum(r.alarms for r in data.incidents)
    planned = sum(r.planned_like for r in data.incidents)
    total = ["Все группы аварий", num(alarms), num(planned),
             pct(planned / alarms if alarms else None)]
    _table(pdf, (60, 38, 38, 27), ("LEFT", "RIGHT", "RIGHT", "RIGHT"), [head, *rows], total)
    everything = data.alarms
    _note(pdf, f"Всего тревожных сообщений за период, с группой аварии и без: "
               f"{num(everything.alarms)}, из них с подсказкой ППР/ТО — "
               f"{num(everything.planned_like)}. Группа аварии — по тексту состояния датчика "
               "(services/semantics.py). Подсказка «вероятно, ППР или ТО» — газ в будни с 9:00 "
               "до 14:59 или серия срабатываний за 10 минут; класс события она не меняет, "
               "решение принимает диспетчер.")


def _objects(pdf: FPDF, data: ReportData) -> None:
    _section(pdf, f"5. Топ-{len(data.top_objects) or 10} объектов по прогнозам и авариям",
             need=40)
    if not data.top_objects:
        _text(pdf, "За период нет ни прогнозов, ни тревожных сообщений групп аварий.")
        return
    head = ["№", "Объект", "Комплекс", "Прогнозов", "Аварий", "Сумма"]
    rows = [[str(n), r.name or f"объект {r.obj_id}", r.complex_name or "—", num(r.forecasts),
             num(r.incidents), num(r.total)] for n, r in enumerate(data.top_objects, 1)]
    _table(pdf, (8, 58, 50, 22, 20, 22), ("RIGHT", "LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
           [head, *rows])
    tail = (f" Ещё {num(data.incidents_without_object)} тревожных сообщений групп аварий пришли "
            "с каналов без строки в справочнике: объект у них неизвестен, в рейтинг они не "
            "вошли." if data.incidents_without_object else "")
    _note(pdf, "Порядок — по сумме прогнозов и тревожных сообщений групп аварий за период, при "
               "равной сумме — по числу прогнозов. Аварии — тревожные сообщения групп из "
               "раздела 4." + tail)
