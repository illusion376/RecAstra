"""
Конфигурация. Всё через переменные окружения / .env.

Ключевая идея: LLM-провайдер ещё не выбран, поэтому он задаётся конфигом,
а не кодом. Любой OpenAI-совместимый endpoint подключается сменой двух строк
в .env — код трогать не нужно.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Приложение -------------------------------------------------------
    app_name: str = "Speech2Spec — анализ разговора"
    debug: bool = False
    cors_origins: str = Field(
        "*",
        description="Список origin через запятую. На проде сузить до домена фронта.",
    )

    # --- LLM --------------------------------------------------------------
    llm_provider: Literal["mock", "openai_compat", "yandex_gpt"] = Field(
        "mock",
        description=(
            "mock — детерминированный rule-based экстрактор, работает без сети и "
            "ключей; годится для разработки фронта и как аварийный запасной вариант. "
            "openai_compat — любой сервис с OpenAI-совместимым /chat/completions: "
            "OpenAI, Ollama, LM Studio, vLLM, DeepSeek, OpenRouter и т.д. "
            "yandex_gpt — Yandex Cloud Foundation Models (YandexGPT), нативный API."
        ),
    )
    llm_base_url: str = Field(
        "https://api.openai.com/v1",
        description="Например http://localhost:11434/v1 для Ollama",
    )
    llm_api_key: str = Field("", description="Пусто для локальных моделей")
    llm_model: str = Field("gpt-4o-mini", description="Имя модели у провайдера")
    llm_temperature: float = 0.1
    llm_timeout: float = Field(120.0, description="Таймаут одного запроса, секунды")
    llm_max_retries: int = Field(
        3, description="Повторы при сетевой ошибке или невалидном JSON"
    )
    llm_max_tokens: int = Field(
        4096,
        description="Потолок ответа. Для рассуждающих моделей (GPT-5 и подобные) "
        "ставьте с запасом: часть лимита они тратят на размышления, и при "
        "тесном лимите ответ приходит пустым.",
    )
    llm_token_param: Literal["max_tokens", "max_completion_tokens"] = Field(
        "max_tokens",
        description="Как называется поле лимита у модели. Угадывать не нужно: "
        "при отказе провайдер переключится сам и запомнит выбор на весь запуск.",
    )
    llm_supports_json_mode: bool = Field(
        True,
        description="Умеет ли провайдер response_format={'type':'json_object'}. "
        "Если нет — выставить false, парсер вытащит JSON из текста.",
    )
    llm_concurrency: int = Field(
        3, ge=1, le=16, description="Сколько чанков обрабатывать параллельно"
    )

    # --- YandexGPT (Yandex Cloud Foundation Models) ------------------------
    # Внимание: это НЕ SpeechKit. SpeechKit — распознавание речи, он стоит
    # на месте Whisper (см. app/core/stt.py). Языковая модель у Яндекса
    # называется YandexGPT и живёт по другому адресу.
    yandex_base_url: str = "https://llm.api.cloud.yandex.net"
    yandex_folder_id: str = Field(
        "", description="ID каталога в Yandex Cloud, обязателен"
    )
    yandex_api_key: str = Field(
        "",
        description="API-ключ сервисного аккаунта. Не истекает — предпочтительный "
        "способ для хакатона.",
    )
    yandex_iam_token: str = Field(
        "",
        description="Альтернатива API-ключу. Живёт 12 часов, потом запросы начнут "
        "падать с 401 — обновлять придётся вручную.",
    )
    yandex_model: str = Field(
        "yandexgpt",
        description="yandexgpt | yandexgpt-lite | yandexgpt-32k, либо готовый "
        "modelUri вида gpt://<folder_id>/<model>/latest",
    )
    yandex_model_version: str = Field(
        "latest", description="latest | rc | deprecated | номер версии"
    )
    yandex_json_mode: bool = Field(
        False,
        description="Просить строгий JSON полем jsonObject. Если каталог этого "
        "не поддерживает, запрос упадёт с 400 — провайдер сам повторит без него. "
        "Выключено по умолчанию: JSON и так вытаскивается из свободного текста.",
    )

    # --- Распознавание речи и загрузка файлов ------------------------------
    stt_provider: Literal["none", "nexara"] = Field(
        "none",
        description="none — загрузка аудио отключена, работаем с готовой "
        "расшифровкой. nexara — расшифровываем загруженный файл сами.",
    )
    nexara_api_key: str = ""
    nexara_task: str = Field(
        "diarize",
        description="diarize — с разделением говорящих. Без него анализ теряет "
        "понимание, кто требует, а кто соглашается.",
    )
    nexara_num_speakers: int = Field(
        2,
        ge=0,
        description="Сколько голосов на записи. На встрече заказчика с "
        "техническим специалистом их двое, и явное указание заметно снижает "
        "число ошибок на границах реплик. 0 — определять автоматически.",
    )
    nexara_roles: str = Field(
        "Заказчик,Специалист",
        description="Роли говорящих через запятую. Nexara размечает реплики "
        "сразу ими вместо обезличенных speaker_0 — это снимает отдельный шаг "
        "«кто из них заказчик». Пусто — оставить обезличенные метки.",
    )
    nexara_timeout: float = Field(
        600.0, description="Распознавание часовой записи идёт минуты, секунды"
    )
    transcript_language: str = Field(
        "ru", description="Язык записи. Пусто — определять автоматически."
    )
    media_dir: str = Field("media", description="Куда складывать загруженные записи")
    max_upload_mb: int = Field(
        200, ge=1, description="Потолок размера записи, мегабайты (как на фронте)"
    )

    # --- Конвейер ---------------------------------------------------------
    map_context_chars: int = Field(
        12000,
        ge=2000,
        le=60000,
        description="Сколько символов транскрипта уходит в проход «карта "
        "разговора». Ограничено контекстом модели: у yandexgpt он 8k токенов, "
        "поэтому для него значение стоит снизить до ~8000.",
    )
    max_segments: int = Field(
        20000, description="Защита от дурака: потолок на размер входа"
    )
    dedup_threshold: float = Field(
        0.82, ge=0.5, le=1.0, description="Порог схожести для схлопывания дублей"
    )
    quote_match_threshold: float = Field(
        0.62, ge=0.3, le=1.0, description="Порог нечёткого совпадения цитаты"
    )
    analysis_ttl_hours: int = Field(
        24, description="Сколько держать результат в памяти (для in-memory хранилища)"
    )

    @property
    def cors_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def yandex_model_uri(self) -> str:
        """Собрать modelUri. Готовый gpt://… из конфига пропускаем как есть."""
        if self.yandex_model.startswith("gpt://"):
            return self.yandex_model
        return f"gpt://{self.yandex_folder_id}/{self.yandex_model}/{self.yandex_model_version}"

    @property
    def llm_ready(self) -> bool:
        """Готов ли настоящий LLM-провайдер (mock готов всегда)."""
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "yandex_gpt":
            has_auth = bool(self.yandex_api_key or self.yandex_iam_token)
            has_folder = bool(self.yandex_folder_id) or self.yandex_model.startswith("gpt://")
            return has_auth and has_folder
        return bool(self.llm_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
