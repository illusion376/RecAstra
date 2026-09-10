"""
HTTP-эндпоинты.

Покрывают пункты 2, 4 и 5 кейса плюс поиск по транскрипции:
  пункт 2 — запуск анализа и получение результата;
  пункт 4 — редактирование, добавление, удаление, пометка «требует уточнения»;
  пункт 5 — просмотр фрагмента разговора, из которого сделан элемент.

Анализ запускается фоновой задачей: разговор на час обрабатывается минуты,
и держать HTTP-соединение всё это время нельзя. Фронт получает analysis_id
и опрашивает статус.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import VERSION, Settings, get_settings
from app.core.pipeline import AnalysisPipeline
from app.core.stt import stt_to_segments
from app.deps import get_llm, get_storage
from app.schemas import (
    ITEM_TYPE_LABELS,
    Analysis,
    AnalysisCreated,
    AnalysisResult,
    AnalysisStatusResponse,
    AnalyzeRequest,
    EnumsResponse,
    GroupedItems,
    HealthResponse,
    Item,
    ItemCreate,
    ItemStatus,
    ItemType,
    ItemsResponse,
    FlagRequest,
    ItemUpdate,
    JobStatus,
    Origin,
    Priority,
    SearchHit,
    SearchResponse,
    SourceContext,
    SttAnalyzeRequest,
    TranscriptResponse,
    TranscriptSegment,
    utcnow,
)
from app.storage.base import Storage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["служебные"])
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    llm = get_llm(settings)
    return HealthResponse(
        provider=llm.name,
        model=llm.model,
        llm_ready=settings.llm_ready,
        version=VERSION,
    )


@router.get("/meta/enums", response_model=EnumsResponse, tags=["служебные"])
async def enums() -> EnumsResponse:
    """Справочники для фронта — чтобы не хардкодить строки в двух местах."""
    return EnumsResponse(
        item_types=[
            {"value": t.value, "label": ITEM_TYPE_LABELS[t.value]} for t in ItemType
        ],
        statuses=[s.value for s in ItemStatus],
        priorities=[p.value for p in Priority],
        origins=[o.value for o in Origin],
    )


# ---------------------------------------------------------------------------
# Пункт 2 — анализ разговора
# ---------------------------------------------------------------------------


@router.post(
    "/analysis",
    response_model=AnalysisCreated,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["анализ"],
    summary="Запустить анализ разговора",
)
async def create_analysis(
    payload: AnalyzeRequest,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> AnalysisCreated:
    if len(payload.segments) > settings.max_segments:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Слишком много сегментов, максимум {settings.max_segments}",
        )

    analysis = Analysis(meta=payload.meta, options=payload.options)
    analysis.status = JobStatus.PENDING
    await storage.create_analysis(analysis)
    await storage.save_segments(analysis.id, payload.segments)

    asyncio.create_task(_run_analysis(analysis.id, payload.segments, storage, settings))

    return AnalysisCreated(
        analysis_id=analysis.id,
        status=analysis.status,
        poll_url=f"/api/v1/analysis/{analysis.id}",
    )


@router.post(
    "/analysis/from-stt",
    response_model=AnalysisCreated,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["анализ"],
    summary="Запустить анализ из сырого ответа распознавания (Nexara, SpeechKit, Whisper)",
)
async def create_analysis_from_stt(
    payload: SttAnalyzeRequest,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> AnalysisCreated:
    """
    Тот же анализ, но входом служит ответ сервиса распознавания как есть.

    У каждого сервиса своя форма ответа, поэтому конвертация живёт здесь,
    а не на фронте: в одном месте и покрыта тестами. Если сервис не сообщил
    конец реплики (так делает Nexara), длительность оценивается по длине
    текста — иначе пропадёт информация о паузах, а по ней конвейер режет
    разговор на темы.
    """
    try:
        segments = stt_to_segments(payload.stt_response)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    log.info("Из ответа распознавания получено %s сегментов", len(segments))
    return await create_analysis(
        AnalyzeRequest(segments=segments, meta=payload.meta, options=payload.options),
        storage=storage,
        settings=settings,
    )


@router.post(
    "/transcripts/from-stt",
    response_model=list[TranscriptSegment],
    tags=["источник"],
    summary="Конвертировать ответ распознавания в сегменты, без запуска анализа",
)
async def convert_stt(payload: SttAnalyzeRequest) -> list[TranscriptSegment]:
    """
    Отладочная ручка: посмотреть, во что превратился ответ распознавания.

    Полезна, когда сервис поменяли или обновили: сразу видно, разобрались ли
    таймкоды и метки говорящих, до того как запускать анализ.
    """
    try:
        return stt_to_segments(payload.stt_response)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post(
    "/analysis/from-speechkit",
    response_model=AnalysisCreated,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["анализ"],
    summary="Устарело: используйте /analysis/from-stt",
    deprecated=True,
)
async def create_analysis_from_speechkit(
    payload: SttAnalyzeRequest,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> AnalysisCreated:
    """Прежнее имя ручки. Работает так же — оставлено, чтобы ничего не сломать."""
    return await create_analysis_from_stt(payload, storage=storage, settings=settings)


async def _run_analysis(
    analysis_id: str,
    segments: list[TranscriptSegment],
    storage: Storage,
    settings: Settings,
) -> None:
    """Фоновая задача. Все ошибки складываем в analysis.error, наружу не летят."""
    analysis = await storage.get_analysis(analysis_id)
    if analysis is None:
        return

    async def on_progress(stage: str, percent: int, done: int, total: int) -> None:
        analysis.progress.stage = stage
        analysis.progress.percent = percent
        analysis.progress.chunks_done = done
        analysis.progress.chunks_total = total
        await storage.save_analysis(analysis)

    analysis.status = JobStatus.RUNNING
    await storage.save_analysis(analysis)

    try:
        pipeline = AnalysisPipeline(get_llm(settings), settings)
        analysis, items = await pipeline.run(analysis, segments, on_progress)
        await storage.replace_items(analysis_id, items)
        await storage.save_analysis(analysis)
        log.info("Анализ %s завершён: %s элементов", analysis_id, len(items))
    except Exception as exc:  # noqa: BLE001 — фоновая задача не должна падать молча
        log.exception("Анализ %s провалился", analysis_id)
        analysis.status = JobStatus.FAILED
        analysis.error = f"{type(exc).__name__}: {exc}"
        analysis.finished_at = utcnow()
        await storage.save_analysis(analysis)


@router.get(
    "/analysis/{analysis_id}",
    response_model=AnalysisStatusResponse,
    tags=["анализ"],
    summary="Статус и прогресс анализа",
)
async def get_status(
    analysis_id: str, storage: Storage = Depends(get_storage)
) -> AnalysisStatusResponse:
    analysis = await _require_analysis(analysis_id, storage)
    return AnalysisStatusResponse(
        analysis_id=analysis.id,
        status=analysis.status,
        progress=analysis.progress,
        stats=analysis.stats,
        error=analysis.error,
    )


@router.get(
    "/analysis/{analysis_id}/result",
    response_model=AnalysisResult,
    tags=["анализ"],
    summary="Полный результат анализа",
)
async def get_result(
    analysis_id: str, storage: Storage = Depends(get_storage)
) -> AnalysisResult:
    analysis = await _require_analysis(analysis_id, storage)
    if analysis.status in (JobStatus.PENDING, JobStatus.RUNNING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Анализ ещё выполняется, опрашивайте GET /api/v1/analysis/{id}",
        )
    if analysis.status == JobStatus.FAILED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=analysis.error or "Анализ завершился с ошибкой",
        )

    items = await storage.list_items(analysis_id)
    return AnalysisResult(analysis=analysis, items=items, grouped=_group(items))


@router.get("/analysis", response_model=list[Analysis], tags=["анализ"])
async def list_analyses(
    limit: int = Query(50, ge=1, le=200), storage: Storage = Depends(get_storage)
) -> list[Analysis]:
    return await storage.list_analyses(limit=limit)


@router.delete(
    "/analysis/{analysis_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["анализ"]
)
async def delete_analysis(
    analysis_id: str, storage: Storage = Depends(get_storage)
) -> None:
    if not await storage.delete_analysis(analysis_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Анализ не найден")


# ---------------------------------------------------------------------------
# Пункт 4 — проверка и правка результата
# ---------------------------------------------------------------------------


@router.get(
    "/analysis/{analysis_id}/items",
    response_model=ItemsResponse,
    tags=["элементы"],
    summary="Список элементов с фильтрами",
)
async def list_items(
    analysis_id: str,
    type: Optional[list[ItemType]] = Query(None, description="Фильтр по типам"),
    status_filter: Optional[list[ItemStatus]] = Query(
        None, alias="status", description="Фильтр по статусам"
    ),
    role: Optional[str] = Query(None, description="Подстрока роли"),
    q: Optional[str] = Query(None, description="Поиск по тексту элемента"),
    storage: Storage = Depends(get_storage),
) -> ItemsResponse:
    await _require_analysis(analysis_id, storage)
    items = await storage.list_items(
        analysis_id,
        types=type,
        statuses=[s.value for s in status_filter] if status_filter else None,
        role=role,
        query=q,
    )
    return ItemsResponse(total=len(items), items=items)


@router.post(
    "/analysis/{analysis_id}/items",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    tags=["элементы"],
    summary="Добавить требование вручную",
)
async def add_item(
    analysis_id: str, payload: ItemCreate, storage: Storage = Depends(get_storage)
) -> Item:
    await _require_analysis(analysis_id, storage)
    item = Item(
        analysis_id=analysis_id,
        origin=Origin.MANUAL,
        confidence=1.0,  # добавил человек — сомнений нет
        **payload.model_dump(),
    )
    return await storage.add_item(item)


@router.patch(
    "/analysis/{analysis_id}/items/{item_id}",
    response_model=Item,
    tags=["элементы"],
    summary="Отредактировать требование",
)
async def update_item(
    analysis_id: str,
    item_id: str,
    payload: ItemUpdate,
    storage: Storage = Depends(get_storage),
) -> Item:
    item = await _require_item(analysis_id, item_id, storage)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return item

    for field, value in changes.items():
        setattr(item, field, value)

    # Правка человеком перебивает оценку модели: элемент больше не «сырой».
    if item.origin == Origin.LLM:
        item.origin = Origin.EDITED
    if {"title", "text", "type"} & changes.keys():
        item.confidence = 1.0
    item.updated_at = utcnow()
    return await storage.save_item(item)


@router.delete(
    "/analysis/{analysis_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["элементы"],
    summary="Удалить требование",
)
async def delete_item(
    analysis_id: str,
    item_id: str,
    hard: bool = Query(
        False,
        description="true — стереть насовсем; по умолчанию мягкое удаление "
        "со статусом rejected, чтобы можно было вернуть",
    ),
    storage: Storage = Depends(get_storage),
) -> None:
    item = await _require_item(analysis_id, item_id, storage)
    if hard:
        await storage.delete_item(analysis_id, item_id)
        return
    item.status = ItemStatus.REJECTED
    item.updated_at = utcnow()
    await storage.save_item(item)


@router.post(
    "/analysis/{analysis_id}/items/{item_id}/flag",
    response_model=Item,
    tags=["элементы"],
    summary="Отметить как требующее уточнения",
)
async def flag_item(
    analysis_id: str,
    item_id: str,
    payload: FlagRequest,
    storage: Storage = Depends(get_storage),
) -> Item:
    item = await _require_item(analysis_id, item_id, storage)
    item.status = (
        ItemStatus.NEEDS_CLARIFICATION if payload.needs_clarification else ItemStatus.CONFIRMED
    )
    if payload.comment:
        tag = f"уточнить: {payload.comment}"
        if tag not in item.tags:
            item.tags.append(tag)
    item.updated_at = utcnow()
    return await storage.save_item(item)


# ---------------------------------------------------------------------------
# Пункт 5 — связь с исходным разговором
# ---------------------------------------------------------------------------


@router.get(
    "/analysis/{analysis_id}/items/{item_id}/source",
    response_model=SourceContext,
    tags=["источник"],
    summary="Фрагмент разговора, из которого сформирован элемент",
)
async def get_source(
    analysis_id: str,
    item_id: str,
    context: int = Query(2, ge=0, le=20, description="Сколько сегментов вокруг"),
    storage: Storage = Depends(get_storage),
) -> SourceContext:
    item = await _require_item(analysis_id, item_id, storage)
    segments = await storage.get_segments(analysis_id)

    if not item.source or not item.source.segment_ids:
        return SourceContext(item_id=item_id, source=item.source)

    wanted = set(item.source.segment_ids)
    positions = [i for i, s in enumerate(segments) if s.id in wanted]
    if not positions:
        return SourceContext(item_id=item_id, source=item.source)

    first, last = min(positions), max(positions)
    return SourceContext(
        item_id=item_id,
        source=item.source,
        segments=segments[first : last + 1],
        context_before=segments[max(0, first - context) : first],
        context_after=segments[last + 1 : last + 1 + context],
    )


@router.get(
    "/analysis/{analysis_id}/transcript",
    response_model=TranscriptResponse,
    tags=["источник"],
    summary="Транскрипция целиком",
)
async def get_transcript(
    analysis_id: str, storage: Storage = Depends(get_storage)
) -> TranscriptResponse:
    analysis = await _require_analysis(analysis_id, storage)
    segments = await storage.get_segments(analysis_id)
    return TranscriptResponse(
        analysis_id=analysis_id,
        meta=analysis.meta,
        segments=segments,
        full_text=" ".join(s.text for s in segments),
    )


@router.get(
    "/analysis/{analysis_id}/search",
    response_model=SearchResponse,
    tags=["источник"],
    summary="Поиск по транскрипции",
)
async def search_transcript(
    analysis_id: str,
    q: str = Query(..., min_length=2, description="Поисковый запрос"),
    limit: int = Query(50, ge=1, le=500),
    storage: Storage = Depends(get_storage),
) -> SearchResponse:
    await _require_analysis(analysis_id, storage)
    segments = await storage.get_segments(analysis_id)

    pattern = re.compile(re.escape(q), re.IGNORECASE)
    hits: list[SearchHit] = []
    for seg in segments:
        if pattern.search(seg.text):
            hits.append(
                SearchHit(
                    segment=seg,
                    highlight=pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", seg.text),
                )
            )
        if len(hits) >= limit:
            break

    return SearchResponse(query=q, total=len(hits), hits=hits)


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------


async def _require_analysis(analysis_id: str, storage: Storage) -> Analysis:
    analysis = await storage.get_analysis(analysis_id)
    if analysis is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Анализ не найден")
    return analysis


async def _require_item(analysis_id: str, item_id: str, storage: Storage) -> Item:
    await _require_analysis(analysis_id, storage)
    item = await storage.get_item(analysis_id, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Элемент не найден")
    return item


def _group(items: list[Item]) -> GroupedItems:
    grouped = GroupedItems()
    for item in items:
        getattr(grouped, item.type.value).append(item)
    return grouped
