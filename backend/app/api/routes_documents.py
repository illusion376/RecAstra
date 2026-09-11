"""Exported specifications: immutable snapshots, persisted independently of meetings.

Документы хранятся в базе (таблица documents) и принадлежат пользователю:
каждый видит и удаляет только свои.
"""
import sqlite3
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps import get_current_user, get_database
from app.storage.sqlite import Database
from app.storage.users import User

router = APIRouter(prefix='/documents', tags=['документы'])

class DocumentCreate(BaseModel):
    id: UUID
    meeting_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=2_000_000)

class Document(DocumentCreate):
    created_at: datetime

@router.get('', response_model=list[Document])
def list_documents(db: Database = Depends(get_database), user: User = Depends(get_current_user)):
    return [Document.model_validate(row) for row in db.list_documents(user.id)]

@router.post('', response_model=Document, status_code=201)
def save_document(body: DocumentCreate, db: Database = Depends(get_database), user: User = Depends(get_current_user)):
    previous = db.get_document(str(body.id))
    if previous is not None:
        # Повтор того же экспорта (например, после обрыва связи) — не ошибка.
        same = previous['owner_id'] == user.id and Document.model_validate(previous).model_dump(exclude={'created_at'}) == body.model_dump()
        if not same:
            raise HTTPException(409, 'Документ с таким ID уже существует')
        return Document.model_validate(previous)
    document = Document(**body.model_dump(), created_at=datetime.now(timezone.utc))
    try:
        db.add_document(user.id, str(document.id), document.meeting_id, document.title, document.content, document.created_at)
    except sqlite3.IntegrityError:  # тот же ID успели сохранить параллельным запросом
        raise HTTPException(409, 'Документ с таким ID уже существует')
    return document


@router.delete('/{document_id}')
def delete_document(document_id: UUID, db: Database = Depends(get_database), user: User = Depends(get_current_user)):
    if not db.delete_document(user.id, str(document_id)):
        raise HTTPException(404, 'Документ уже удалён или не найден')
    return {'id': str(document_id), 'deleted': True}
