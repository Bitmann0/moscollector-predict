from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import OutcomeAuto, Page, Scenario
from ..schemas.forecasts import (
    DecisionIn,
    DecisionOut,
    ForecastCard,
    ForecastItem,
    OutcomeIn,
    OutcomeOut,
)
from ..security import CurrentUser, require_perm
from ..services import decisions, forecasts

router = APIRouter(tags=["forecasts"])

DecisionFilter = Literal["none", "any", "dispatch_crew", "remote_check", "defer", "reject"]


@router.get("/forecasts", response_model=Page[ForecastItem],
            dependencies=[Depends(require_perm("view"))])
def list_forecasts(
    scenario: Scenario | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    decision: DecisionFilter | None = None,
    outcome: OutcomeAuto | None = None,
    obj: str | None = None,
    group_by: Literal["obj", "case_key"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> Page[ForecastItem]:
    return forecasts.list_forecasts(db, scenario=scenario, date_from=date_from, date_to=date_to,
                                    decision=decision, outcome=outcome, obj=obj,
                                    group_by=group_by, page=page, page_size=page_size)


@router.get("/forecasts/{forecast_id}", response_model=ForecastCard,
            dependencies=[Depends(require_perm("view"))])
def get_card(forecast_id: str, db: Session = Depends(get_db)) -> ForecastCard:
    card = forecasts.get_card(db, forecast_id)
    if card is None:
        raise HTTPException(status_code=404, detail="forecast_not_found")
    return card


@router.post("/forecasts/{forecast_id}/decisions", response_model=DecisionOut, status_code=201)
def create_decision(forecast_id: str, body: DecisionIn, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_perm("decide"))) -> DecisionOut:
    return decisions.create(db, forecast_id, body, user)


@router.post("/forecasts/{forecast_id}/outcome", response_model=OutcomeOut)
def set_outcome(forecast_id: str, body: OutcomeIn, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_perm("outcome"))) -> OutcomeOut:
    return decisions.set_outcome(db, forecast_id, body, user)
