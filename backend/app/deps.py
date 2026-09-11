"""
Зависимости приложения — единственное место, где выбирается конкретная
реализация хранилища и LLM-провайдера.

Когда в команде появится PostgreSQL, достаточно заменить MemoryStorage
на PostgresStorage здесь. Роуты и конвейер об этом не узнают.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.core.auth import read_token
from app.core.llm.base import LLMProvider
from app.core.llm.mock_provider import MockProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.yandex_gpt import YandexGPTProvider
from app.storage.base import Storage
from app.storage.memory import MemoryStorage
from app.storage.users import User, UserStore, load_or_create_secret

_storage: Storage = MemoryStorage()


def get_storage() -> Storage:
    return _storage


def set_storage(storage: Storage) -> None:
    """Точка подмены — для тестов и для перехода на PostgreSQL."""
    global _storage
    _storage = storage


@lru_cache
def _provider_for(provider_name: str) -> LLMProvider:
    settings = get_settings()
    if provider_name == "openai_compat":
        return OpenAICompatProvider(settings)
    if provider_name == "yandex_gpt":
        return YandexGPTProvider(settings)
    return MockProvider()


def get_llm(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    return _provider_for(s.llm_provider)


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------


@lru_cache
def _user_store_for(path: str) -> UserStore:
    return UserStore(path)


def get_user_store(settings: Settings = Depends(get_settings)) -> UserStore:
    return _user_store_for(settings.users_file)


@lru_cache
def _secret_for(users_file: str) -> str:
    return load_or_create_secret(Path(users_file).with_name(".auth_secret"))


def get_auth_secret(settings: Settings) -> str:
    return settings.auth_secret or _secret_for(settings.users_file)


# auto_error=False: без заголовка отвечаем своим 401 на русском, а не 403 FastAPI.
_bearer = HTTPBearer(auto_error=False)

GUEST = User(
    id="guest",
    email="guest@localhost",
    name="Гость",
    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    access_token: Optional[str] = Query(
        None,
        include_in_schema=False,
        description="Запасной способ передать токен — для <audio src>, "
        "который не умеет слать заголовки",
    ),
    settings: Settings = Depends(get_settings),
    users: UserStore = Depends(get_user_store),
) -> User:
    """Текущий пользователь по токену «Authorization: Bearer …»."""
    if not settings.auth_required:
        return GUEST
    token = credentials.credentials if credentials else access_token
    if not token:
        raise _unauthorized("Требуется вход в систему")
    user_id = read_token(token, get_auth_secret(settings))
    if user_id is None:
        raise _unauthorized("Сессия истекла или недействительна. Войдите снова")
    user = users.get_by_id(user_id)
    if user is None:
        raise _unauthorized("Пользователь не найден. Войдите снова")
    return User(**user.model_dump(exclude={"password_hash"}))
