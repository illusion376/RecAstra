"""
Зависимости приложения — единственное место, где выбирается конкретная
реализация хранилища и LLM-провайдера.

Когда в команде появится PostgreSQL, достаточно заменить MemoryStorage
на PostgresStorage здесь. Роуты и конвейер об этом не узнают.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.core.llm.base import LLMProvider
from app.core.llm.mock_provider import MockProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.yandex_gpt import YandexGPTProvider
from app.storage.base import Storage
from app.storage.memory import MemoryStorage

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
