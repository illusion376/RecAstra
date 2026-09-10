"""
Контракт API — единственный источник правды для фронта и для бэкендера с БД.

Всё, что отдаётся наружу и принимается снаружи, описано здесь.
FastAPI генерирует из этих моделей OpenAPI-схему: /docs, /openapi.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------


class ItemType(str, Enum):
    """Семь типов сущностей из пункта 2 кейса."""

    FUNCTIONAL_REQUIREMENT = "functional_requirement"  # функциональные требования
    USER_SCENARIO = "user_scenario"  # пользовательские сценарии
    USER_ROLE = "user_role"  # роли пользователей
    CONSTRAINT = "constraint"  # ограничения
    CONDITION = "condition"  # важные условия
    OPEN_QUESTION = "open_question"  # открытые вопросы
    AGREEMENT = "agreement"  # договорённости


ITEM_TYPE_LABELS: dict[str, str] = {
    ItemType.FUNCTIONAL_REQUIREMENT: "Функциональные требования",
    ItemType.USER_SCENARIO: "Пользовательские сценарии",
    ItemType.USER_ROLE: "Роли пользователей",
    ItemType.CONSTRAINT: "Ограничения",
    ItemType.CONDITION: "Важные условия",
    ItemType.OPEN_QUESTION: "Открытые вопросы",
    ItemType.AGREEMENT: "Договорённости",
}


class ItemStatus(str, Enum):
    """Пункт 4 кейса — проверка результата пользователем."""

    DRAFT = "draft"  # извлечено системой, не проверено
    CONFIRMED = "confirmed"  # пользователь подтвердил
    NEEDS_CLARIFICATION = "needs_clarification"  # помечено как требующее уточнения
    REJECTED = "rejected"  # мягкое удаление, можно вернуть


class Priority(str, Enum):
    """MoSCoW — «определение приоритетности требований» из доп. возможностей."""

    MUST = "must"
    SHOULD = "should"
    COULD = "could"
    WONT = "wont"


class Origin(str, Enum):
    LLM = "llm"  # извлечено конвейером
    MANUAL = "manual"  # добавлено пользователем руками
    EDITED = "edited"  # извлечено конвейером, затем отредактировано


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SpeakerRole(str, Enum):
    CUSTOMER = "customer"  # заказчик
    SPECIALIST = "specialist"  # технический специалист
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Транскрипция (вход)
# ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    """
    Один сегмент от Whisper. Ровно то, что отдаёт `whisper.transcribe()`
    в ключе `segments`, лишние поля можно не убирать — они игнорируются.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(..., description="Порядковый номер сегмента, от 0")
    start: float = Field(..., ge=0, description="Начало, секунды")
    end: float = Field(..., ge=0, description="Конец, секунды")
    text: str = Field(..., description="Расшифрованный текст сегмента")
    timing_estimated: bool = False
    speaker: Optional[str] = Field(
        None,
        description="Метка спикера, если есть диаризация (например 'SPEAKER_00'). "
        "Не обязательна — конвейер работает и без неё.",
    )

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class TranscriptMeta(BaseModel):
    """Необязательные метаданные записи — попадают в промпт как контекст."""

    model_config = ConfigDict(extra="ignore")

    title: Optional[str] = Field(None, description="Название встречи")
    project: Optional[str] = Field(None, description="Проект / продукт")
    language: str = Field("ru", description="Язык разговора")
    participants: Optional[list[str]] = Field(
        None, description="Участники встречи, если известны"
    )
    source_filename: Optional[str] = None
    duration: Optional[float] = Field(None, description="Длительность записи, секунды")


class AnalyzeRequest(BaseModel):
    """POST /api/v1/analysis — запуск анализа разговора."""

    segments: list[TranscriptSegment] = Field(
        ..., min_length=1, description="Сегменты транскрипции от Whisper"
    )
    meta: TranscriptMeta = Field(default_factory=TranscriptMeta)
    options: "AnalyzeOptions" = Field(default_factory=lambda: AnalyzeOptions())


class SttAnalyzeRequest(BaseModel):
    """
    POST /api/v1/analysis/from-stt

    То же, что AnalyzeRequest, но вместо готовых сегментов принимает сырой
    ответ сервиса распознавания — у каждого он свой. Конвертация происходит
    на сервере, фронту не нужно её повторять.
    """

    model_config = ConfigDict(populate_by_name=True)

    stt_response: Any = Field(
        None,
        description="Ответ сервиса распознавания целиком, как пришёл. Поддержаны: "
        "Nexara (реплики с полем time), Yandex SpeechKit v2 и v3, сегменты Whisper, "
        "плоский текст.",
    )
    # Прежнее имя поля, из времён когда ручка была только под SpeechKit.
    speechkit_response: Any = Field(None, deprecated=True, exclude=True)
    meta: TranscriptMeta = Field(default_factory=TranscriptMeta)
    options: "AnalyzeOptions" = Field(default_factory=lambda: AnalyzeOptions())

    @model_validator(mode="after")
    def _accept_legacy_field(self) -> "SttAnalyzeRequest":
        if self.stt_response is None and self.speechkit_response is not None:
            self.stt_response = self.speechkit_response
        if self.stt_response is None:
            raise ValueError("Не передан ответ распознавания в поле stt_response")
        return self


