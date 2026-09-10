"""
Адаптер ответов распознавания речи → сегменты транскрипции.

Распознавание — это не анализ: оно стоит на месте, где раньше был Whisper,
и у каждого сервиса своя форма ответа. Здесь всё приводится к единому виду
TranscriptSegment, чтобы конвейеру было безразлично, чем распознавали.

Поддерживаются:

  Nexara — плоский список реплик с полем "time" вида "00:00:29"
      и меткой говорящего. Конца реплики не даёт, точность — целые секунды.

  Yandex SpeechKit v3 (recognizeFileAsync) — сообщения операции с final /
      finalRefinement, времена в startTimeMs / endTimeMs (миллисекунды строкой).

  Yandex SpeechKit v2 (longRunningRecognize) — chunks с alternatives,
      времена у отдельных слов строками вида «12.340s».

  Whisper — готовые сегменты со start / end, проходят насквозь.

  Плоский текст — если распознавание вернуло только строку.

Разметка говорящих переносится в поле speaker. Конвейеру это даёт заметный
прирост: становится видно, кто требует, а кто соглашается.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.schemas import TranscriptSegment

log = logging.getLogger(__name__)

# «12.340s» → 12.34
_DURATION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*s?\s*$")
# «00:00:29», «01:02:03.500», «12:34»
_CLOCK_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)\s*$")

# Скорость русской деловой речи, символов в секунду. Нужна только там,
# где сервис не сообщил конец реплики: по длине текста прикидываем,
# сколько она длилась. Значение занижено намеренно — лучше немного
# недооценить длительность, чем наехать на следующую реплику.
SPEECH_CHARS_PER_SEC = 14.0
MIN_SEGMENT_SEC = 0.8


def _to_seconds(value: Any) -> float | None:
    """Привести время к секундам. Понимает числа, «1.5s» и «00:01:29»."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    clock = _CLOCK_RE.match(text)
    if clock:
        hours = int(clock.group(1) or 0)
        minutes = int(clock.group(2))
        seconds = float(clock.group(3).replace(",", "."))
        return hours * 3600 + minutes * 60 + seconds

    m = _DURATION_RE.match(text)
    return float(m.group(1)) if m else None


def _ms_to_seconds(value: Any) -> float | None:
    seconds = _to_seconds(value)
    return None if seconds is None else seconds / 1000.0


def _speaker_of(*candidates: Any) -> str | None:
    """
    Собрать метку говорящего.

    Сервисы отдают обезличенные метки: «speaker_0», «1», «SPEAKER_00».
    Кто из них заказчик, а кто специалист, распознавание не знает — это
    решается позже, по смыслу реплик. Здесь только приводим к читаемому виду.
    """
    for c in candidates:
        if c in (None, "", 0, "0"):
            continue
        text = str(c).strip()
        if not text:
            continue
        # speaker_0 / SPEAKER_00 → «Говорящий 1» (нумерация с единицы,
        # иначе в интерфейсе появляется «Говорящий 0», что выглядит ошибкой).
        m = re.match(r"^speaker[_\-\s]?(\d+)$", text, re.IGNORECASE)
        if m:
            return f"Говорящий {int(m.group(1)) + 1}"
        return f"Говорящий {text}" if text.isdigit() else text
    return None


def _estimate_end(start: float, text: str, next_start: float | None) -> float:
    """
    Прикинуть конец реплики, когда сервис его не сообщил.

    Соблазнительно взять началом следующей реплики — но тогда между
    сегментами никогда не будет зазора, и пропадёт информация о паузах.
    А пауза — один из сигналов, по которым конвейер режет разговор на темы.
    Поэтому оцениваем по длине текста и лишь ограничиваем сверху началом
    следующей реплики, чтобы сегменты не наезжали друг на друга.
    """
    estimated = start + max(len(text) / SPEECH_CHARS_PER_SEC, MIN_SEGMENT_SEC)
    if next_start is not None and next_start > start:
        return min(estimated, next_start)
    return estimated


