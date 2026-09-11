# Бэкенд RecAstra: FastAPI + uvicorn.
#
#   docker build -t recastra-backend ./backend
#   docker run -p 8000:8000 -v recastra-data:/app/data recastra-backend
#
# Обычно запускается через docker-compose.yml в корне репозитория.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости, потом код: при правке кода слой с pip берётся из кэша.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# База, секрет токенов и загруженные записи — в томах, иначе пропадут
# вместе с контейнером. Каталоги создаём заранее, чтобы непривилегированный
# пользователь мог в них писать.
RUN useradd --create-home --uid 1000 recastra \
    && mkdir -p /app/data /app/media \
    && chown -R recastra:recastra /app/data /app/media
USER recastra

ENV DATABASE_PATH=/app/data/recastra.db \
    MEDIA_DIR=/app/media
VOLUME ["/app/data", "/app/media"]

EXPOSE 8000

# «/» открыт без входа и не трогает ни базу, ни модель.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

# Один процесс: фоновая обработка встреч идёт задачами внутри него,
# а SQLite не любит много пишущих процессов.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