class AnalyzeOptions(BaseModel):
    """Переключатели конвейера. Все со значениями по умолчанию."""

    build_conversation_map: bool = Field(
        True,
        description="Проход А: построить карту разговора (темы, роли, глоссарий) "
        "и передать её как контекст в проход извлечения. Один дешёвый вызов "
        "на весь транскрипт, заметно поднимает связность результата.",
    )
    verify_quotes: bool = Field(
        True,
        description="Проверять, что цитата-якорь действительно есть в транскрипте. "
        "Защита от галлюцинаций: непроверенные элементы понижаются в confidence.",
    )
    drop_unverified: bool = Field(
        False,
        description="Полностью выбрасывать элементы, чью цитату не удалось найти. "
        "По умолчанию не выбрасываем, а помечаем — пользователь решит сам.",
    )
    deduplicate: bool = Field(True, description="Схлопывать дубли между чанками")
    llm_dedup: bool = Field(
        True,
        description="Досматривать моделью «серую зону» дедупликации — пары, "
        "похожие слишком сильно для разных требований и слишком слабо для "
        "автоматической склейки. Один дополнительный вызов на весь анализ.",
    )
    detect_contradictions: bool = Field(
        True, description="Искать противоречащие друг другу требования"
    )
    generate_user_stories: bool = Field(
        True, description="Формировать user story для функциональных требований"
    )
    max_chunk_chars: int = Field(
        4000, ge=800, le=20000, description="Максимальный размер смыслового чанка"
    )


AnalyzeRequest.model_rebuild()
SttAnalyzeRequest.model_rebuild()


# ---------------------------------------------------------------------------
# Трассировка до фрагмента разговора (пункт 5 кейса)
# ---------------------------------------------------------------------------


class SourceRef(BaseModel):
    """Откуда взялся элемент. Это то, что фронт показывает по клику «Источник»."""

    quote: str = Field(..., description="Цитата из разговора, обосновывающая элемент")
    segment_ids: list[int] = Field(
        default_factory=list, description="id сегментов транскрипции"
    )
    start: Optional[float] = Field(None, description="Начало фрагмента, секунды")
    end: Optional[float] = Field(None, description="Конец фрагмента, секунды")
    char_start: Optional[int] = Field(
        None, description="Смещение в склеенном тексте транскрипции"
    )
    char_end: Optional[int] = None
    verified: bool = Field(
        False, description="Цитата найдена в транскрипте (точно или нечётко)"
    )
    match_score: float = Field(
        0.0, ge=0.0, le=1.0, description="Насколько точно цитата совпала с текстом"
    )


class Item(BaseModel):
    """
    Универсальный элемент результата анализа.
    Один и тот же тип для всех семи категорий — фронту проще, а поле `type`
    определяет, как его показать.
    """

    id: str = Field(default_factory=lambda: new_id("item"))
    analysis_id: str
    type: ItemType

    title: str = Field(..., description="Короткая формулировка, до ~80 символов")
    text: str = Field(..., description="Полная формулировка в стиле ТЗ")

    role: Optional[str] = Field(
        None, description="Роль пользователя, к которой относится элемент"
    )
    actor: Optional[str] = Field(
        None, description="Кто это озвучил: заказчик или специалист"
    )
    priority: Optional[Priority] = None
    status: ItemStatus = ItemStatus.DRAFT
    origin: Origin = Origin.LLM

    confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="Уверенность конвейера в элементе"
    )
    tags: list[str] = Field(default_factory=list)

    user_story: Optional[str] = Field(
        None, description="«Как <роль>, я хочу <действие>, чтобы <ценность>»"
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="Критерии приёмки, если удалось вывести"
    )

    source: Optional[SourceRef] = None
    related_item_ids: list[str] = Field(
        default_factory=list, description="Связанные элементы (сценарий ↔ требование)"
    )

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Contradiction(BaseModel):
    """Автоматически выявленное противоречие между двумя элементами."""

    id: str = Field(default_factory=lambda: new_id("ctr"))
    item_id_a: str
    item_id_b: str
    explanation: str
    severity: float = Field(0.5, ge=0.0, le=1.0)


class ConversationMapTopic(BaseModel):
    title: str
    summary: str
    segment_ids: list[int] = Field(default_factory=list)
    start: Optional[float] = None
    end: Optional[float] = None


class ConversationMap(BaseModel):
    """Результат прохода А — общая карта разговора. Годится и как timeline на фронте."""

    summary: str = ""
    topics: list[ConversationMapTopic] = Field(default_factory=list)
    roles_mentioned: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Задача анализа
# ---------------------------------------------------------------------------


