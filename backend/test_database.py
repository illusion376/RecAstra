"""
База данных: проекты и документы принадлежат пользователю и переживают
перезапуск сервера. Офлайн, без сети и ключей (провайдер mock).

    python test_database.py
"""
import os
os.environ['LLM_PROVIDER'] = 'mock'
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app import deps
from app.config import Settings, get_settings
from app.core.auth import hash_password
from app.main import app
from app.storage.sqlite import SCHEMA_VERSION, Database

SAMPLE = json.loads((Path(__file__).parent / 'samples' / 'transcript_sample.json').read_text(encoding='utf-8'))


class DatabaseApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'recastra.db')
        app.dependency_overrides[get_settings] = lambda: Settings(
            database_path=self.db_path, media_dir=str(Path(self.tmp.name) / 'media'),
            auth_required=True, auth_secret='test-secret', llm_provider='mock', stt_provider='none',
        )
        self.client = TestClient(app).__enter__()
        self.anna = self.register('Анна', 'anna@example.com')
        self.boris = self.register('Борис', 'boris@example.com')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.restart()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    # --- помощники ----------------------------------------------------------

    @property
    def db(self) -> Database:
        return deps._database_for(self.db_path)

    def restart(self):
        """Имитация перезапуска сервера: закрыть базу и забыть соединение."""
        self.db.close()
        deps._database_for.cache_clear()

    def register(self, name, email):
        r = self.client.post('/auth/register', json={'name': name, 'email': email, 'password': 'secret1'})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': f"Bearer {r.json()['token']}"}

    def create_project(self, headers, title):
        return self.client.post('/meetings/from-transcript', headers=headers, json={
            'title': title, 'filename': 'meeting.mp3', 'stt_response': {'segments': SAMPLE['segments']},
        })

    def wait_ready(self, headers, mid, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = self.client.get(f'/meetings/{mid}', headers=headers).json()
            if body['status'] in ('ready', 'failed'):
                return body
            time.sleep(0.1)
        self.fail('встреча не дошла до готовности')

    def count(self, table, project_id):
        return self.db.query(f'SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?', (project_id,))[0]['n']

    # --- проекты ------------------------------------------------------------

    def test_integrated_project_actions_respect_owner_and_persist(self):
        response = self.create_project(self.anna, 'Проверка обновления')
        self.assertEqual(response.status_code, 202)
        mid = response.json()['id']
        meeting = self.wait_ready(self.anna, mid)
        sid = meeting['transcript'][0]['id']
        path = f'/meetings/{mid}'
        correction = path + f'/transcript/{sid}/speaker'
        self.assertEqual(self.client.patch(correction, headers=self.boris, json={'speaker': 'Менеджер'}).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=self.boris).status_code, 404)
        self.assertEqual(self.client.delete(path).status_code, 401)
        self.assertEqual(self.client.patch(correction, headers=self.anna, json={'speaker': 'Менеджер'}).status_code, 200)
        self.restart()
        self.assertEqual(self.client.get(path, headers=self.anna).json()['transcript'][0]['speaker'], 'Менеджер')
        self.assertEqual(self.client.delete(path, headers=self.anna).status_code, 200)
        self.restart()
        self.assertEqual(self.client.get(path, headers=self.anna).status_code, 404)
        self.assertEqual(self.count('segments', mid), 0)

    def test_projects_are_private_and_survive_restart(self):
        r = self.create_project(self.anna, 'Сервис онлайн-записи')
        self.assertEqual(r.status_code, 202, r.text)
        mid = r.json()['id']
        meeting = self.wait_ready(self.anna, mid)
        self.assertEqual(meeting['status'], 'ready', meeting.get('error'))
        self.assertTrue(meeting['analysis'], 'mock должен найти требования')
        self.assertEqual(self.count('segments', mid), len(SAMPLE['segments']))

        # Свои проекты видны, чужие — нет, и ID чужого проекта ничего не даёт.
        self.assertEqual([m['id'] for m in self.client.get('/meetings', headers=self.anna).json()], [mid])
        self.assertEqual(self.client.get('/meetings', headers=self.boris).json(), [])
        for method, path, body in [
            ('get', f'/meetings/{mid}', None),
            ('put', f'/meetings/{mid}/analysis', {'analysis': []}),
            ('get', f'/api/v1/analysis/{mid}', None),
            ('get', f'/api/v1/analysis/{mid}/items', None),
            ('get', f'/api/v1/analysis/{mid}/transcript', None),
            ('delete', f'/api/v1/analysis/{mid}', None),
        ]:
            r = self.client.request(method, path, headers=self.boris, json=body)
            self.assertEqual(r.status_code, 404, f'{method.upper()} {path}: {r.status_code}')
        self.assertEqual(len(self.client.get(f'/meetings/{mid}', headers=self.anna).json()['analysis']), len(meeting['analysis']))

        # Правка владельца сохраняется в базе.
        edited = meeting['analysis'][:2]
        original_confidence = edited[1]['confidence']
        edited[1]['confidence'] = 1.0  # client cannot override model confidence
        edited[0]['description'] = 'Правка после встречи.'
        r = self.client.put(f'/meetings/{mid}/analysis', headers=self.anna, json={'analysis': edited})
        self.assertEqual(r.status_code, 200, r.text)

        self.restart()
        with TestClient(app) as client:
            after = client.get(f'/meetings/{mid}', headers=self.anna).json()
            self.assertEqual(after['status'], 'ready')
            self.assertEqual(after['title'], 'Сервис онлайн-записи')
            self.assertEqual(len(after['transcript']), len(meeting['transcript']))
            self.assertEqual([c['id'] for c in after['analysis']], [c['id'] for c in edited])
            self.assertEqual(after['analysis'][0]['description'], 'Правка после встречи.')
            self.assertIsNone(after['analysis'][0].get('confidence'))
            self.assertEqual(after['analysis'][1]['confidence'], original_confidence)
            self.assertEqual(client.get('/meetings', headers=self.boris).json(), [])

    def test_titles_are_unique_per_user(self):
        self.assertEqual(self.create_project(self.anna, 'Проект').status_code, 202)
        self.assertEqual(self.create_project(self.boris, 'проект').status_code, 202, 'у другого пользователя можно')
        r = self.create_project(self.anna, '  ПРОЕКТ ')
        self.assertEqual(r.status_code, 409)
        self.assertIn('названием', r.json()['detail'])

    def test_delete_cascades_to_transcript_and_items(self):
        mid = self.create_project(self.anna, 'Удаляемый').json()['id']
        self.wait_ready(self.anna, mid)
        self.assertGreater(self.count('items', mid), 0)
        self.assertEqual(self.client.delete(f'/api/v1/analysis/{mid}', headers=self.anna).status_code, 204)
        self.assertEqual(self.count('segments', mid), 0)
        self.assertEqual(self.count('items', mid), 0)
        self.assertEqual(self.client.get(f'/meetings/{mid}', headers=self.anna).status_code, 404)

    def test_audio_is_served_only_to_owner(self):
        r = self.client.post('/meetings', headers=self.anna, data={'title': 'С записью'},
                             files={'file': ('meeting.mp3', b'ID3' + b'\0' * 500, 'audio/mpeg')})
        self.assertEqual(r.status_code, 202, r.text)
        url = r.json()['audio_url']
        self.assertEqual(self.client.get(url, headers=self.anna).status_code, 200)
        self.assertEqual(self.client.get(url, headers=self.boris).status_code, 404)
        token = self.boris['Authorization'].split()[1]
        self.assertEqual(self.client.get(f'{url}?access_token={token}').status_code, 404)

    # --- документы ----------------------------------------------------------

    def test_documents_are_private(self):
        body = {'id': str(uuid4()), 'meeting_id': 'm1', 'title': 'ТЗ', 'content': '# Текст'}
        self.assertEqual(self.client.post('/documents', headers=self.anna, json=body).status_code, 201)
        self.assertEqual(self.client.get('/documents', headers=self.boris).json(), [])
        self.assertEqual(self.client.delete(f"/documents/{body['id']}", headers=self.boris).status_code, 404)
        self.assertEqual(self.client.post('/documents', headers=self.boris, json=body).status_code, 409)
        rows = self.client.get('/documents', headers=self.anna).json()
        self.assertEqual([d['id'] for d in rows], [body['id']])

    def test_deleting_user_removes_their_data(self):
        mid = self.create_project(self.anna, 'Проект Анны').json()['id']
        self.wait_ready(self.anna, mid)
        self.client.post('/documents', headers=self.anna, json={'id': str(uuid4()), 'meeting_id': mid, 'title': 'ТЗ', 'content': '#'})
        with self.db.transaction() as c:
            c.execute("DELETE FROM users WHERE email = 'anna@example.com'")
        for table in ['projects', 'documents']:
            self.assertEqual(self.db.query(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'], 0, table)
        self.assertEqual(self.count('segments', mid), 0)


class DatabaseFileTests(unittest.TestCase):
    def test_legacy_users_json_is_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / 'users.json'
            legacy.write_text(json.dumps({'users': [{
                'id': 'u-old', 'email': 'old@example.com', 'name': 'Старый', 'created_at': datetime.now(timezone.utc).isoformat(),
                'password_hash': hash_password('secret1'),
            }]}), encoding='utf-8')
            db_path = str(Path(directory) / 'recastra.db')
            app.dependency_overrides[get_settings] = lambda: Settings(database_path=db_path, auth_secret='s', auth_required=True, llm_provider='mock')
            try:
                with TestClient(app) as client:
                    r = client.post('/auth/login', json={'email': 'old@example.com', 'password': 'secret1'})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()['user']['id'], 'u-old')
                self.assertFalse(legacy.exists())
                self.assertTrue((Path(directory) / 'users.json.imported').exists())
            finally:
                deps._database_for(db_path).close()
                deps._database_for.cache_clear()
                app.dependency_overrides.clear()

    def test_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'future.db')
            conn = sqlite3.connect(path)
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION + 1}')
            conn.close()
            with self.assertRaises(RuntimeError):
                Database(path)

    def test_reopen_keeps_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'db.sqlite')
            db = Database(path)
            user = db.create_user('a@b.cd', 'А', hash_password('secret1'))
            db.close()
            db = Database(path)
            self.assertEqual(db.get_user_by_email('A@B.cd').id, user.id)
            self.assertEqual(db.query('PRAGMA user_version')[0][0], SCHEMA_VERSION)
            db.close()


if __name__ == '__main__':
    unittest.main()
