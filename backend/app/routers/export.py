from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_perm
from ..services import export, report, report_pdf

router = APIRouter(tags=["export"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"


@router.get("/export/forecasts.xlsx", response_class=Response,
            responses={200: {"content": {XLSX: {}}}},
            dependencies=[Depends(require_perm("export"))])
def forecasts_xlsx(date_from: date | None = Query(None, alias="from"),
                   date_to: date | None = Query(None, alias="to"),
                   db: Session = Depends(get_db)) -> Response:
    body = export.forecasts_xlsx(db, date_from, date_to)
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="forecasts.xlsx"'})


@router.get("/export/report.pdf", response_class=Response,
            responses={200: {"content": {PDF: {}}}},
            dependencies=[Depends(require_perm("export"))])
def management_report_pdf(date_from: date | None = Query(
                              None, alias="from",
                              description="По умолчанию — семь суток до to включительно"),
                          date_to: date | None = Query(
                              None, alias="to", description="По умолчанию — демо-дата"),
                          db: Session = Depends(get_db)) -> Response:
    """Отчёт руководству за период (ТЗ §8): сценарии, прогнозы по дням, заявки по статусам,
    тревожные сообщения по группам аварий, топ-10 объектов. Выгрузка пишется в аудит.

    422 `from_after_to` — from позже to; 422 `period_too_long` — период длиннее
    366 суток (report.MAX_DAYS).
    """
    start, end = report.period(db, date_from, date_to)
    body = report_pdf.render(report.collect(db, start, end))
    name = f"report_{start:%Y%m%d}_{end:%Y%m%d}.pdf"
    return Response(body, media_type=PDF,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
