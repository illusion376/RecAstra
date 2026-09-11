"""Exported specifications: immutable snapshots, persisted independently of meetings."""
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.config import Settings, get_settings

router = APIRouter(prefix='/documents', tags=['документы'])

class DocumentCreate(BaseModel):
    id: UUID
    meeting_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=2_000_000)

class Document(DocumentCreate):
    created_at: datetime

@router.get('', response_model=list[Document])
def list_documents(settings: Settings = Depends(get_settings)):
    directory = Path(settings.documents_dir)
    docs = []
    for path in directory.glob('*.json'):
        try:
            docs.append(Document.model_validate_json(path.read_text(encoding='utf-8')))
        except (ValueError, OSError):
            continue
    return sorted(docs, key=lambda doc: doc.created_at, reverse=True)

@router.post('', response_model=Document, status_code=201)
def save_document(body: DocumentCreate, settings: Settings = Depends(get_settings)):
    directory = Path(settings.documents_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{body.id}.json'
    if path.exists():
        previous = Document.model_validate_json(path.read_text(encoding='utf-8'))
        if previous.model_dump(exclude={'created_at'}) != body.model_dump():
            raise HTTPException(409, 'Документ с таким ID уже существует')
        return previous
    document = Document(**body.model_dump(), created_at=datetime.now(timezone.utc))
    temporary = directory / f'.{uuid4()}.tmp'
    try:
        temporary.write_text(document.model_dump_json(), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return document


@router.delete('/{document_id}')
def delete_document(document_id: UUID, settings: Settings = Depends(get_settings)):
    path = Path(settings.documents_dir) / f'{document_id}.json'
    try:
        path.unlink()
    except FileNotFoundError:
        raise HTTPException(404, 'Документ уже удалён или не найден')
    return {'id': str(document_id), 'deleted': True}
