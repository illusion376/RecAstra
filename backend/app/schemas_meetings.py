"""
Схемы контракта фронтенда (API.md).

Это внешний слой: он повторяет то, что уже написано в `lib/api.ts`, вплоть
до названий полей. Внутренние модели (app/schemas.py) богаче — здесь только
то, что фронт умеет читать, плюс несколько необязательных полей сверх
контракта (цитата, отметка о проверке, уверенность). Лишние ключи JSON
фронту не мешают: неизвестные поля просто игнорируются, зато когда он будет
готов их показать, менять бэкенд не придётся.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

# Шесть контейнеров из API.md. "roles" контракт принимает «для совместимости»,
# но отдельного раздела под него на фронте нет — см. README, это открытый вопрос.
MeetingKind = Literal[
    "functional", "scenarios", "constraints", "conditions", "questions", "agreements", "roles"
]

MeetingStatus = Literal["processing", "ready", "failed"]


class TranscriptLine(BaseModel):
    """Реплика расшифровки. Все четыре поля обязательны по контракту."""

    id: str = Field(..., description="Уникален внутри встречи, например «s12»")
    speaker: str = Field(..., description="Кто говорит; пустым быть не должен")
    start: float = Field(..., ge=0, description="Секунды от начала записи, не строка")
    text: str


class AnalysisCard(BaseModel):
    """
    Карточка анализа.

    Обязательны id, kind, title, description, source. Остальное необязательно
    и опускается, когда значения нет: в контракте сказано, что null для
    необязательных полей не предусмотрен.
    """

    id: str
    kind: MeetingKind
    title: str
    description: str = ""
    source: Optional[float] = Field(
        None, description="Секунды от начала записи или null, если источника нет"
    )

    role: Optional[str] = None
    needs_clarification: bool = False
    resolved: Optional[bool] = Field(
        None, description="Только для вопросов: отмечен ли как закрытый"
    )

    # --- Сверх контракта -------------------------------------------------
    # Ради этих полей всё и затевалось: система обязана доказать каждое
    # требование цитатой из разговора, а не просто утверждать его.
    quote: Optional[str] = Field(None, description="Дословная цитата из разговора")
    verified: Optional[bool] = Field(
        None,
        description="Цитата найдена в расшифровке. false — вероятно, выдумка модели, "
        "стоит подсветить в интерфейсе",
    )
    confidence: Optional[float] = Field(None, description="Уверенность системы, 0..1")
    priority: Optional[str] = Field(None, description="must / should / could")
    user_story: Optional[str] = None
    source_end: Optional[float] = Field(None, description="Конец фрагмента, секунды")

    @model_serializer(mode="wrap")
    def _omit_empty_optionals(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        # source остаётся всегда, даже как null: контракт называет его обязательным.
        for key in (
            "role",
            "resolved",
            "quote",
            "verified",
            "confidence",
            "priority",
            "user_story",
            "source_end",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class Meeting(BaseModel):
    """Встреча целиком — то, что фронт получает и на список, и на карточку."""

    id: str
    title: str
    date: str = Field(..., description="ISO 8601 с часовым поясом")
    filename: str
    duration: float = Field(0, ge=0, description="Секунды")
    status: MeetingStatus
    transcript: list[TranscriptLine] = Field(default_factory=list)
    analysis: list[AnalysisCard] = Field(default_factory=list)
    audio_url: Optional[str] = None

    # Не из контракта: чтобы на фронте было что показать, когда обработка упала.
    error: Optional[str] = None

    @model_serializer(mode="wrap")
    def _omit_absent_audio(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        # «Если файла нет, не передавайте поле — null не поддерживается».
        if data.get("audio_url") is None:
            data.pop("audio_url", None)
        if data.get("error") is None:
            data.pop("error", None)
        return data


class PutAnalysisRequest(BaseModel):
    """
    PUT /meetings/{id}/analysis — тело содержит весь актуальный массив,
    а не один изменённый пункт. Сервер заменяет анализ целиком.
    """

    analysis: list[AnalysisCard] = Field(
        ..., description="Полный список карточек. Пустой список очищает анализ."
    )


class CreateFromTranscriptRequest(BaseModel):
    """
    Сверх контракта: создать встречу из готовой расшифровки, без аудио.

    Нужна, пока распознавание не подключено: фронт и демонстрация могут
    работать на текстовой расшифровке, не дожидаясь загрузки файлов.
    """

    title: str = "Встреча"
    filename: str = "transcript.json"
    stt_response: Any = Field(
        ..., description="Ответ распознавания в любом поддержанном виде"
    )
