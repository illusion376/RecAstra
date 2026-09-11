"""
In-memory реализация хранилища.

Рабочие данные живут в SQLite (app/storage/sqlite.py). Эта реализация
осталась для быстрых изолированных тестов: подставляется через
app.dependency_overrides[get_storage].
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.schemas import Analysis, Item, ItemType, TranscriptSegment
from app.storage.base import (
    DUPLICATE_TITLE_MESSAGE,
    DuplicateProjectTitle,
    Storage,
    filter_and_sort_items,
    normalize_title,
    title_key,
)


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self._analyses: dict[str, Analysis] = {}
        self._segments: dict[str, list[TranscriptSegment]] = {}
        self._items: dict[str, dict[str, Item]] = {}
        self._lock = asyncio.Lock()

    # --- Анализы ----------------------------------------------------------

    async def create_analysis(self, analysis: Analysis) -> Analysis:
        async with self._lock:
            title = normalize_title(analysis.meta.title)
            if title and any(
                other.id != analysis.id and title_key(other.meta.title) == title_key(title)
                for other in self._analyses.values()
            ):
                raise DuplicateProjectTitle(DUPLICATE_TITLE_MESSAGE)
            analysis.meta.title = title or None
            self._analyses[analysis.id] = analysis
            self._items.setdefault(analysis.id, {})
            self._segments.setdefault(analysis.id, [])
        return analysis

    async def get_analysis(self, analysis_id: str) -> Optional[Analysis]:
        return self._analyses.get(analysis_id)

    async def save_analysis(self, analysis: Analysis) -> Analysis:
        async with self._lock:
            self._analyses[analysis.id] = analysis
        return analysis

    async def list_analyses(self, limit: int = 50) -> list[Analysis]:
        items = sorted(
            self._analyses.values(), key=lambda a: a.created_at, reverse=True
        )
        return items[:limit]

    async def delete_analysis(self, analysis_id: str) -> bool:
        async with self._lock:
            existed = self._analyses.pop(analysis_id, None) is not None
            self._segments.pop(analysis_id, None)
            self._items.pop(analysis_id, None)
        return existed

    # --- Транскрипция -----------------------------------------------------

    async def save_segments(
        self, analysis_id: str, segments: list[TranscriptSegment]
    ) -> None:
        async with self._lock:
            self._segments[analysis_id] = segments

    async def get_segments(self, analysis_id: str) -> list[TranscriptSegment]:
        return self._segments.get(analysis_id, [])

    # --- Элементы ---------------------------------------------------------

    async def replace_items(self, analysis_id: str, items: list[Item]) -> None:
        async with self._lock:
            self._items[analysis_id] = {i.id: i for i in items}

    async def add_item(self, item: Item) -> Item:
        async with self._lock:
            self._items.setdefault(item.analysis_id, {})[item.id] = item
        return item

    async def get_item(self, analysis_id: str, item_id: str) -> Optional[Item]:
        return self._items.get(analysis_id, {}).get(item_id)

    async def save_item(self, item: Item) -> Item:
        async with self._lock:
            self._items.setdefault(item.analysis_id, {})[item.id] = item
        return item

    async def delete_item(self, analysis_id: str, item_id: str) -> bool:
        async with self._lock:
            return self._items.get(analysis_id, {}).pop(item_id, None) is not None

    async def list_items(
        self,
        analysis_id: str,
        types: Optional[list[ItemType]] = None,
        statuses: Optional[list[str]] = None,
        role: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[Item]:
        items = list(self._items.get(analysis_id, {}).values())
        return filter_and_sort_items(items, types, statuses, role, query)
