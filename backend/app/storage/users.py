"""
Пользователи — в JSON-файле на диске, чтобы учётки переживали перезапуск
сервера (встречи пока живут в памяти, а заново регистрироваться после
каждого --reload никто не станет).

Когда появится PostgreSQL, этот класс заменяется таблицей users —
ручки /auth зависят только от трёх методов ниже.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

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


class UserStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # --- чтение / запись файла -------------------------------------------

    def _load(self) -> list[StoredUser]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return [StoredUser.model_validate(row) for row in raw.get("users", [])]

    def _save(self, users: list[StoredUser]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"users": [u.model_dump(mode="json") for u in users]}
        temporary = self.path.with_name(f".{uuid4()}.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)  # атомарно: файл не останется битым
        finally:
            temporary.unlink(missing_ok=True)

    # --- API ---------------------------------------------------------------

    def get_by_email(self, email: str) -> Optional[StoredUser]:
        key = normalize_email(email)
        with self._lock:
            return next((u for u in self._load() if u.email == key), None)

    def get_by_id(self, user_id: str) -> Optional[StoredUser]:
        with self._lock:
            return next((u for u in self._load() if u.id == user_id), None)

    def create(self, email: str, name: str, password_hash: str) -> StoredUser:
        key = normalize_email(email)
        with self._lock:
            users = self._load()
            if any(u.email == key for u in users):
                raise EmailTaken("Пользователь с такой почтой уже зарегистрирован")
            user = StoredUser(
                id=str(uuid4()),
                email=key,
                name=name.strip(),
                created_at=datetime.now(timezone.utc),
                password_hash=password_hash,
            )
            users.append(user)
            self._save(users)
            return user


def load_or_create_secret(path: str | Path) -> str:
    """
    Секрет подписи токенов, если AUTH_SECRET не задан в .env.

    Хранится рядом с пользователями: случайный секрет на каждый запуск
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
