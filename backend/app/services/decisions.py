"""История решений диспетчера и текущий итог проверки прогноза."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.forecasts import (
    DecisionIn,
    DecisionOut,
    ManualOutcomeRevisionOut,
    OutcomeIn,
    OutcomeOut,
)
from ..security import CurrentUser
from .forecasts import decision_out
from .helpers import assume_msk, from_db, now_utc, to_db


def _forecast_or_404(db: Session, forecast_id: str) -> models.Forecast:
    row = db.get(models.Forecast, forecast_id)
    if row is None:
        raise HTTPException(status_code=404, detail="forecast_not_found")
    return row


def create(db: Session, forecast_id: str, body: DecisionIn, user: CurrentUser) -> DecisionOut:
    _forecast_or_404(db, forecast_id)
    reason = db.get(models.ReasonCode, body.reason_code)
    if reason is None or body.action not in (reason.actions or []):
        raise HTTPException(status_code=422, detail="reason_code_not_allowed_for_action")
    row = models.Decision(forecast_id=forecast_id, action=body.action,
                          reason_code=body.reason_code, comment=body.comment,
                          author=user.login, created_at=now_utc(), source="live")
    db.add(row)
    db.commit()
    return decision_out(row)


def set_outcome(db: Session, forecast_id: str, body: OutcomeIn, user: CurrentUser) -> OutcomeOut:
    _forecast_or_404(db, forecast_id)
    row = db.get(models.Outcome, forecast_id)
    if row is None:
        row = models.Outcome(forecast_id=forecast_id)
        db.add(row)
    recorded_at = now_utc()
    event_at = to_db(assume_msk(body.event_at)) if body.event_at else None
    db.add(models.ManualOutcomeRevision(
        forecast_id=forecast_id, outcome=body.outcome, comment=body.comment,
        event_at=event_at, channel_id=body.channel, author=user.login,
        recorded_at=recorded_at, source="live"))
    row.outcome_manual = body.outcome
    row.comment = body.comment
    row.event_at = event_at
    row.channel_id = body.channel
    row.author = user.login
    row.updated_at = recorded_at
    row.source = "live"
    db.commit()
    return OutcomeOut(forecast_id=forecast_id, outcome_auto=row.outcome_auto,
                      outcome_manual=row.outcome_manual, comment=row.comment,
                      event_at=from_db(row.event_at), channel=row.channel_id,
                      author=row.author, updated_at=from_db(row.updated_at), source=row.source)


def outcome_history(db: Session, forecast_id: str) -> list[ManualOutcomeRevisionOut]:
    _forecast_or_404(db, forecast_id)
    rows = db.scalars(select(models.ManualOutcomeRevision).where(
        models.ManualOutcomeRevision.forecast_id == forecast_id)
        .order_by(models.ManualOutcomeRevision.id.desc()))
    return [ManualOutcomeRevisionOut(
        id=row.id, forecast_id=row.forecast_id, outcome=row.outcome,
        comment=row.comment, event_at=from_db(row.event_at), channel=row.channel_id,
        author=row.author, recorded_at=from_db(row.recorded_at), source=row.source) for row in rows]