class JobProgress(BaseModel):
    stage: str = Field("", description="Человекочитаемое название текущего этапа")
    percent: int = Field(0, ge=0, le=100)
    chunks_total: int = 0
    chunks_done: int = 0


class AnalysisStats(BaseModel):
    segments: int = 0
    chunks: int = 0
    characters: int = 0
    duration_sec: Optional[float] = None
    items_by_type: dict[str, int] = Field(default_factory=dict)
    items_total: int = 0
    unverified_items: int = 0
    llm_calls: int = 0
    tokens_in: int = Field(0, description="Токенов на входе, если провайдер их отдаёт")
    tokens_out: int = 0
    elapsed_sec: float = 0.0
    provider: str = ""
    model: str = ""


class Analysis(BaseModel):
    """Полное состояние одного анализа."""

    id: str = Field(default_factory=lambda: new_id("an"))
    status: JobStatus = JobStatus.PENDING
    progress: JobProgress = Field(default_factory=JobProgress)
    meta: TranscriptMeta = Field(default_factory=TranscriptMeta)
    options: AnalyzeOptions = Field(default_factory=AnalyzeOptions)
    stats: AnalysisStats = Field(default_factory=AnalysisStats)
    conversation_map: Optional[ConversationMap] = None
    contradictions: list[Contradiction] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Ответы API
# ---------------------------------------------------------------------------


class AnalysisCreated(BaseModel):
    analysis_id: str
    status: JobStatus
    poll_url: str = Field(..., description="Куда опрашивать статус")


class AnalysisStatusResponse(BaseModel):
    analysis_id: str
    status: JobStatus
    progress: JobProgress
    stats: AnalysisStats
    error: Optional[str] = None


class GroupedItems(BaseModel):
    """Элементы, сгруппированные по семи категориям — под левое меню фронта."""

    functional_requirement: list[Item] = Field(default_factory=list)
    user_scenario: list[Item] = Field(default_factory=list)
    user_role: list[Item] = Field(default_factory=list)
    constraint: list[Item] = Field(default_factory=list)
    condition: list[Item] = Field(default_factory=list)
    open_question: list[Item] = Field(default_factory=list)
    agreement: list[Item] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """GET /api/v1/analysis/{id}/result — всё, что нужно для экрана ТЗ."""

    analysis: Analysis
    items: list[Item]
    grouped: GroupedItems


class ItemsResponse(BaseModel):
    total: int
    items: list[Item]


class ItemCreate(BaseModel):
    """Пункт 4: «добавлять требования»."""

    type: ItemType
    title: str
    text: str
    role: Optional[str] = None
    priority: Optional[Priority] = None
    status: ItemStatus = ItemStatus.CONFIRMED
    tags: list[str] = Field(default_factory=list)
    user_story: Optional[str] = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    source: Optional[SourceRef] = None


class ItemUpdate(BaseModel):
    """Пункт 4: «редактировать требования». Все поля необязательны — PATCH."""

    type: Optional[ItemType] = None
    title: Optional[str] = None
    text: Optional[str] = None
    role: Optional[str] = None
    priority: Optional[Priority] = None
    status: Optional[ItemStatus] = None
    tags: Optional[list[str]] = None
    user_story: Optional[str] = None
    acceptance_criteria: Optional[list[str]] = None
    related_item_ids: Optional[list[str]] = None


class FlagRequest(BaseModel):
    """Пункт 4: «отмечать информацию как требующую уточнения»."""

    needs_clarification: bool = True
    comment: Optional[str] = None


class SourceContext(BaseModel):
    """
    GET /api/v1/analysis/{id}/items/{item_id}/source
    Пункт 5: показать фрагмент разговора, на основании которого сформировано требование.
    """

    item_id: str
    source: Optional[SourceRef]
    segments: list[TranscriptSegment] = Field(
        default_factory=list, description="Сегменты фрагмента"
    )
    context_before: list[TranscriptSegment] = Field(default_factory=list)
    context_after: list[TranscriptSegment] = Field(default_factory=list)


class SearchHit(BaseModel):
    segment: TranscriptSegment
    highlight: str = Field(..., description="Текст с <mark>подсветкой</mark> совпадения")


class SearchResponse(BaseModel):
    """Доп. возможность из кейса: «поиск по транскрипции»."""

    query: str
    total: int
    hits: list[SearchHit]


class TranscriptResponse(BaseModel):
    analysis_id: str
    meta: TranscriptMeta
    segments: list[TranscriptSegment]
    full_text: str


class EnumsResponse(BaseModel):
    """GET /api/v1/meta/enums — чтобы фронт не хардкодил справочники."""

    item_types: list[dict[str, str]]
    statuses: list[str]
    priorities: list[str]
    origins: list[str]


class HealthResponse(BaseModel):
    status: str = "ok"
    provider: str
    model: str
    llm_ready: bool
    version: str


class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    extra: Optional[dict[str, Any]] = None
