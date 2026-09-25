from pydantic import BaseModel, Field

from .common import Role


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    login: str
    name: str
    role: Role
    permissions: list[str]
