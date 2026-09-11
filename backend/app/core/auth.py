"""
Пароли и токены — только стандартная библиотека, без новых зависимостей.

Пароль хранится как PBKDF2-SHA256 с солью. Токен — подписанная HMAC
строка «данные.подпись», по устройству это упрощённый JWT: сервер ничего
не хранит о выданных токенах, а подделать подпись без секрета нельзя.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

PBKDF2_ITERATIONS = 390_000
_SCHEME = "pbkdf2_sha256"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{_SCHEME}${PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != _SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64decode(salt), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Сравнение за постоянное время — чтобы не подсказывать пароль таймингом.
    return hmac.compare_digest(digest, _b64decode(expected))


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------


def _sign(payload: str, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())


def create_token(user_id: str, secret: str, ttl_seconds: int) -> str:
    body = {"sub": user_id, "exp": int(time.time()) + ttl_seconds}
    payload = _b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign(payload, secret)}"


def read_token(token: str, secret: str) -> Optional[str]:
    """ID пользователя из токена или None, если токен подделан или истёк."""
    try:
        payload, signature = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None
    try:
        body = json.loads(_b64decode(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("sub"), str):
        return None
    if not isinstance(body.get("exp"), int) or body["exp"] < time.time():
        return None
    return body["sub"]