# ---------------------------------------------------------------------------
# Распознавание формы ответа
# ---------------------------------------------------------------------------


def _as_segment_list(payload: Any) -> list[dict[str, Any]] | None:
    """Достать плоский список реплик, если ответ устроен именно так."""
    raw = payload
    if isinstance(payload, dict):
        for key in ("segments", "result", "results", "chunks", "response", "data"):
            if isinstance(payload.get(key), list):
                raw = payload[key]
                break
        else:
            return None

    if not isinstance(raw, list) or not raw:
        return None
    if not all(isinstance(x, dict) for x in raw):
        return None
    # Реплика — это текст плюс какое-то время. Если есть alternatives,
    # это форма SpeechKit, её разбирает другая ветка.
    first = raw[0]
    if "alternatives" in first or "final" in first or "finalRefinement" in first:
        return None
    if "text" not in first and "word" not in first:
        return None
    return raw


def _looks_like_whisper(segments: list[dict[str, Any]]) -> bool:
    first = segments[0]
    return "start" in first or "startTime" in first


def _parse_flat_segments(raw: list[dict[str, Any]]) -> list[TranscriptSegment]:
    """
    Разобрать плоский список реплик: Whisper, Nexara и всё похожее.

    Начало берём из первого попавшегося поля времени, конец — из своего,
    если он есть, иначе оцениваем.
    """
    prepared: list[tuple[float, float | None, str, str | None]] = []

    for item in raw:
        text = str(item.get("text") or item.get("word") or "").strip()
        if not text:
            continue

        start = None
        for key in ("start", "time", "startTime", "start_time", "begin", "from"):
            if key in item:
                start = _to_seconds(item[key])
                if start is not None:
                    break
        if start is None and "startTimeMs" in item:
            start = _ms_to_seconds(item["startTimeMs"])

        end = None
        for key in ("end", "endTime", "end_time", "to"):
            if key in item:
                end = _to_seconds(item[key])
                if end is not None:
                    break
        if end is None and "endTimeMs" in item:
            end = _ms_to_seconds(item["endTimeMs"])

        speaker = _speaker_of(
            item.get("speaker"), item.get("speakerTag"), item.get("channelTag")
        )
        prepared.append((start if start is not None else 0.0, end, text, speaker))

    if not prepared:
        return []

    # Сортируем по времени: при раздельных дорожках порядок бывает по каналам.
    prepared.sort(key=lambda p: p[0])

    segments: list[TranscriptSegment] = []
    for n, (start, end, text, speaker) in enumerate(prepared):
        next_start = prepared[n + 1][0] if n + 1 < len(prepared) else None
        if end is None or end <= start:
            end = _estimate_end(start, text, next_start)
        segments.append(
            TranscriptSegment(
                id=n,
                start=round(start, 3),
                end=round(end, 3),
                text=text,
                speaker=speaker,
            )
        )
    return segments


# ---------------------------------------------------------------------------
# SpeechKit
# ---------------------------------------------------------------------------


def _alternative_text(alt: dict[str, Any]) -> str:
    text = str(alt.get("text") or "").strip()
    if text:
        return text
    words = alt.get("words") or []
    parts = [
        str(w.get("text") or w.get("word") or "").strip()
        for w in words
        if isinstance(w, dict)
    ]
    return " ".join(p for p in parts if p).strip()


def _times_from_words(alt: dict[str, Any]) -> tuple[float | None, float | None]:
    words = [w for w in (alt.get("words") or []) if isinstance(w, dict)]
    if not words:
        return None, None

    def start_of(w: dict[str, Any]) -> float | None:
        if "startTimeMs" in w:
            return _ms_to_seconds(w.get("startTimeMs"))
        return _to_seconds(w.get("startTime"))

    def end_of(w: dict[str, Any]) -> float | None:
        if "endTimeMs" in w:
            return _ms_to_seconds(w.get("endTimeMs"))
        return _to_seconds(w.get("endTime"))

    starts = [s for s in (start_of(w) for w in words) if s is not None]
    ends = [e for e in (end_of(w) for w in words) if e is not None]
    return (min(starts) if starts else None), (max(ends) if ends else None)


