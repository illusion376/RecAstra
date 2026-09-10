"""
Перевод между внутренними моделями и контрактом фронтенда.

Внутри у конвейера семь типов, статусы, уверенность и источник со ссылкой
на сегменты. Фронт ждёт шесть контейнеров, плоский `source` числом и булев
флаг уточнения. Все расхождения сведены сюда, чтобы ни конвейер, ни фронт
не знали друг о друге.
"""

from __future__ import annotations

from typing import Optional

from app.schemas import (
    Analysis,
    Item,
    ItemStatus,
    ItemType,
    JobStatus,
    Origin,
    Priority,
    SourceRef,
    TranscriptSegment,
    utcnow,
)
from app.schemas_meetings import AnalysisCard, Meeting, MeetingStatus, TranscriptLine

# Семь наших типов против шести контейнеров фронта.
KIND_BY_TYPE: dict[ItemType, str] = {
    ItemType.FUNCTIONAL_REQUIREMENT: "functional",
    ItemType.USER_SCENARIO: "scenarios",
    ItemType.USER_ROLE: "roles",
    ItemType.CONSTRAINT: "constraints",
    ItemType.CONDITION: "conditions",
    ItemType.OPEN_QUESTION: "questions",
    ItemType.AGREEMENT: "agreements",
}

TYPE_BY_KIND: dict[str, ItemType] = {v: k for k, v in KIND_BY_TYPE.items()}
# Синонимы на случай, если фронт пришлёт единственное число или старое имя.
TYPE_BY_KIND.update(
    {
        "functional_requirement": ItemType.FUNCTIONAL_REQUIREMENT,
        "scenario": ItemType.USER_SCENARIO,
        "role": ItemType.USER_ROLE,
        "constraint": ItemType.CONSTRAINT,
        "condition": ItemType.CONDITION,
        "question": ItemType.OPEN_QUESTION,
        "agreement": ItemType.AGREEMENT,
    }
)

STATUS_BY_JOB: dict[JobStatus, MeetingStatus] = {
    JobStatus.PENDING: "processing",
    JobStatus.RUNNING: "processing",
    JobStatus.DONE: "ready",
    JobStatus.FAILED: "failed",
}


def line_id(segment_id: int) -> str:
    """Фронт ждёт строковые id реплик, у нас они целые."""
    return f"s{segment_id}"


def segment_id_from_line(line: str) -> Optional[int]:
    raw = line[1:] if line.startswith("s") else line
    try:
        return int(raw)
    except ValueError:
        return None


def to_transcript(segments: list[TranscriptSegment]) -> list[TranscriptLine]:
    """
    Реплики для фронта.

    Поле speaker в контракте обязательное, а распознавание метку даёт не
    всегда — подставляем нейтральную, иначе фронт получит невалидный объект.
    """
    return [
        TranscriptLine(
            id=line_id(s.id),
            speaker=s.speaker or "Участник",
            timing_estimated=s.timing_estimated,
            start=s.start,
            text=s.text,
        )
        for s in segments
    ]


def to_card(item: Item) -> AnalysisCard:
    """Одно требование → карточка контракта."""
    source_start = item.source.start if item.source else None
    resolved = None
    if item.type == ItemType.OPEN_QUESTION:
        # Для вопросов «закрыт» означает подтверждён пользователем.
        resolved = item.status == ItemStatus.CONFIRMED

    return AnalysisCard(
        id=item.id,
        kind=KIND_BY_TYPE[item.type],
        title=item.title,
        description=item.text,
        source=source_start,
        role=item.role,
        needs_clarification=item.status == ItemStatus.NEEDS_CLARIFICATION,
        resolved=resolved,
        quote=(item.source.quote or None) if item.source else None,
        verified=item.source.verified if item.source else None,
        confidence=item.confidence,
        priority=item.priority.value if item.priority else None,
        user_story=item.user_story,
        source_end=item.source.end if item.source else None,
    )


def to_analysis_cards(items: list[Item]) -> list[AnalysisCard]:
    """Отброшенные пользователем требования во фронт не уезжают."""
    return [to_card(i) for i in items if i.status != ItemStatus.REJECTED]


