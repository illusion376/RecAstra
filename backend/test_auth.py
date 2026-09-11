"""
Регистрация, вход и защита ручек. Офлайн, без сети и ключей.

    python test_auth.py
"""
import os
os.environ['LLM_PROVIDER'] = 'mock'
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import deps
from app.config import Settings, get_settings
from app.core.auth import create_token, hash_password, read_token, verify_password
from app.main import app

SECRET = 'test-secret'


class PasswordAndTokenTests(unittest.TestCase):
    def test_password_hash(self):
        stored = hash_password('секрет123')
        self.assertNotIn('секрет123', stored)
        self.assertTrue(verify_password('секрет123', stored))
        self.assertFalse(verify_password('секрет124', stored))
        self.assertNotEqual(stored, hash_password('секрет123'), 'соль должна отличаться')
        self.assertFalse(verify_password('x', 'мусор'))

    def test_token(self):
        token = create_token('u1', SECRET, 60)
        self.assertEqual(read_token(token, SECRET), 'u1')
        self.assertIsNone(read_token(token, 'другой-секрет'))
        self.assertIsNone(read_token(create_token('u1', SECRET, -1), SECRET), 'истёкший')
        payload, signature = token.split('.')
        self.assertIsNone(read_token(payload[:-2] + 'xx.' + signature, SECRET), 'подделка')
        for junk in ['', 'abc', 'a.b.c', '.']:
            self.assertIsNone(read_token(junk, SECRET))


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'db.sqlite')
        self.required = True
        app.dependency_overrides[get_settings] = lambda: Settings(
            database_path=self.db_path, auth_required=self.required,
            auth_secret=SECRET, llm_provider='mock',
        )
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.restart()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    @property
    def db(self):
        return deps._database_for(self.db_path)

    def restart(self):
        """Имитация перезапуска сервера: закрыть базу и забыть соединение."""
        self.db.close()
        deps._database_for.cache_clear()

    def register(self, email='anna@example.com', password='secret1', name='Анна'):
        return self.client.post('/auth/register', json={'email': email, 'password': password, 'name': name})

    def test_protected_without_token(self):
        for path in ['/meetings', '/documents', '/api/v1/analysis', '/auth/me']:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401, path)
            self.assertEqual(r.headers.get('www-authenticate'), 'Bearer')
            self.assertIsInstance(r.json()['detail'], str)
        r = self.client.get('/meetings', headers={'Authorization': 'Bearer fake.token'})
        self.assertEqual(r.status_code, 401)

    def test_register_login_and_access(self):
        r = self.register(email='  Anna@Example.com ')
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body['user']['email'], 'anna@example.com')
        self.assertEqual(body['user']['name'], 'Анна')
        self.assertNotIn('password_hash', body['user'])
        headers = {'Authorization': f"Bearer {body['token']}"}

        self.assertEqual(self.client.get('/auth/me', headers=headers).json()['id'], body['user']['id'])
        for path in ['/meetings', '/documents', '/api/v1/analysis']:
            self.assertEqual(self.client.get(path, headers=headers).status_code, 200, path)
        # <audio src> не умеет заголовки — токен можно передать параметром.
        self.assertEqual(self.client.get(f"/meetings?access_token={body['token']}").status_code, 200)

        self.assertEqual(self.register(email='ANNA@example.com').status_code, 409)

        ok = self.client.post('/auth/login', json={'email': 'anna@EXAMPLE.com', 'password': 'secret1'})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()['user']['id'], body['user']['id'])
        for creds in [{'email': 'anna@example.com', 'password': 'wrong!!'},
                      {'email': 'nobody@example.com', 'password': 'secret1'}]:
            bad = self.client.post('/auth/login', json=creds)
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(bad.json()['detail'], 'Неверная почта или пароль')

        rows = self.db.query('SELECT email, password_hash FROM users')
        self.assertEqual([r['email'] for r in rows], ['anna@example.com'])
        self.assertNotIn('secret1', rows[0]['password_hash'], 'пароль не должен лежать открытым текстом')

    def test_validation(self):
        self.assertEqual(self.register(password='12345').status_code, 422)
        self.assertEqual(self.register(email='не-почта').status_code, 422)
        self.assertEqual(self.register(name='   ').status_code, 422)
        self.assertEqual(self.db.query('SELECT COUNT(*) AS n FROM users')[0]['n'], 0)

    def test_users_survive_restart(self):
        self.assertEqual(self.register().status_code, 201)
        self.restart()
        with TestClient(app) as client:
            r = client.post('/auth/login', json={'email': 'anna@example.com', 'password': 'secret1'})
            self.assertEqual(r.status_code, 200)

    def test_token_of_deleted_user(self):
        token = self.register().json()['token']
        with self.db.transaction() as c:
            c.execute('DELETE FROM users')
        r = self.client.get('/meetings', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 401)

    def test_auth_can_be_disabled(self):
        self.required = False
        self.assertEqual(self.client.get('/meetings').status_code, 200)
        self.assertEqual(self.client.get('/auth/me').json()['id'], 'guest')


class GeneratedSecretTests(unittest.TestCase):
    def test_secret_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / 'sub' / 'db.sqlite')
            app.dependency_overrides[get_settings] = lambda: Settings(database_path=db_path, auth_secret='', auth_required=True, llm_provider='mock')
            try:
                with TestClient(app) as client:
                    token = client.post('/auth/register', json={'email': 'a@b.cd', 'password': 'secret1', 'name': 'А'}).json()['token']
                    self.assertEqual(client.get('/auth/me', headers={'Authorization': f'Bearer {token}'}).status_code, 200)
                self.assertTrue((Path(directory) / 'sub' / '.auth_secret').read_text().strip())
            finally:
                deps._database_for(db_path).close()
                deps._database_for.cache_clear()
                app.dependency_overrides.clear()


if __name__ == '__main__':
    unittest.main()
