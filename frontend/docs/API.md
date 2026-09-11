# API RecAstra

Базовый адрес — `NEXT_PUBLIC_API_URL`. Ответы JSON без обёртки `data`.
Полная схема и ограничения полей: `/docs` и `/openapi.json` запущенного бэкенда.

## Авторизация

| Метод | Путь | Тело | Ответ |
| --- | --- | --- | --- |
| POST | `/auth/register` | `{name, email, password}` | 201, `{token, token_type, user}` |
| POST | `/auth/login` | `{email, password}` | 200, `{token, token_type, user}` |
| GET | `/auth/me` | — | 200, пользователь |

Пароль — минимум 6 символов. Все запросы к проектам и документам требуют
`Authorization: Bearer <token>`. Чужие проекты не видны; истёкший токен возвращает 401.
Для аудиоплеера API-клиент добавляет токен в параметр `access_token` URL записи.

## Проекты и расшифровка

| Метод | Путь | Тело | Ответ |
| --- | --- | --- | --- |
| GET | `/meetings` | — | 200, массив Meeting |
| POST | `/meetings` | multipart: `title`, `file` | 202, Meeting |
| POST | `/meetings/from-transcript` | `{title, filename, stt_response}` | 202, Meeting |
| GET | `/meetings/{id}` | — | 200, Meeting |
| DELETE | `/meetings/{id}` | — | 200, `{id, deleted: true}` |
| PUT | `/meetings/{id}/analysis` | `{analysis: [...]}` | 200, Meeting |
| PATCH | `/meetings/{id}/transcript/{segment_id}/speaker` | `{speaker: "Менеджер"}` | 200, Meeting |

`POST /meetings` принимает MP3, WAV, M4A, OGG, WebM, FLAC, MP4; лимит по умолчанию 200 МБ.
При FormData не задавайте Content-Type вручную. Название проекта уникально внутри аккаунта:
регистр, лишние пробелы и Unicode NFKC нормализуются; дубликат возвращает 409.

```bash
curl -X POST "http://localhost:8000/meetings" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "title=Сервис заявок" \
  -F "file=@meeting.mp3"
```

Meeting содержит `id`, `title`, `date`, `filename`, `duration` (секунды), `status`,
`transcript`, `analysis`, необязательные `audio_url` и `error`.
Статусы: `processing`, `ready`, `failed`. Фронтенд опрашивает выбранный обрабатываемый проект каждые 3 секунды.

Реплика: `{id, speaker, start, text, timing_estimated?}`. ID, например `s0`, передаётся без изменения.
`timing_estimated=true` означает приблизительное время, полученное разбиением текста.
Роль можно исправить на `Заказчик`, `Менеджер` или `Участник`; во время обработки возвращается 409.
Удаление проекта удаляет запись и анализ, но сохраняет экспортированные документы.

## Карточки анализа

`PUT` заменяет весь массив; `{analysis: []}` очищает его.
Пример тела: [save-analysis.json](api-examples/save-analysis.json).
Пример готового проекта: [meeting-ready.json](api-examples/meeting-ready.json).

| Поле | Содержание |
| --- | --- |
| `id` | Уникальная строка |
| `kind` | `functional`, `scenarios`, `constraints`, `conditions`, `questions`, `agreements`; API также поддерживает `roles`, скрытый в интерфейсе |
| `title`, `description` | Формулировка и подробности |
| `source` | Секунды исходной реплики или `null` |
| `role` | Необязательная роль |
| `needs_clarification` | Требует уточнения |
| `resolved` | Открытый вопрос решён |
| `priority` | `must`, `should`, `could`, `wont` или `null` |
| `confidence` | Оценка анализатора от 0 до 1; может отсутствовать |

Карточки и экспорт сортируются по приоритету внутри раздела; неизвестный приоритет идёт последним.
Уверенность показывается в процентах и не является гарантией правильности.
Для ручных и изменённых формулировок оценка отсутствует. Клиент не может подменить оценку модели через сохранение карточек.
Поля `quote`, `verified`, `user_story` и `source_end` могут приходить дополнительно.

## Документы

| Метод | Путь | Тело | Ответ |
| --- | --- | --- | --- |
| GET | `/documents` | — | 200, массив документов |
| POST | `/documents` | `{id, meeting_id, title, content}` | 201, документ |
| DELETE | `/documents/{id}` | — | 200, `{id, deleted: true}` |

`id` документа — UUID, `content` — Markdown. Ответ дополнительно содержит `created_at`.
Повтор идентичного POST с тем же ID возвращает существующий снимок; другое содержимое — 409.
Каждый экспорт создаёт отдельный документ. Скачивание уже сохранённого документа новый снимок не создаёт.

## Ошибки

Ошибка содержит `detail`: строку или список ошибок валидации.
401 — требуется вход; 404 — объект не найден или недоступен;
409 — конфликт; 413 — файл слишком большой; 415 — неподдерживаемый формат;
422 — некорректные данные. Фронтенд показывает сообщение и сохраняет несохранённые правки при обычной ошибке запроса.
