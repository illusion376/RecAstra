"""
Модели пользователя и секрет подписи токенов.

Сами пользователи хранятся в SQLite — см. app/storage/sqlite.py (таблица users).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class User(BaseModel):
    id: str
    email: str
    name: str
    created_at: datetime


class StoredUser(User):
    password_hash: str


class EmailTaken(ValueError):
    """Пользователь с такой почтой уже зарегистрирован."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def load_or_create_secret(path: str | Path) -> str:
    """
    Секрет подписи токенов, если AUTH_SECRET не задан в .env.

    Хранится рядом с базой: случайный секрет на каждый запуск
    разлогинивал бы всех при каждом --reload.
    """
    file = Path(path)
    try:
        value = file.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    file.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    file.write_text(value, encoding="utf-8")
    try:
        file.chmod(0o600)
    except OSError:
        pass
    return value