def to_meeting(
    analysis: Analysis,
    segments: list[TranscriptSegment],
    items: list[Item],
    audio_url: Optional[str] = None,
) -> Meeting:
    """Собрать объект встречи целиком."""
    status = STATUS_BY_JOB.get(analysis.status, "processing")

    duration = analysis.stats.duration_sec or analysis.meta.duration or 0.0
    if not duration and segments:
        duration = max(s.end for s in segments)

    return Meeting(
        id=analysis.id,
        title=analysis.meta.title or "Встреча",
        date=analysis.created_at.isoformat(),
        filename=analysis.meta.source_filename or "recording",
        duration=round(float(duration), 2),
        status=status,
        # Пока не готово — контракт требует именно пустые списки, не null.
        transcript=to_transcript(segments) if segments else [],
        analysis=to_analysis_cards(items) if status == "ready" else [],
        audio_url=audio_url,
        error=analysis.error if status == "failed" else None,
    )


# ---------------------------------------------------------------------------
# Обратный перевод: PUT /meetings/{id}/analysis
# ---------------------------------------------------------------------------


def _source_from_seconds(
    seconds: Optional[float], segments: list[TranscriptSegment], previous: Optional[SourceRef]
) -> Optional[SourceRef]:
    """
    Восстановить ссылку на фрагмент по одному числу.

    Фронт хранит источник как секунды и цитату не возвращает. Если требование
    не переехало на другое место записи, оставляем прежний источник целиком —
    иначе потеряли бы цитату и отметку о проверке. Если время изменилось,
    ищем сегмент, накрывающий этот момент.
    """
    if seconds is None:
        return previous

    if previous and previous.start is not None and abs(previous.start - seconds) < 0.5:
        return previous

    hit = next((s for s in segments if s.start <= seconds <= s.end), None)
    if hit is None:
        hit = min(segments, key=lambda s: abs(s.start - seconds), default=None)

    if hit is None:
        return SourceRef(quote="", segment_ids=[], start=seconds, end=None, verified=False)

    return SourceRef(
        quote=previous.quote if previous else "",
        segment_ids=[hit.id],
        start=hit.start,
        end=hit.end,
        # Источник переставлен вручную — прежняя проверка цитаты к нему
        # больше не относится.
        verified=False,
        match_score=0.0,
    )


def card_to_item(
    card: AnalysisCard,
    analysis_id: str,
    segments: list[TranscriptSegment],
    existing: Optional[Item] = None,
) -> Item:
    """Карточка от фронта → внутреннее требование."""
    item_type = TYPE_BY_KIND.get(card.kind, ItemType.FUNCTIONAL_REQUIREMENT)

    if card.needs_clarification:
        status = ItemStatus.NEEDS_CLARIFICATION
    elif card.resolved:
        status = ItemStatus.CONFIRMED
    elif existing is not None:
        status = (
            ItemStatus.DRAFT
            if existing.status == ItemStatus.NEEDS_CLARIFICATION
            else existing.status
        )
    else:
        # Пришло от человека — значит проверено человеком.
        status = ItemStatus.CONFIRMED

    priority = None
    if card.priority in {p.value for p in Priority}:
        priority = Priority(card.priority)
    elif existing is not None:
        priority = existing.priority

    changed = existing is not None and (
        existing.title != card.title or existing.text != card.description
    )
    if existing is None:
        origin = Origin.MANUAL
    else:
        origin = Origin.EDITED if changed else existing.origin

    fields: dict = {
        "id": card.id,
        "analysis_id": analysis_id,
        "type": item_type,
        "title": card.title,
        "text": card.description,
        "role": card.role or (existing.role if existing else None),
        "actor": existing.actor if existing else None,
        "priority": priority,
        "status": status,
        "origin": origin,
        "confidence": (existing.confidence if existing and origin == Origin.LLM else 1.0),
        "tags": list(existing.tags) if existing else [],
        "user_story": card.user_story or (existing.user_story if existing else None),
        "acceptance_criteria": list(existing.acceptance_criteria) if existing else [],
        "source": _source_from_seconds(
            card.source, segments, existing.source if existing else None
        ),
        "related_item_ids": list(existing.related_item_ids) if existing else [],
        "updated_at": utcnow(),
    }
    # У существующего требования сохраняем момент создания, у нового
    # его проставит модель значением по умолчанию.
    if existing is not None:
        fields["created_at"] = existing.created_at

    return Item(**fields)
