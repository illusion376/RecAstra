"""Offline exported document persistence regression."""
import os
os.environ['LLM_PROVIDER'] = 'mock'
import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from app.config import Settings, get_settings

class DocumentsTests(unittest.TestCase):
    def test_snapshot_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            app.dependency_overrides[get_settings] = lambda: Settings(documents_dir=directory, llm_provider='mock')
            try:
                body = {'id': str(uuid4()), 'meeting_id': 'demo', 'title': 'ТЗ', 'content': '# Согласованный текст'}
                with TestClient(app) as client:
                    self.assertEqual(client.get('/documents').json(), [])
                    response = client.post('/documents', json=body)
                    self.assertEqual(response.status_code, 201)
                    self.assertEqual(client.post('/documents', json=body).json(), response.json())
                    self.assertEqual(client.post('/documents', json={**body, 'content':'другой текст'}).status_code, 409)
                with TestClient(app) as client:
                    rows = client.get('/documents').json()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['content'], body['content'])
                    self.assertEqual(client.post('/documents', json={**body,'id':'../oops'}).status_code, 422)
                    self.assertEqual(client.delete('/documents/not-a-uuid').status_code, 422)
                    self.assertEqual(client.delete(f"/documents/{body['id']}").status_code, 200)
                    self.assertEqual(client.get('/documents').json(), [])
                    self.assertEqual(client.delete(f"/documents/{body['id']}").status_code, 404)
                with TestClient(app) as client:
                    self.assertEqual(client.get('/documents').json(), [])
            finally:
                app.dependency_overrides.clear()

if __name__ == '__main__':
    unittest.main()
