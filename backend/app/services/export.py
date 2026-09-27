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
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .helpers import from_db

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
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    stmt = select(models.Forecast).where(models.Forecast.in_budget.is_(True))
    if date_from:
        stmt = stmt.where(models.Forecast.asof >= date_from)
    if date_to:
        stmt = stmt.where(models.Forecast.asof <= date_to)
    decisions = {d.forecast_id: d for d in db.scalars(select(models.Decision).order_by(
        models.Decision.created_at, models.Decision.id))}
    outcomes = {o.forecast_id: o for o in db.scalars(select(models.Outcome))}
    for forecast in db.scalars(stmt.order_by(models.Forecast.asof.desc(), models.Forecast.rank)):
        address = forecast.address or {}
        decision, outcome = decisions.get(forecast.id), outcomes.get(forecast.id)
        sheet.append([forecast.id, forecast.scenario, forecast.asof, from_db(forecast.valid_from),
                      from_db(forecast.valid_to), forecast.horizon_hours, forecast.score_type,
                      forecast.risk, forecast.priority_score, forecast.rank,
                      address.get("obj_name"), address.get("parent_name"), forecast.channel_id,
                      address.get("sensor_type"), address.get("picket"), forecast.data_status,
                      decision.action if decision else None, decision.reason_code if decision else None,
                      outcome.outcome_auto if outcome else None,
                      outcome.outcome_manual if outcome else None, None, forecast.source])
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(max(len(str(c.value or "")) for c in column) + 2, 45)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
