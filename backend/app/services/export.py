"""Выгрузка журнала прогнозов в XLSX (ТЗ §8).

ЗАГЛУШКА — владелец BE-11 (C2).
Заменить: строки журнала за период from–to (те же поля, что в GET /forecasts, решение
и итог проверки), форматирование дат в МСК, ширину колонок.
Сейчас — книга с листом «Прогнозы», строкой заголовков и без данных.
Контракт: forecasts_xlsx(db, date_from, date_to) -> bytes не меняется, заголовки
COLUMNS — первая строка листа; тест tests/test_endpoints_shape.py должен остаться
зелёным.
"""
import io
from datetime import date

from openpyxl import Workbook
from sqlalchemy.orm import Session

SHEET = "Прогнозы"
COLUMNS = [
    "ID прогноза", "Сценарий", "Дата расчёта", "Окно с", "Окно по", "Горизонт, ч",
    "Тип оценки", "Риск", "Приоритет", "Ранг", "Объект", "Комплекс", "Канал",
    "Тип датчика", "Пикет", "Данные", "Решение", "Причина", "Итог по СМВУ",
    "Итог проверки", "Заявка", "Источник",
]


def forecasts_xlsx(db: Session, date_from: date | None, date_to: date | None) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = SHEET
    sheet.append(COLUMNS)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
