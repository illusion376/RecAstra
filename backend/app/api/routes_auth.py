"""
Регистрация и вход.

    POST /auth/register  {name, email, password} → 201 {token, user}
    POST /auth/login     {email, password}       → 200 {token, user}
    GET  /auth/me        (Bearer-токен)          → user

Выхода на сервере нет: токен не хранится, фронт просто забывает его.
Остальные ручки (/meetings, /documents, /api/v1) требуют заголовок
«Authorization: Bearer <token>», см. app/main.py.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.core.auth import create_token, hash_password, verify_password
from app.deps import get_auth_secret, get_current_user, get_database
from app.storage.sqlite import Database
from app.storage.users import EmailTaken, StoredUser, User

router = APIRouter(prefix="/auth", tags=["авторизация"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Проверка пароля по «пустышке», когда почты нет: иначе по времени ответа
# было бы видно, зарегистрирован ли адрес.
_DUMMY_HASH = hash_password("dummy-password")


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, examples=["manager@example.com"])
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Некорректный адрес почты")
        return value


class RegisterRequest(Credentials):
    name: str = Field(min_length=1, max_length=100, examples=["Анна"])
    password: str = Field(min_length=6, max_length=128)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Укажите имя")
        return value


class AuthResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    user: User


def _session(user: StoredUser, settings: Settings) -> AuthResponse:
    token = create_token(
        user.id, get_auth_secret(settings), settings.auth_token_ttl_hours * 3600
    )
    return AuthResponse(token=token, user=User(**user.model_dump(exclude={"password_hash"})))


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Регистрация",
)
def register(
    body: RegisterRequest,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
) -> AuthResponse:
    try:
        user = db.create_user(body.email, body.name, hash_password(body.password))
    except EmailTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _session(user, settings)


@router.post("/login", response_model=AuthResponse, summary="Вход")
def login(
    body: Credentials,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
) -> AuthResponse:
    user = db.get_user_by_email(body.email)
    valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверная почта или пароль")
    return _session(user, settings)


@router.get("/me", response_model=User, summary="Текущий пользователь")
def me(user: User = Depends(get_current_user)) -> User:
    return user
