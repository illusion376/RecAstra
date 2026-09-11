import os
os.environ['LLM_PROVIDER'] = 'mock'
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.config import Settings, get_settings
from app.deps import get_storage
from app.schemas import Analysis, TranscriptMeta, TranscriptSegment, JobStatus
from app.storage.memory import MemoryStorage

class ProjectActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_correction_and_delete_no_resurrection(self):
        storage = MemoryStorage()
        project = Analysis(meta=TranscriptMeta(title='Проект'), status=JobStatus.DONE)
        await storage.create_analysis(project)
        segments = [TranscriptSegment(id=0, start=0, end=2, text='Нужна форма.', speaker='Менеджер')]
        await storage.save_segments(project.id, segments)
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / f'{project.id}.mp3'
            audio.write_bytes(b'audio')
            app.dependency_overrides[get_storage] = lambda: storage
            app.dependency_overrides[get_settings] = lambda: Settings(media_dir=folder, database_path=str(Path(folder) / 'test.sqlite'), auth_required=False, llm_provider='mock')
            try:
                with TestClient(app) as client:
                    url = f'/meetings/{project.id}'
                    r = client.patch(url+'/transcript/s0/speaker', json={'speaker':'Заказчик'})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(client.get(url).json()['transcript'][0]['speaker'], 'Заказчик')
                    self.assertEqual(client.patch(url+'/transcript/s99/speaker', json={'speaker':'Менеджер'}).status_code, 404)
                    self.assertEqual(client.delete(url).status_code, 200)
                    self.assertFalse(audio.exists())
                    self.assertEqual(client.get(url).status_code, 404)
                    self.assertEqual(client.delete(url).status_code, 404)
                await storage.save_analysis(project)
                await storage.save_segments(project.id, segments)
                await storage.replace_items(project.id, [])
                self.assertIsNone(await storage.get_analysis(project.id))
                self.assertEqual(await storage.get_segments(project.id), [])
            finally:
                app.dependency_overrides.clear()
