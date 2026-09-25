"""Вход и текущий пользователь. Живое."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import models, vocab
from ..config import get_settings
from ..db import get_db
from ..schemas.auth import LoginIn, UserOut
from ..security import COOKIE, CurrentUser, current_user, issue_session, verify_password

router = APIRouter(tags=["auth"])


def _out(user: CurrentUser) -> UserOut:
    return UserOut(login=user.login, name=user.name, role=user.role,
                   permissions=sorted(user.perms))


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> UserOut:
    row = db.get(models.User, body.login)
    if row is None or not verify_password(body.password, row.password_hash):
        raise HTTPException(status_code=401, detail="bad_credentials")
    settings = get_settings()
    response.set_cookie(COOKIE, issue_session(row.login, row.password_hash), httponly=True, samesite="lax",
                        secure=settings.cookie_secure, max_age=settings.session_hours * 3600)
    user = CurrentUser(row.login, row.name, row.role, vocab.permissions_of(row.role))
    request.state.user = user
    return _out(user)


@router.post("/auth/logout", status_code=204)
def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(COOKIE)
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(current_user)) -> UserOut:
    return _out(user)
