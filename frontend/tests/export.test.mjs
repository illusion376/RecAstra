import { test } from 'node:test';
import assert from 'node:assert/strict';
import { newId, specFileName } from '../lib/export.ts';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test('id is a UUID v4 even without crypto.randomUUID (page opened over http://IP)', () => {
  // По http://IP браузер не даёт randomUUID — остаётся только getRandomValues.
  const insecure = { getRandomValues: array => globalThis.crypto.getRandomValues(array) };
  const ids = new Set(Array.from({ length: 200 }, () => newId(insecure)));
  assert.equal(ids.size, 200);
  for (const id of ids) assert.match(id, UUID);
  assert.match(newId(), UUID);
});

test('file name keeps Cyrillic and drops unsafe characters', () => {
  assert.equal(specFileName('Сервис онлайн-записи'), 'ТЗ_Сервис_онлайн-записи.md');
  assert.equal(specFileName('../../etc/passwd: "договор"?'), 'ТЗ_etc_passwd_договор.md');
  assert.equal(specFileName('   '), 'ТЗ_проект.md');
  assert.ok(specFileName('Я'.repeat(500)).length <= 'ТЗ_.md'.length + 100);
});
