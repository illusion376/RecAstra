"""
Ручки по контракту фронтенда (API.md).

Это адаптер: снаружи — ровно тот API, под который уже написан `lib/api.ts`,
внутри — тот же конвейер, что и у `/api/v1/analysis`. Фронт менять не нужно,
богатый внутренний API остаётся доступен для будущего.

Порядок работы:
    POST /meetings        → 202 и Meeting со статусом processing
    (в фоне) распознавание → анализ → статус ready
    GET  /meetings/{id}   → фронт опрашивает раз в 3 секунды
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import Settings, get_settings
from app.core.meetings_mapper import card_to_item, to_meeting
from app.core.pipeline import AnalysisPipeline
from app.core.stt import stt_to_segments
from app.core.transcribe import (
    ALLOWED_EXTENSIONS,
    TranscriptionUnavailable,
    content_type_for,
    transcribe_file,
)
from app.deps import get_llm, get_storage
from app.schemas import Analysis, JobStatus, TranscriptMeta, TranscriptSegment, utcnow
from app.schemas_meetings import (
    CreateFromTranscriptRequest,
    Meeting,
    PutAnalysisRequest,
)
from app.storage.base import Storage

log = logging.getLogger(__name__)
router = APIRouter(tags=["meetings"])

CHUNK = 1024 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


def _media_dir(settings: Settings) -> Path:
    path = Path(settings.media_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audio_path(analysis_id: str, settings: Settings) -> Optional[Path]:
    """Найти сохранённую запись встречи, если она есть."""
    for candidate in _media_dir(settings).glob(f"{analysis_id}.*"):
        if candidate.is_file():
            return candidate
    return None


async def _build_meeting(
    analysis: Analysis, storage: Storage, settings: Settings
) -> Meeting:
    segments = await storage.get_segments(analysis.id)
    items = await storage.list_items(analysis.id)
    audio = _audio_path(analysis.id, settings)
    audio_url = f"/media/{audio.name}" if audio else None
    return to_meeting(analysis, segments, items, audio_url=audio_url)


async def _require(analysis_id: str, storage: Storage) -> Analysis:
    analysis = await storage.get_analysis(analysis_id)
    if analysis is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Встреча не найдена")
    return analysis


# ---------------------------------------------------------------------------
# Список и карточка
# ---------------------------------------------------------------------------


@router.get(
    "/meetings",
    response_model=list[Meeting],
    summary="Список встреч",
)
async def list_meetings(
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> list[Meeting]:
    """
    Контракт требует массив ПОЛНЫХ объектов: фронт открывает встречу прямо
    из списка и отдельный запрос за деталями не делает.
    """
    analyses = await storage.list_analyses(limit=200)
    return [await _build_meeting(a, storage, settings) for a in analyses]


@router.get("/meetings/{meeting_id}", response_model=Meeting, summary="Одна встреча")
async def get_meeting(
    meeting_id: str,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Meeting:
    analysis = await _require(meeting_id, storage)
    return await _build_meeting(analysis, storage, settings)


# ---------------------------------------------------------------------------
# Загрузка записи
# ---------------------------------------------------------------------------


@router.post(
    "/meetings",
    response_model=Meeting,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Загрузить запись встречи",
)
async def create_meeting(
    request: Request,
    title: str = Form(...),
    file: UploadFile = File(...),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Meeting:
    """
    Принимает запись и сразу отвечает, не дожидаясь обработки: у фронта
    таймаут 120 секунд, а расшифровка с анализом идут минуты. Дальше он
    опрашивает GET /meetings/{id}, пока статус не сменится с processing.
    """
    filename = Path(file.filename or "recording").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Формат {suffix or '(без расширения)'} не поддерживается. "
            f"Допустимы: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    analysis = Analysis(
        meta=TranscriptMeta(title=title.strip() or "Встреча", source_filename=filename)
    )
    analysis.status = JobStatus.PENDING
    analysis.progress.stage = "Загрузка записи"
    await storage.create_analysis(analysis)

    # Пишем потоком: файл может быть до 200 МБ, целиком в память его брать нельзя.
    target = _media_dir(settings) / f"{analysis.id}{suffix}"
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > limit:
                    out.close()
                    target.unlink(missing_ok=True)
                    await storage.delete_analysis(analysis.id)
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Файл больше {settings.max_upload_mb} МБ",
                    )
                out.write(chunk)
    finally:
        await file.close()

    if written == 0:
        target.unlink(missing_ok=True)
        await storage.delete_analysis(analysis.id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Файл пустой")

    log.info("Встреча %s: принято %.1f МБ (%s)", analysis.id, written / 1024 / 1024, filename)
    asyncio.create_task(_process_recording(analysis.id, target, storage, settings))

    return await _build_meeting(analysis, storage, settings)


@router.post(
    "/transcribe",
    summary="Только распознать запись, без анализа",
)
async def transcribe_only(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> dict:
    """
    Расшифровка без анализа — перенесено из отдельного `transcriber.py`,
    который делал то же самое своим приложением.

    Полезна, чтобы проверить качество распознавания и разметку говорящих,
    не запуская языковую модель и не тратя токены.
    """
    filename = Path(file.filename or "recording").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Формат {suffix or '(без расширения)'} не поддерживается",
        )

    # Пишем во временный файл: библиотека распознавания стримит запись
    # с диска, поэтому держать её целиком в памяти незачем.
    tmp = _media_dir(settings) / f"tmp_{uuid4().hex}{suffix}"
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(CHUNK):
                out.write(chunk)
        await file.close()

        try:
            segments = await transcribe_file(tmp, settings)
        except TranscriptionUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        return {
            "segments": [
                {
                    "id": s.id,
                    "start": s.start,
                    "end": s.end,
                    "speaker": s.speaker,
                    "text": s.text,
                }
                for s in segments
            ]
        }
    finally:
        tmp.unlink(missing_ok=True)


@router.post(
    "/meetings/from-transcript",
    response_model=Meeting,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Создать встречу из готовой расшифровки (без аудио)",
)
async def create_meeting_from_transcript(
    payload: CreateFromTranscriptRequest,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Meeting:
    """
    Сверх контракта. Нужна, пока распознавание не подключено: фронт и показ
    продукта могут работать на текстовой расшифровке, не упираясь в аудио.
    """
    try:
        segments = stt_to_segments(payload.stt_response)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    analysis = Analysis(
        meta=TranscriptMeta(
            title=payload.title,
            source_filename=payload.filename,
            duration=max((s.end for s in segments), default=0.0),
        )
    )
    await storage.create_analysis(analysis)
    await storage.save_segments(analysis.id, segments)

    asyncio.create_task(_run_pipeline(analysis.id, segments, storage, settings))
    return await _build_meeting(analysis, storage, settings)


# ---------------------------------------------------------------------------
# Фоновая обработка
# ---------------------------------------------------------------------------


async def _process_recording(
    analysis_id: str, path: Path, storage: Storage, settings: Settings
) -> None:
    """Распознать запись, затем проанализировать."""
    analysis = await storage.get_analysis(analysis_id)
    if analysis is None:
        return

    analysis.status = JobStatus.RUNNING
    analysis.progress.stage = "Распознавание речи"
    analysis.progress.percent = 2
    await storage.save_analysis(analysis)

    try:
        segments = await transcribe_file(path, settings)
    except TranscriptionUnavailable as exc:
        log.warning("Встреча %s: распознавание недоступно — %s", analysis_id, exc)
        analysis.status = JobStatus.FAILED
        analysis.error = str(exc)
        analysis.finished_at = utcnow()
        await storage.save_analysis(analysis)
        return
    except Exception as exc:  # noqa: BLE001 — фон не должен падать молча
        log.exception("Встреча %s: распознавание сорвалось", analysis_id)
        analysis.status = JobStatus.FAILED
        analysis.error = f"Ошибка распознавания: {type(exc).__name__}: {exc}"
        analysis.finished_at = utcnow()
        await storage.save_analysis(analysis)
        return

    await storage.save_segments(analysis_id, segments)
    analysis.meta.duration = max((s.end for s in segments), default=0.0)
    await storage.save_analysis(analysis)

    await _run_pipeline(analysis_id, segments, storage, settings)


async def _run_pipeline(
    analysis_id: str,
    segments: list[TranscriptSegment],
    storage: Storage,
    settings: Settings,
) -> None:
    """Анализ разговора. Та же логика, что и у /api/v1/analysis."""
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
        log.info("Встреча %s готова: %s требований", analysis_id, len(items))
    except Exception as exc:  # noqa: BLE001
        log.exception("Встреча %s: анализ сорвался", analysis_id)
        analysis.status = JobStatus.FAILED
        analysis.error = f"{type(exc).__name__}: {exc}"
        analysis.finished_at = utcnow()
        await storage.save_analysis(analysis)


# ---------------------------------------------------------------------------
# Сохранение правок
# ---------------------------------------------------------------------------


@router.put(
    "/meetings/{meeting_id}/analysis",
    response_model=Meeting,
    summary="Сохранить весь анализ целиком",
)
async def put_analysis(
    meeting_id: str,
    payload: PutAnalysisRequest,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Meeting:
    """
    Тело содержит весь актуальный массив, а не изменённый пункт: чего нет
    в списке — считается удалённым. Расшифровка при этом не меняется.
    """
    analysis = await _require(meeting_id, storage)
    segments = await storage.get_segments(meeting_id)
    previous = {i.id: i for i in await storage.list_items(meeting_id)}

    seen: set[str] = set()
    items = []
    for card in payload.analysis:
        if card.id in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Повторяющийся id карточки: {card.id}",
            )
        seen.add(card.id)
        items.append(card_to_item(card, meeting_id, segments, previous.get(card.id)))

    await storage.replace_items(meeting_id, items)
    log.info("Встреча %s: анализ заменён, карточек %s", meeting_id, len(items))
    return await _build_meeting(analysis, storage, settings)


# ---------------------------------------------------------------------------
# Раздача аудио
# ---------------------------------------------------------------------------


@router.get("/media/{name}", summary="Аудиозапись встречи")
async def get_media(
    name: str,
    request: Request,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Отдаёт запись плееру. Поддерживает Range: без него браузер не сможет
    перематывать длинную запись, а перемотка по клику на требование —
    это и есть связь с исходным разговором из кейса.

    Файл называется по ID встречи, и отдаём его только владельцу встречи.
    """
    safe = Path(name).name  # обрезаем любые «../» из имени
    path = _media_dir(settings) / safe
    if not path.is_file() or await storage.get_analysis(Path(safe).stem) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Запись не найдена")

    file_size = path.stat().st_size
    media_type = content_type_for(path)
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    match = _RANGE_RE.match(range_header.strip())
    if not match:
        raise HTTPException(
            status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Некорректный Range"
        )

    raw_start, raw_end = match.groups()
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else file_size - 1
    else:
        # «bytes=-500» — последние 500 байт.
        length = int(raw_end or 0)
        start = max(file_size - length, 0)
        end = file_size - 1

    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    def stream():
        remaining = end - start + 1
        with path.open("rb") as fh:
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        stream(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )
