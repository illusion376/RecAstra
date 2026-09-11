import os
os.environ['LLM_PROVIDER'] = 'mock'
os.environ['AUTH_REQUIRED'] = 'false'  # авторизацию проверяет test_auth.py
os.environ['DATABASE_PATH'] = ':memory:'  # не трогать рабочую базу
import asyncio
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.deps import get_storage
from app.config import Settings, get_settings
from app.schemas import Analysis, TranscriptMeta
from app.storage.memory import MemoryStorage
from app.storage.base import DuplicateProjectTitle

class ProjectTitlesTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_normalized_names(self):
        storage = MemoryStorage()
        results = await asyncio.gather(*[
            storage.create_analysis(Analysis(meta=TranscriptMeta(title=title)))
            for title in ['  Новый   проект ', 'НОВЫЙ проект', 'Новый проект']
        ], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, DuplicateProjectTitle) for r in results), 2)
        self.assertEqual(len(await storage.list_analyses()), 1)
        await storage.create_analysis(Analysis(meta=TranscriptMeta(title='Другой проект')))
        self.assertEqual(len(await storage.list_analyses()), 2)

    async def test_both_creation_routes_return_conflict(self):
        storage = MemoryStorage()
        await storage.create_analysis(Analysis(meta=TranscriptMeta(title='Проект')))
        app.dependency_overrides[get_storage] = lambda: storage
        app.dependency_overrides[get_settings] = lambda: Settings(auth_required=False, database_path=":memory:", llm_provider="mock")
        try:
            with TestClient(app) as client:
                response = client.post('/meetings', data={'title': ' ПРОЕКТ '}, files={'file': ('audio.mp3', b'audio', 'audio/mpeg')})
                self.assertEqual(response.status_code, 409, response.text)
                response = client.post('/meetings/from-transcript', json={'title': 'проект', 'stt_response': {'text': 'Заказчик: Нужна форма заявки.'}})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn('названием', response.json()['detail'])
        finally:
            app.dependency_overrides.clear()
