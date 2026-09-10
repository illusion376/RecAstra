"""
Шаг распознавания: аудиофайл → сегменты транскрипции.

Контракт фронта требует, чтобы бэкенд принимал аудио и сам его расшифровывал,
поэтому распознавание живёт здесь. Провайдер выбирается конфигом — так же,
как языковая модель.

Если распознавание не настроено, встреча получает статус failed с внятным
объяснением. Это лучше, чем притвориться, что всё в порядке, и отдать
пустую расшифровку: пользователь должен понимать, почему нет результата.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.stt import stt_to_segments
from app.schemas import TranscriptSegment

log = logging.getLogger(__name__)

# Что принимает фронт (см. docs/API.md). Сервер проверяет независимо:
# полагаться на проверку в браузере нельзя, запрос может прийти и мимо него.
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".mp4"}

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
}


class TranscriptionUnavailable(RuntimeError):
    """Распознавание не настроено или недоступно."""


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _segments_to_dicts(result: Any) -> list[dict]:
    """
    Привести ответ библиотеки к словарям для общего адаптера.

    В режиме diarize у сегмента появляется speaker; при включённом roles
    там оказывается не «speaker_0», а сразу роль — «Заказчик». Забираем
    через getattr, потому что набор полей зависит от режима.
    """
    raw = getattr(result, "segments", None) or getattr(result, "sentences", None)
    if not raw:
        raise TranscriptionUnavailable("Nexara вернула ответ без сегментов")

    out: list[dict] = []
    for s in raw:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        out.append(
            {
                "start": getattr(s, "start", None),
                "end": getattr(s, "end", None),
                "speaker": getattr(s, "speaker", None),
                "text": text,
            }
        )
    return out


def _nexara_sync(path: Path, settings: Settings, with_roles: bool) -> list[dict]:
    """
    Синхронный вызов Nexara. Библиотека блокирующая, поэтому наружу
    выставляется только через поток — иначе встанет весь сервер.
    """
    try:
        from nexara import Nexara  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise TranscriptionUnavailable(
            "Не установлена библиотека nexara. Поставьте её: pip install nexara"
        ) from exc

    client = Nexara(api_key=settings.nexara_api_key, timeout=settings.nexara_timeout)

    kwargs: dict[str, Any] = {
        # Путь, а не байты и не открытый файл: библиотека стримит его с диска,
        # и запись на 200 МБ не оседает в памяти целиком.
        "file": str(path),
        "task": settings.nexara_task,
        "response_format": "verbose_json",
    }
    if settings.transcript_language:
        kwargs["language"] = settings.transcript_language
    # На встрече заказчика с техническим специалистом голосов ровно два.
    # Когда их число неизвестно, диаризация чаще режет посреди фразы.
    if settings.nexara_num_speakers > 0:
        kwargs["num_speakers"] = settings.nexara_num_speakers
    if with_roles and settings.nexara_roles:
        # Размечает реплики сразу ролями, а не обезличенными speaker_0.
        # Это снимает отдельный проход «кто из них заказчик».
        kwargs["roles"] = [r.strip() for r in settings.nexara_roles.split(",") if r.strip()]

    result = client.transcriptions.create(**kwargs)
    return _segments_to_dicts(result)


async def transcribe_file(path: Path, settings: Settings) -> list[TranscriptSegment]:
    """Расшифровать файл. Бросает TranscriptionUnavailable, если не может."""
    if settings.stt_provider == "none":
        raise TranscriptionUnavailable(
            "Распознавание речи не настроено. Задайте в .env STT_PROVIDER=nexara "
            "и NEXARA_API_KEY, либо загружайте готовую расшифровку через "
            "POST /meetings/from-transcript"
        )

    if settings.stt_provider != "nexara":
        raise TranscriptionUnavailable(
            f"Неизвестный провайдер распознавания: {settings.stt_provider}"
        )

    if not settings.nexara_api_key:
        raise TranscriptionUnavailable("Не задан NEXARA_API_KEY")

    log.info("Отправляю %s в Nexara на распознавание", path.name)
    try:
        raw = await asyncio.to_thread(_nexara_sync, path, settings, True)
    except TranscriptionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Разметка ролями поддерживается не на всех тарифах и моделях.
        # Отказ из-за неё не должен стоить нам всей расшифровки: повторяем
        # без ролей, говорящие останутся обезличенными.
        if settings.nexara_roles:
            log.warning(
                "Nexara отказала при разметке ролями (%s), повторяю без неё", exc
            )
            raw = await asyncio.to_thread(_nexara_sync, path, settings, False)
        else:
            raise

    segments = stt_to_segments({"segments": raw})
    speakers = {s.speaker for s in segments if s.speaker}
    log.info(
        "Nexara вернула %s сегментов, говорящих: %s",
        len(segments),
        ", ".join(sorted(speakers)) if speakers else "не размечены",
    )
    return segments