def _iter_chunks(payload: Any) -> list[dict[str, Any]]:
    """Вытащить куски распознавания SpeechKit из любой известной обёртки."""
    if isinstance(payload, list):
        chunks: list[dict[str, Any]] = []
        for entry in payload:
            chunks.extend(_iter_chunks(entry))
        return chunks

    if not isinstance(payload, dict):
        return []

    for key in ("final", "finalRefinement"):
        if key in payload:
            inner = payload[key]
            if key == "finalRefinement" and isinstance(inner, dict):
                inner = inner.get("normalizedText") or inner.get("final") or inner
            if isinstance(inner, dict):
                inner.setdefault("channelTag", payload.get("channelTag"))
                inner.setdefault("speakerTag", payload.get("speakerTag"))
                return [inner]

    for key in ("response", "result", "results", "chunks"):
        if key in payload:
            nested = _iter_chunks(payload[key])
            if nested:
                return nested

    if "alternatives" in payload:
        return [payload]

    return []


def _parse_speechkit_chunks(chunks: list[dict[str, Any]]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    cursor = 0.0

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        alternatives = chunk.get("alternatives") or []
        if not isinstance(alternatives, list) or not alternatives:
            continue
        alt = alternatives[0]
        if not isinstance(alt, dict):
            continue

        text = _alternative_text(alt)
        if not text:
            continue

        start = _ms_to_seconds(alt.get("startTimeMs"))
        end = _ms_to_seconds(alt.get("endTimeMs"))
        if start is None or end is None:
            w_start, w_end = _times_from_words(alt)
            start = start if start is not None else w_start
            end = end if end is not None else w_end
        if start is None:
            start = cursor
        if end is None or end < start:
            end = _estimate_end(start, text, None)

        segments.append(
            TranscriptSegment(
                id=len(segments),
                start=round(float(start), 3),
                end=round(float(end), 3),
                text=text,
                speaker=_speaker_of(
                    alt.get("speakerTag"),
                    chunk.get("speakerTag"),
                    chunk.get("channelTag"),
                ),
            )
        )
        cursor = segments[-1].end

    segments.sort(key=lambda s: (s.start, s.id))
    for n, seg in enumerate(segments):
        seg.id = n
    return segments


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def stt_to_segments(payload: Any) -> list[TranscriptSegment]:
    """
    Привести ответ любого поддержанного сервиса к сегментам транскрипции.

    Бросает ValueError, если форму разобрать не удалось: молча отдавать
    пустой список нельзя, иначе анализ «успешно» вернёт ноль требований,
    и разбираться в этом будут долго.
    """
    # Совсем простой случай: строка или {"text": "..."}.
    if isinstance(payload, str) and payload.strip():
        return [TranscriptSegment(id=0, start=0.0, end=0.0, text=payload.strip())]
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and not any(k in payload for k in ("segments", "chunks", "result", "results"))
    ):
        text = payload["text"].strip()
        if text:
            return [TranscriptSegment(id=0, start=0.0, end=0.0, text=text)]

    flat = _as_segment_list(payload)
    if flat is not None:
        segments = _parse_flat_segments(flat)
        if segments:
            if not _looks_like_whisper(flat):
                log.info(
                    "Распознавание не сообщило конец реплик — длительность оценена "
                    "по длине текста (%s сегментов)",
                    len(segments),
                )
            return segments

    chunks = _iter_chunks(payload)
    if chunks:
        segments = _parse_speechkit_chunks(chunks)
        if segments:
            return segments

    raise ValueError(
        "Не удалось разобрать ответ распознавания: не найдено ни списка реплик, "
        "ни alternatives. Передайте ответ сервиса целиком, без ручной обработки."
    )


# Прежнее имя — чтобы не ломать то, что уже написано под SpeechKit.
speechkit_to_segments = stt_to_segments
