"""
База данных на SQLite: пользователи, их проекты (встречи) и экспортированные ТЗ.

Почему SQLite: встроен в Python, база — один файл `data/recastra.db`, ставить
и поднимать ничего не нужно. Для прототипа с десятками пользователей этого
хватает с запасом, а переход на PostgreSQL упирается только в этот модуль —
роуты работают с интерфейсом Storage.

Схема
-----
    users      — учётные записи;
    projects   — встречи (в интерфейсе — «проекты»), у каждой есть владелец;
    segments   — реплики расшифровки, по строке на реплику;
    items      — требования и прочие элементы анализа;
    documents  — экспортированные ТЗ, тоже с владельцем.

Удаление пользователя каскадом удаляет его проекты и документы, удаление
проекта — его реплики и требования.

Вложенные структуры анализа (прогресс, статистика, карта разговора, поля
требования) лежат JSON-колонкой `data`: они целиком читаются и пишутся
конвейером, а раскладывать их по таблицам значит тащить схему за каждой
правкой модели. Всё, по чему ищут и фильтруют, — отдельные колонки.

Изоляция данных: SqliteStorage создаётся на конкретного владельца, и каждый
запрос содержит `owner_id = ?`. Чужой проект для него просто не существует —
ручки отвечают 404, не выдавая, что такой ID есть.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from app.schemas import Analysis, Item, ItemType, TranscriptSegment
from app.storage.base import (
    DUPLICATE_TITLE_MESSAGE,
    DuplicateProjectTitle,
    Storage,
    filter_and_sort_items,
    normalize_title,
    title_key,
)
from app.storage.users import EmailTaken, StoredUser, normalize_email

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT,
    title_key  TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
-- Названия уникальны в пределах владельца: у двух людей может быть по «Проекту».
CREATE UNIQUE INDEX IF NOT EXISTS projects_owner_title ON projects(owner_id, title_key);
CREATE INDEX IF NOT EXISTS projects_owner_created ON projects(owner_id, created_at);

CREATE TABLE IF NOT EXISTS segments (
    project_id       TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    segment_id       INTEGER NOT NULL,
    start_sec        REAL    NOT NULL,
    end_sec          REAL    NOT NULL,
    speaker          TEXT,
    text             TEXT    NOT NULL,
    timing_estimated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, position)
);

CREATE TABLE IF NOT EXISTS items (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id         TEXT NOT NULL,
    type       TEXT NOT NULL,
    status     TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS documents (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_owner_created ON documents(owner_id, created_at);
"""


