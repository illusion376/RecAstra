"""
Точка входа FastAPI.

    uvicorn app.main:app --reload
    http://localhost:8000/docs   — интерактивная документация для фронта
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_documents import router as documents_router
from app.api.routes import router
from app.api.routes_meetings import router as meetings_router
from app.config import VERSION, get_settings
from app.deps import get_llm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("app")

DESCRIPTION = """
Модуль **«Анализ разговора»** (пункт 2 кейса 3 «Из разговора — в ТЗ»).

На вход — сегменты транскрипции от Whisper, на выход — структурированные
требования семи типов, каждое с цитатой-якорем и таймкодом исходного
фрагмента.

**Основной сценарий для фронта:**

1. `POST /api/v1/analysis` — отправить сегменты, получить `analysis_id`;
2. `GET /api/v1/analysis/{id}` — опрашивать, пока `status` не станет `done`;
3. `GET /api/v1/analysis/{id}/result` — забрать результат (плоский список
   и он же сгруппированный по семи типам);
4. `PATCH` / `POST` / `DELETE` `/items` — правки пользователя;
5. `GET /items/{item_id}/source` — фрагмент разговора под конкретным требованием.

Провайдер LLM задаётся переменными окружения и не зашит в код.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    llm = get_llm(settings)
    log.info(
        "Старт %s. LLM-провайдер: %s, модель: %s", settings.app_name, llm.name, llm.model
    )
    if settings.llm_provider == "mock":
        log.warning(
            "Работаем на mock-провайдере: извлечение идёт по правилам, без LLM. "
            "Для полноценного анализа задайте в .env LLM_PROVIDER=yandex_gpt "
            "(плюс YANDEX_FOLDER_ID и YANDEX_API_KEY) либо LLM_PROVIDER=openai_compat"
        )
    elif not settings.llm_ready:
        # Лучше сказать об этом на старте, чем ловить непонятные отказы
        # на первом же анализе во время демонстрации.
        log.error(
            "Провайдер %s выбран, но настроен не полностью — запросы будут падать. "
            "Сверьтесь с .env.example",
            settings.llm_provider,
        )
    yield
    await llm.aclose()


app = FastAPI(
    title="Speech2Spec — анализ разговора",
    description=DESCRIPTION,
    version=VERSION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "анализ", "description": "Запуск анализа и получение результата"},
        {"name": "элементы", "description": "Проверка и правка требований (пункт 4)"},
        {"name": "источник", "description": "Связь с исходным разговором (пункт 5)"},
        {"name": "служебные", "description": "Health-check и справочники"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Контракт фронтенда (API.md) — то, под что уже написан lib/api.ts.
app.include_router(meetings_router)
app.include_router(documents_router)
# Внутренний API: он богаче и остаётся доступен для будущего.
app.include_router(router)


from app.storage.base import DuplicateProjectTitle

@app.exception_handler(DuplicateProjectTitle)
async def duplicate_title_handler(request: Request, exc: DuplicateProjectTitle) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """ValueError из конвейера — это некорректные данные, а не сбой сервера."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/", tags=["служебные"], include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "Speech2Spec — анализ разговора",
        "version": VERSION,
        "docs": "/docs",
        "openapi": "/openapi.json",
    }
