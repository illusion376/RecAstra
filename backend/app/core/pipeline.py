"""
Оркестрация конвейера анализа разговора.

    1. Нормализация транскрипции          normalize.py
    2. Смысловая сегментация              chunking.py
    3. Проход А: карта разговора          llm/prompts.py
    4. Проход Б: извлечение по чанкам     llm/prompts.py
    5. Проверка цитат и трассировка       verify.py
    6. Дедупликация                       dedup.py
    7. Связи и противоречия               здесь
    8. Статистика

Этап 4 идёт параллельно по чанкам; параллелизм ограничен настройкой
LLM_CONCURRENCY, чтобы не ловить 429 и не задушить локальную модель.
Падение отдельного чанка не роняет анализ целиком — остальные доезжают.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from app.config import Settings
from app.core.chunking import Chunk, chunk_transcript
from app.core.dedup import candidate_pairs, deduplicate, merge_pair
from app.core.llm.base import LLMError, LLMProvider
from app.core.llm.prompts import (
    CONTRADICTIONS_SYSTEM,
    EXTRACT_SYSTEM,
    MAP_SYSTEM,
    SAME_ITEMS_SYSTEM,
    build_contradictions_user,
    build_extract_user,
    build_map_user,
    build_same_items_user,
)
from app.core.normalize import TranscriptDoc, normalize
from app.core.verify import QuoteIndex, verify_quote
from app.schemas import (
    Analysis,
    AnalysisStats,
    Contradiction,
    ConversationMap,
    ConversationMapTopic,
    Item,
    ItemType,
    JobStatus,
    Origin,
    Priority,
    SourceRef,
    TranscriptSegment,
    utcnow,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int, int], Awaitable[None]]

_VALID_TYPES = {t.value for t in ItemType}
_VALID_PRIORITIES = {p.value for p in Priority}


class AnalysisPipeline:
    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.llm = provider
        self.s = settings

    async def run(
        self,
        analysis: Analysis,
        segments: list[TranscriptSegment],
        on_progress: Optional[ProgressCallback] = None,
    ) -> tuple[Analysis, list[Item]]:
        started = time.perf_counter()
        # Провайдер живёт дольше одного анализа, поэтому считаем вызовы дельтой.
        calls_at_start = self.llm.calls
        tokens_in_at_start = self.llm.tokens_in
        tokens_out_at_start = self.llm.tokens_out
        opts = analysis.options

        async def progress(stage: str, percent: int, done: int = 0, total: int = 0) -> None:
            if on_progress:
                await on_progress(stage, percent, done, total)

        # --- 1. Нормализация ---------------------------------------------
        await progress("Нормализация транскрипции", 5)
        doc = normalize(segments)
        if not doc.segments:
            raise ValueError("После очистки транскрипции не осталось текста")

        # --- 2. Сегментация ----------------------------------------------
        await progress("Разбор структуры разговора", 10)
        chunks = chunk_transcript(doc, max_chars=opts.max_chunk_chars)
        log.info("Транскрипт разбит на %s смысловых чанков", len(chunks))

        # --- 3. Карта разговора -------------------------------------------
        conv_map: Optional[ConversationMap] = None
        if opts.build_conversation_map:
            await progress("Построение карты разговора", 15)
            conv_map = await self._build_map(doc, analysis)
            analysis.conversation_map = conv_map

        # --- 4. Извлечение -------------------------------------------------
        await progress("Извлечение требований", 20, 0, len(chunks))
        index = QuoteIndex(doc)
        done = 0
        lock = asyncio.Lock()
        raw_items: list[Item] = []

        async def handle(chunk: Chunk) -> list[Item]:
            nonlocal done
            try:
                data = await self.llm.complete_json(
                    EXTRACT_SYSTEM, build_extract_user(chunk, conv_map, analysis.meta)
                )
                parsed = self._parse_items(data, analysis.id, chunk, index, opts)
            except (LLMError, ValueError) as exc:
                # Один битый чанк не должен ронять весь анализ.
                log.warning("Чанк %s не обработан: %s", chunk.index, exc)
                parsed = []
            async with lock:
                done += 1
                pct = 20 + int(55 * done / max(len(chunks), 1))
                await progress("Извлечение требований", pct, done, len(chunks))
            return parsed

        results = await asyncio.gather(*(handle(c) for c in chunks))
        for r in results:
            raw_items.extend(r)

        if not raw_items:
            log.warning("Конвейер не извлёк ни одного элемента")

        # --- 5/6. Дедупликация ---------------------------------------------
        merged = 0
        items = raw_items
        if opts.deduplicate and items:
            await progress("Схлопывание дублей", 78)
            items, merged = deduplicate(items, threshold=self.s.dedup_threshold)
            log.info("Схлопнуто дублей по лексике: %s", merged)

            if opts.llm_dedup:
                await progress("Проверка похожих формулировок", 84)
                removed = await self._llm_dedup(items)
                if removed:
                    merged += len(removed)
                    items = [i for i in items if i.id not in removed]
                    log.info("Схлопнуто дублей моделью: %s", len(removed))

        # --- 7. Связи и противоречия ---------------------------------------
        await progress("Поиск связей", 88)
        self._link_items(items)

        if opts.detect_contradictions and len(items) > 1:
            await progress("Поиск противоречий", 92)
            analysis.contradictions = await self._find_contradictions(items)

        # --- 8. Статистика --------------------------------------------------
        await progress("Сборка результата", 97)
        by_type: dict[str, int] = {t.value: 0 for t in ItemType}
        for it in items:
            by_type[it.type.value] += 1

        analysis.stats = AnalysisStats(
            segments=len(doc.segments),
            chunks=len(chunks),
            characters=len(doc.full_text),
            duration_sec=doc.duration,
            items_by_type=by_type,
            items_total=len(items),
            unverified_items=sum(
                1 for i in items if i.source and not i.source.verified
            ),
            llm_calls=self.llm.calls - calls_at_start,
            tokens_in=self.llm.tokens_in - tokens_in_at_start,
            tokens_out=self.llm.tokens_out - tokens_out_at_start,
            elapsed_sec=round(time.perf_counter() - started, 2),
            provider=self.llm.name,
            model=self.llm.model,
        )
        analysis.status = JobStatus.DONE
        analysis.finished_at = utcnow()
        await progress("Готово", 100, len(chunks), len(chunks))
        return analysis, items

    # ------------------------------------------------------------------
    # Проход А
    # ------------------------------------------------------------------

    async def _build_map(self, doc: TranscriptDoc, analysis: Analysis) -> ConversationMap:
        try:
            data = await self.llm.complete_json(
                MAP_SYSTEM,
                build_map_user(doc.full_text, analysis.meta, limit=self.s.map_context_chars),
            )
        except LLMError as exc:
            log.warning("Карту разговора построить не удалось: %s", exc)
            return ConversationMap()

        topics: list[ConversationMapTopic] = []
        for t in _as_list(data.get("topics")):
            if not isinstance(t, dict):
                continue
            seg_ids = [int(s) for s in _as_list(t.get("segment_ids")) if _is_int(s)]
            start, end = doc.time_range(seg_ids)
            topics.append(
                ConversationMapTopic(
                    title=str(t.get("title") or "").strip()[:120],
                    summary=str(t.get("summary") or "").strip(),
                    segment_ids=seg_ids,
                    start=start,
                    end=end,
                )
            )

        glossary = data.get("glossary")
        return ConversationMap(
            summary=str(data.get("summary") or "").strip(),
            topics=[t for t in topics if t.title],
            roles_mentioned=[
                str(r).strip() for r in _as_list(data.get("roles_mentioned")) if str(r).strip()
            ],
            glossary={str(k): str(v) for k, v in glossary.items()}
            if isinstance(glossary, dict)
            else {},
        )

    # ------------------------------------------------------------------
    # Разбор ответа модели
    # ------------------------------------------------------------------

    def _parse_items(
        self,
        data: dict[str, Any],
        analysis_id: str,
        chunk: Chunk,
        index: QuoteIndex,
        opts: Any,
    ) -> list[Item]:
        """
        Превратить сырой JSON модели в Item'ы.

        Модель может вернуть неизвестный тип, строку вместо списка, лишние поля.
        Всё это молча отбрасываем: один кривой элемент не должен ронять чанк.
        """
        out: list[Item] = []

        for raw in _as_list(data.get("items")):
            if not isinstance(raw, dict):
                continue

            type_value = str(raw.get("type") or "").strip()
            if type_value not in _VALID_TYPES:
                log.debug("Неизвестный тип элемента %r, пропускаю", type_value)
                continue

            text = str(raw.get("text") or "").strip()
            title = str(raw.get("title") or "").strip() or text[:80]
            if not text or len(text) < 8:
                continue

            quote = str(raw.get("quote") or "").strip()
            seg_ids = [int(s) for s in _as_list(raw.get("segment_ids")) if _is_int(s)]
            # Если модель не указала сегменты — привяжем хотя бы к чанку.
            seg_ids = [s for s in seg_ids if s in set(chunk.segment_ids)] or chunk.segment_ids[:1]

            source: Optional[SourceRef] = None
            confidence = _as_float(raw.get("confidence"), default=0.7)

            if quote:
                if opts.verify_quotes:
                    source = verify_quote(
                        quote, index, seg_ids, threshold=self.s.quote_match_threshold
                    )
                    if not source.verified:
                        # Цитаты нет в транскрипте — скорее всего, придумана.
                        if opts.drop_unverified:
                            continue
                        confidence = min(confidence, 0.4)
                else:
                    start, end = index.doc.time_range(seg_ids)
                    source = SourceRef(
                        quote=quote, segment_ids=seg_ids, start=start, end=end
                    )
            else:
                # Без цитаты трассировки нет — доверия тоже меньше.
                confidence = min(confidence, 0.5)
                start, end = index.doc.time_range(seg_ids)
                source = SourceRef(quote="", segment_ids=seg_ids, start=start, end=end)

            priority_value = str(raw.get("priority") or "").strip()
            out.append(
                Item(
                    analysis_id=analysis_id,
                    type=ItemType(type_value),
                    title=title[:200],
                    text=text,
                    role=_clean_str(raw.get("role")),
                    actor=_clean_str(raw.get("actor")),
                    priority=Priority(priority_value)
                    if priority_value in _VALID_PRIORITIES
                    else None,
                    confidence=round(max(0.0, min(confidence, 1.0)), 2),
                    origin=Origin.LLM,
                    tags=[str(t).strip() for t in _as_list(raw.get("tags")) if str(t).strip()][:5],
                    user_story=_clean_str(raw.get("user_story"))
                    if opts.generate_user_stories
                    else None,
                    acceptance_criteria=[
                        str(c).strip()
                        for c in _as_list(raw.get("acceptance_criteria"))
                        if str(c).strip()
                    ][:5],
                    source=source,
                )
            )

        return out

    # ------------------------------------------------------------------
    # Связи и противоречия
    # ------------------------------------------------------------------

    @staticmethod
    def _link_items(items: list[Item]) -> None:
        """
        Связать сценарии и роли с требованиями по совпадению роли и тегов.
        Дёшево и без вызовов модели, а на фронте даёт переходы между блоками ТЗ.
        """
        requirements = [i for i in items if i.type == ItemType.FUNCTIONAL_REQUIREMENT]
        for item in items:
            if item.type not in (ItemType.USER_SCENARIO, ItemType.USER_ROLE):
                continue
            related: list[str] = []
            for req in requirements:
                same_role = (
                    item.role and req.role and item.role.casefold() == req.role.casefold()
                )
                shared_tags = set(item.tags) & set(req.tags)
                if same_role or shared_tags:
                    related.append(req.id)
            item.related_item_ids = related[:10]

    async def _llm_dedup(self, items: list[Item]) -> set[str]:
        """
        Досмотр «серой зоны» дедупликации одним вызовом модели.

        Лексическая мера здесь бессильна: перефразировки и разные требования
        с общей лексикой лежат в одном диапазоне схожести. Зато отличить их —
        ровно то, что языковая модель делает хорошо.

        Возвращает id элементов, слитых в другие (их надо убрать из списка).
        """
        pairs = candidate_pairs(items)[:40]
        if not pairs:
            return set()

        by_id = {i.id: i for i in items}
        block = "\n".join(
            f"{n + 1}. A={a.id}: {a.text}\n   B={b.id}: {b.text}"
            for n, (a, b, _score) in enumerate(pairs)
        )

        try:
            data = await self.llm.complete_json(
                SAME_ITEMS_SYSTEM, build_same_items_user(block)
            )
        except LLMError as exc:
            log.warning("Досмотр дублей моделью не выполнен: %s", exc)
            return set()

        removed: set[str] = set()
        for raw in _as_list(data.get("same_pairs")):
            if not isinstance(raw, dict) or not raw.get("confident", True):
                continue
            a_id, b_id = str(raw.get("id_a") or ""), str(raw.get("id_b") or "")
            if a_id not in by_id or b_id not in by_id or a_id == b_id:
                continue
            # Уже слитые повторно не трогаем — иначе потеряем цепочку.
            if a_id in removed or b_id in removed:
                continue
            a, b = by_id[a_id], by_id[b_id]
            primary, dup = (a, b) if a.confidence >= b.confidence else (b, a)
            merge_pair(primary, dup)
            removed.add(dup.id)

        return removed

    async def _find_contradictions(self, items: list[Item]) -> list[Contradiction]:
        """Ищем противоречия только среди требований, ограничений и условий."""
        relevant = [
            i
            for i in items
            if i.type
            in (
                ItemType.FUNCTIONAL_REQUIREMENT,
                ItemType.CONSTRAINT,
                ItemType.CONDITION,
                ItemType.AGREEMENT,
            )
        ][:60]
        if len(relevant) < 2:
            return []

        block = "\n".join(f"- {i.id} [{i.type.value}] {i.text}" for i in relevant)
        try:
            data = await self.llm.complete_json(
                CONTRADICTIONS_SYSTEM, build_contradictions_user(block)
            )
        except LLMError as exc:
            log.warning("Поиск противоречий не выполнен: %s", exc)
            return []

        known = {i.id for i in relevant}
        out: list[Contradiction] = []
        for raw in _as_list(data.get("contradictions")):
            if not isinstance(raw, dict):
                continue
            a, b = str(raw.get("id_a") or ""), str(raw.get("id_b") or "")
            if a not in known or b not in known or a == b:
                continue
            out.append(
                Contradiction(
                    item_id_a=a,
                    item_id_b=b,
                    explanation=str(raw.get("explanation") or "").strip(),
                    severity=round(_as_float(raw.get("severity"), 0.5), 2),
                )
            )
        return out


# ---------------------------------------------------------------------------
# Мелкие помощники разбора
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _is_int(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in ("null", "none", "n/a", "-"):
        return None
    return text
