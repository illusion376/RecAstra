"""
Абстракция хранилища.

Я отвечаю за анализ и эндпоинты, PostgreSQL делает другой человек в команде.
Поэтому весь доступ к данным идёт через этот интерфейс: чтобы подключить БД,
достаточно написать PostgresStorage(Storage) и подменить его в app/deps.py —
роуты и конвейер не меняются.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.schemas import Analysis, Item, ItemType, TranscriptSegment


class DuplicateProjectTitle(ValueError):
    """A project with this normalized title already exists."""


class Storage(ABC):
    # --- Анализы ----------------------------------------------------------

    @abstractmethod
    async def create_analysis(self, analysis: Analysis) -> Analysis: ...

    @abstractmethod
    async def get_analysis(self, analysis_id: str) -> Optional[Analysis]: ...

    @abstractmethod
    async def save_analysis(self, analysis: Analysis) -> Analysis: ...

    @abstractmethod
    async def list_analyses(self, limit: int = 50) -> list[Analysis]: ...

    @abstractmethod
    async def delete_analysis(self, analysis_id: str) -> bool: ...

    # --- Транскрипция -----------------------------------------------------

    @abstractmethod
    async def save_segments(
        self, analysis_id: str, segments: list[TranscriptSegment]
    ) -> None: ...

    @abstractmethod
    async def get_segments(self, analysis_id: str) -> list[TranscriptSegment]: ...

    # --- Элементы ---------------------------------------------------------

    @abstractmethod
    async def replace_items(self, analysis_id: str, items: list[Item]) -> None:
        """Записать результат конвейера, затерев предыдущий."""

    @abstractmethod
    async def add_item(self, item: Item) -> Item: ...

    @abstractmethod
    async def get_item(self, analysis_id: str, item_id: str) -> Optional[Item]: ...

    @abstractmethod
    async def save_item(self, item: Item) -> Item: ...

    @abstractmethod
    async def delete_item(self, analysis_id: str, item_id: str) -> bool: ...

    @abstractmethod
    async def list_items(
        self,
        analysis_id: str,
        types: Optional[list[ItemType]] = None,
        statuses: Optional[list[str]] = None,
        role: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[Item]: ...