def _ts(value: datetime) -> str:
    """Единый формат времени: строки сортируются так же, как моменты."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Database:
    """
    Одно соединение на процесс и блокировка вокруг него.

    Ручки бывают и асинхронными, и обычными (те FastAPI гоняет в пуле
    потоков), поэтому соединение открыто с check_same_thread=False, а все
    обращения идут под одной блокировкой. Запросы короткие — миллисекунды,
    так что цикл событий это не тормозит.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None — транзакции открываем сами, явно.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                # WAL: чтение не ждёт записи — фронт опрашивает встречу,
                # пока конвейер пишет прогресс.
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._migrate()

    # --- служебное ---------------------------------------------------------

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"База {self.path} создана более новой версией приложения "
                f"(схема {version}, поддерживается {SCHEMA_VERSION})"
            )
        if version < SCHEMA_VERSION:
            # Следующие версии схемы добавляются сюда: if version < 2: ALTER TABLE …
            self._conn.executescript(
                f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
            log.info("База %s: схема версии %s", self.path, SCHEMA_VERSION)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- пользователи ------------------------------------------------------

    @staticmethod
    def _user(row: Optional[sqlite3.Row]) -> Optional[StoredUser]:
        return StoredUser(**dict(row)) if row else None

    def get_user_by_email(self, email: str) -> Optional[StoredUser]:
        rows = self.query("SELECT * FROM users WHERE email = ?", (normalize_email(email),))
        return self._user(rows[0] if rows else None)

    def get_user_by_id(self, user_id: str) -> Optional[StoredUser]:
        rows = self.query("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._user(rows[0] if rows else None)

    def create_user(self, email: str, name: str, password_hash: str) -> StoredUser:
        user = StoredUser(
            id=str(uuid4()),
            email=normalize_email(email),
            name=name.strip(),
            created_at=datetime.now(timezone.utc),
            password_hash=password_hash,
        )
        try:
            self._insert_user(user)
        except sqlite3.IntegrityError as exc:
            raise EmailTaken("Пользователь с такой почтой уже зарегистрирован") from exc
        return user

    def ensure_user(self, user: StoredUser) -> None:
        """Завести служебного пользователя, если его ещё нет (гость при AUTH_REQUIRED=false)."""
        if not self.query("SELECT 1 FROM users WHERE id = ?", (user.id,)):
            self._insert_user(user, ignore=True)

    def _insert_user(self, user: StoredUser, ignore: bool = False) -> None:
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        with self.transaction() as c:
            c.execute(
                f"{verb} INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user.id, user.email, user.name, user.password_hash, _ts(user.created_at)),
            )

    def import_users_json(self, path: str | Path) -> int:
        """
        Перенести пользователей из прежнего data/users.json (первая версия
        авторизации хранила их файлом). Файл после переноса переименовывается
        в users.json.imported, чтобы не импортироваться повторно.
        """
        file = Path(path)
        if not file.is_file():
            return 0
        rows = json.loads(file.read_text(encoding="utf-8")).get("users", [])
        users = [StoredUser.model_validate(row) for row in rows]
        for user in users:
            self._insert_user(user, ignore=True)
        file.rename(file.with_name(file.name + ".imported"))
        log.info("Из %s перенесено пользователей: %s", file, len(users))
        return len(users)

    # --- документы ---------------------------------------------------------

    def list_documents(self, owner_id: str) -> list[dict]:
        rows = self.query(
            "SELECT id, meeting_id, title, content, created_at FROM documents "
            "WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        )
        return [dict(r) for r in rows]

    def get_document(self, document_id: str) -> Optional[dict]:
        """Документ по ID среди ВСЕХ владельцев — только для проверки занятости ID."""
        rows = self.query("SELECT * FROM documents WHERE id = ?", (document_id,))
        return dict(rows[0]) if rows else None

    def add_document(
        self, owner_id: str, document_id: str, meeting_id: str, title: str,
        content: str, created_at: datetime,
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO documents (id, owner_id, meeting_id, title, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, owner_id, meeting_id, title, content, _ts(created_at)),
            )

    def delete_document(self, owner_id: str, document_id: str) -> bool:
        with self.transaction() as c:
            cur = c.execute(
                "DELETE FROM documents WHERE id = ? AND owner_id = ?", (document_id, owner_id)
            )
            return cur.rowcount > 0


class SqliteStorage(Storage):
    """Проекты одного пользователя. Чужие проекты этому объекту не видны."""

    def __init__(self, db: Database, owner_id: str) -> None:
        self.db = db
        self.owner_id = owner_id

    def _owns(self, c: sqlite3.Connection, analysis_id: str) -> bool:
        row = c.execute(
            "SELECT 1 FROM projects WHERE id = ? AND owner_id = ?", (analysis_id, self.owner_id)
        ).fetchone()
        return row is not None

    # --- Анализы (проекты) -----------------------------------------------

    async def create_analysis(self, analysis: Analysis) -> Analysis:
        title = normalize_title(analysis.meta.title)
        analysis.meta.title = title or None
        try:
            with self.db.transaction() as c:
                c.execute(
                    "INSERT INTO projects (id, owner_id, title, title_key, status, created_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        analysis.id, self.owner_id, title or None, title_key(title) or None,
                        analysis.status.value, _ts(analysis.created_at), analysis.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "title_key" in str(exc):
                raise DuplicateProjectTitle(DUPLICATE_TITLE_MESSAGE) from exc
            raise
        return analysis

    async def get_analysis(self, analysis_id: str) -> Optional[Analysis]:
        rows = self.db.query(
            "SELECT data FROM projects WHERE id = ? AND owner_id = ?", (analysis_id, self.owner_id)
        )
        return Analysis.model_validate_json(rows[0]["data"]) if rows else None

    async def save_analysis(self, analysis: Analysis) -> Analysis:
        title = normalize_title(analysis.meta.title)
        try:
            with self.db.transaction() as c:
                # Проект могли удалить, пока шла обработка, — тогда не воскрешаем.
                c.execute(
                    "UPDATE projects SET title = ?, title_key = ?, status = ?, data = ? "
                    "WHERE id = ? AND owner_id = ?",
                    (
                        title or None, title_key(title) or None, analysis.status.value,
                        analysis.model_dump_json(), analysis.id, self.owner_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "title_key" in str(exc):
                raise DuplicateProjectTitle(DUPLICATE_TITLE_MESSAGE) from exc
            raise
        return analysis

    async def list_analyses(self, limit: int = 50) -> list[Analysis]:
        rows = self.db.query(
            "SELECT data FROM projects WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.owner_id, limit),
        )
        return [Analysis.model_validate_json(r["data"]) for r in rows]

    async def delete_analysis(self, analysis_id: str) -> bool:
        with self.db.transaction() as c:
            cur = c.execute(
                "DELETE FROM projects WHERE id = ? AND owner_id = ?", (analysis_id, self.owner_id)
            )
            return cur.rowcount > 0

    # --- Транскрипция -----------------------------------------------------

    async def save_segments(self, analysis_id: str, segments: list[TranscriptSegment]) -> None:
        with self.db.transaction() as c:
            if not self._owns(c, analysis_id):
                return
            c.execute("DELETE FROM segments WHERE project_id = ?", (analysis_id,))
            c.executemany(
                "INSERT INTO segments (project_id, position, segment_id, start_sec, end_sec, "
                "speaker, text, timing_estimated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (analysis_id, n, s.id, s.start, s.end, s.speaker, s.text, int(s.timing_estimated))
                    for n, s in enumerate(segments)
                ],
            )

    async def get_segments(self, analysis_id: str) -> list[TranscriptSegment]:
        rows = self.db.query(
            "SELECT s.* FROM segments s JOIN projects p ON p.id = s.project_id "
            "WHERE s.project_id = ? AND p.owner_id = ? ORDER BY s.position",
            (analysis_id, self.owner_id),
        )
        return [
            TranscriptSegment(
                id=r["segment_id"], start=r["start_sec"], end=r["end_sec"], text=r["text"],
                speaker=r["speaker"], timing_estimated=bool(r["timing_estimated"]),
            )
            for r in rows
        ]

    # --- Элементы ---------------------------------------------------------

    @staticmethod
    def _item_row(item: Item) -> tuple:
        return (
            item.analysis_id, item.id, item.type.value, item.status.value, item.title,
            _ts(item.created_at), item.model_dump_json(),
        )

    _UPSERT_ITEM = (
        "INSERT OR REPLACE INTO items (project_id, id, type, status, title, created_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    async def replace_items(self, analysis_id: str, items: list[Item]) -> None:
        with self.db.transaction() as c:
            if not self._owns(c, analysis_id):
                return
            c.execute("DELETE FROM items WHERE project_id = ?", (analysis_id,))
            c.executemany(self._UPSERT_ITEM, [self._item_row(i) for i in items])

    async def add_item(self, item: Item) -> Item:
        return await self.save_item(item)

    async def get_item(self, analysis_id: str, item_id: str) -> Optional[Item]:
        rows = self.db.query(
            "SELECT i.data FROM items i JOIN projects p ON p.id = i.project_id "
            "WHERE i.project_id = ? AND i.id = ? AND p.owner_id = ?",
            (analysis_id, item_id, self.owner_id),
        )
        return Item.model_validate_json(rows[0]["data"]) if rows else None

    async def save_item(self, item: Item) -> Item:
        with self.db.transaction() as c:
            if self._owns(c, item.analysis_id):
                c.execute(self._UPSERT_ITEM, self._item_row(item))
        return item

    async def delete_item(self, analysis_id: str, item_id: str) -> bool:
        with self.db.transaction() as c:
            if not self._owns(c, analysis_id):
                return False
            cur = c.execute(
                "DELETE FROM items WHERE project_id = ? AND id = ?", (analysis_id, item_id)
            )
            return cur.rowcount > 0

    async def list_items(
        self,
        analysis_id: str,
        types: Optional[list[ItemType]] = None,
        statuses: Optional[list[str]] = None,
        role: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[Item]:
        rows = self.db.query(
            "SELECT i.data FROM items i JOIN projects p ON p.id = i.project_id "
            "WHERE i.project_id = ? AND p.owner_id = ?",
            (analysis_id, self.owner_id),
        )
        items = [Item.model_validate_json(r["data"]) for r in rows]
        return filter_and_sort_items(items, types, statuses, role, query)
