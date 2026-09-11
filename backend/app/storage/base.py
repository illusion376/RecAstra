"""
Абстракция хранилища.

Весь доступ к данным идёт через этот интерфейс. Рабочая реализация —
SqliteStorage (app/storage/sqlite.py), она же привязывает проекты к владельцу.
Чтобы перейти на PostgreSQL, достаточно написать PostgresStorage(Storage)
и подменить его в app/deps.py — роуты и конвейер не меняются.
"""

from __future__ import annotations

import unicodedata
from abc import ABC, abstractmethod
from typing import Optional

from app.schemas import Analysis, Item, ItemType, TranscriptSegment


class DuplicateProjectTitle(ValueError):
    """A project with this normalized title already exists."""


DUPLICATE_TITLE_MESSAGE = "Проект с таким названием уже существует. Выберите другое название."


def normalize_title(title: Optional[str]) -> str:
    """«  Новый   проект » → «Новый проект»: так название показывается."""
    return " ".join(unicodedata.normalize("NFKC", title or "").split())


def title_key(title: Optional[str]) -> str:
    """Ключ уникальности: регистр и лишние пробелы не делают название новым."""
    return normalize_title(title).casefold()


def filter_and_sort_items(
    items: list[Item],
    types: Optional[list[ItemType]] = None,
    statuses: Optional[list[str]] = None,
    role: Optional[str] = None,
    query: Optional[str] = None,
) -> list[Item]:
    """Общие для всех хранилищ фильтры и порядок элементов."""
    if types:
        wanted = {t.value for t in types}
        items = [i for i in items if i.type.value in wanted]
    if statuses:
        items = [i for i in items if i.status.value in set(statuses)]
    if role:
        needle = role.casefold()
        items = [i for i in items if i.role and needle in i.role.casefold()]
    if query:
        needle = query.casefold()
        items = [
            i
            for i in items
            if needle in i.title.casefold() or needle in i.text.casefold()
        ]

    # Стабильный порядок: сначала по типу (в порядке кейса), затем по времени
    # в разговоре — так список на фронте не «прыгает» между запросами.
    type_order = {t.value: n for n, t in enumerate(ItemType)}
    return sorted(
        items,
        key=lambda i: (
            type_order.get(i.type.value, 99),
            i.source.start if i.source and i.source.start is not None else 1e9,
            i.created_at,
        ),
    )


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
