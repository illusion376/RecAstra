"""
Проверка интеграции с Яндексом — без обращения к облаку.

HTTP-транспорт подменён заглушкой: она отвечает так же, как отвечает
Foundation Models API, и заодно даёт посмотреть, что именно ушло в запросе.
Так проверяется форма запроса, разбор ответа и поведение на ошибках,
для которых иначе пришлось бы ждать реального 401 или 429.

    python test_yandex.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from app.config import Settings  # noqa: E402
from app.core.llm.base import LLMError  # noqa: E402
from app.core.llm.yandex_gpt import YandexGPTProvider  # noqa: E402
from app.core.stt import stt_to_segments as speechkit_to_segments  # noqa: E402

ok = 0
fail = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ OK ] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def make_provider(handler, **overrides: Any) -> tuple[YandexGPTProvider, list[httpx.Request]]:
    """Провайдер с подменённым транспортом. Возвращает его и журнал запросов."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    settings = Settings(
        llm_provider="yandex_gpt",
        yandex_folder_id="b1gtest000folder",
        yandex_api_key="AQVN-test-key",
        yandex_model="yandexgpt",
        llm_max_retries=overrides.pop("llm_max_retries", 2),
        llm_temperature=0.1,
        llm_max_tokens=2000,
        **overrides,
    )
    client = httpx.AsyncClient(
        base_url=settings.yandex_base_url,
        headers=YandexGPTProvider._auth_headers(settings),
        transport=httpx.MockTransport(_handler),
    )
    return YandexGPTProvider(settings, client=client), seen


def reply(text: str, *, tokens_in: int = 120, tokens_out: int = 45) -> httpx.Response:
    """Ответ в форме Foundation Models API."""
    return httpx.Response(
        200,
        json={
            "result": {
                "alternatives": [
                    {
                        "message": {"role": "assistant", "text": text},
                        "status": "ALTERNATIVE_STATUS_FINAL",
                    }
                ],
                "usage": {
                    "inputTextTokens": str(tokens_in),
                    "completionTokens": str(tokens_out),
                    "totalTokens": str(tokens_in + tokens_out),
                },
                "modelVersion": "23.10.2024",
            }
        },
    )


def main() -> int:
    print("\n=== 1. Форма запроса к YandexGPT ===")

    provider, seen = make_provider(lambda req, n: reply('{"items": []}'))
    result = asyncio.run(provider.complete_json("СИСТЕМНЫЙ ПРОМПТ", "ПОЛЬЗОВАТЕЛЬСКИЙ"))

    check("ответ разобран", result == {"items": []})
    req = seen[0]
    body = json.loads(req.content)

    check("путь совпадает с API", req.url.path == "/foundationModels/v1/completion", req.url.path)
    check(
        "modelUri собран из folder_id",
        body["modelUri"] == "gpt://b1gtest000folder/yandexgpt/latest",
        body["modelUri"],
    )
    check(
        "авторизация через Api-Key",
        req.headers.get("Authorization") == "Api-Key AQVN-test-key",
    )
    check("каталог продублирован заголовком", req.headers.get("x-folder-id") == "b1gtest000folder")
    check(
        "maxTokens передан строкой",
        isinstance(body["completionOptions"]["maxTokens"], str),
        repr(body["completionOptions"]["maxTokens"]),
    )
    check("stream выключен", body["completionOptions"]["stream"] is False)
    check(
        "сообщения используют поле text, а не content",
        body["messages"][0]["text"] == "СИСТЕМНЫЙ ПРОМПТ"
        and body["messages"][1]["text"] == "ПОЛЬЗОВАТЕЛЬСКИЙ",
    )
    check(
        "токены учтены в статистике",
        provider.tokens_in == 120 and provider.tokens_out == 45,
        f"in={provider.tokens_in}, out={provider.tokens_out}",
    )

    print("\n=== 2. Реальное поведение модели ===")

    # YandexGPT часто оборачивает JSON в ```json — парсер обязан это пережить.
    provider, _ = make_provider(
        lambda req, n: reply('Вот результат:\n```json\n{"items": [{"type": "constraint"}]}\n```')
    )
    out = asyncio.run(provider.complete_json("s", "u"))
    check("JSON в ```-обёртке с текстом вокруг разобран", out["items"][0]["type"] == "constraint")

    # Обрыв по лимиту токенов даёт битый JSON — нужна внятная ошибка, а не мусор.
    def truncated(req: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "alternatives": [
                        {
                            "message": {"role": "assistant", "text": '{"items": [{"ty'},
                            "status": "ALTERNATIVE_STATUS_TRUNCATED_FINAL",
                        }
                    ]
                }
            },
        )

    provider, seen = make_provider(truncated, llm_max_retries=3)
    try:
        asyncio.run(provider.complete_json("s", "u"))
        check("обрыв по токенам даёт понятную ошибку", False)
    except LLMError as exc:
        check("обрыв по токенам даёт понятную ошибку", "лимит" in str(exc).lower(), str(exc)[:90])
    check("обрыв не ретраится", len(seen) == 1, f"запросов: {len(seen)}")

    print("\n=== 3. Ошибки облака ===")

    provider, seen = make_provider(
        lambda req, n: httpx.Response(401, text="unauthorized"), llm_max_retries=3
    )
    try:
        asyncio.run(provider.complete_json("s", "u"))
        check("401 объясняет причину", False)
    except LLMError as exc:
        check("401 объясняет причину", "IAM" in str(exc) or "API_KEY" in str(exc).upper())
    # Неверный ключ не исправится повтором: на двадцати чанках лишние
    # попытки с паузами превращают мгновенный отказ в минуты ожидания.
    check("401 не ретраится", len(seen) == 1, f"запросов: {len(seen)}")

    provider, seen = make_provider(
        lambda req, n: httpx.Response(403, text="forbidden"), llm_max_retries=3
    )
    try:
        asyncio.run(provider.complete_json("s", "u"))
        check("403 подсказывает про роль", False)
    except LLMError as exc:
        check("403 подсказывает про роль", "ai.languageModels.user" in str(exc))
    check("403 не ретраится", len(seen) == 1, f"запросов: {len(seen)}")

    # 429 — штатная ситуация при лимитах RPS, должен ретраиться и в итоге упасть внятно.
    calls = {"n": 0}

    def rate_limited(req: httpx.Request, n: int) -> httpx.Response:
        calls["n"] = n
        return httpx.Response(429, text="too many requests")

    provider, _ = make_provider(rate_limited, llm_max_retries=2)
    try:
        asyncio.run(provider.complete_json("s", "u"))
        check("429 приводит к ошибке после повторов", False)
    except LLMError:
        check("429 приводит к ошибке после повторов", calls["n"] == 2, f"попыток: {calls['n']}")

    print("\n=== 4. jsonObject снимается, если каталог его не знает ===")

    def reject_json_mode(req: httpx.Request, n: int) -> httpx.Response:
        body = json.loads(req.content)
        if "jsonObject" in body:
            return httpx.Response(400, text="unknown field jsonObject")
        return reply('{"items": []}')

    provider, seen = make_provider(reject_json_mode, yandex_json_mode=True, llm_max_retries=3)
    out = asyncio.run(provider.complete_json("s", "u"))
    check("после отказа запрос повторён без jsonObject", out == {"items": []})
    check(
        "во втором запросе поля уже нет",
        "jsonObject" in json.loads(seen[0].content) and "jsonObject" not in json.loads(seen[1].content),
    )

    print("\n=== 5. Адаптер SpeechKit → сегменты ===")

    # Форма v3: recognizeFileAsync, времена в миллисекундах строкой.
    v3 = {
        "result": [
            {
                "final": {
                    "alternatives": [
                        {
                            "text": "Нам нужно чтобы согласование шло в одной системе",
                            "startTimeMs": "15200",
                            "endTimeMs": "31800",
                        }
                    ],
                    "channelTag": "1",
                },
                "speakerTag": "1",
            },
            {
                "final": {
                    "alternatives": [
                        {"text": "Понял, а кто участвует в процессе", "startTimeMs": "31800", "endTimeMs": "44500"}
                    ],
                    "speakerTag": "2",
                }
            },
        ]
    }
    segs = speechkit_to_segments(v3)
    check("v3: оба фрагмента разобраны", len(segs) == 2, f"{len(segs)} сегментов")
    check("v3: миллисекунды переведены в секунды", segs[0].start == 15.2 and segs[0].end == 31.8,
          f"{segs[0].start}–{segs[0].end}")
    check("v3: id проставлены подряд", [s.id for s in segs] == [0, 1])
    check("v3: метка говорящего перенесена", segs[1].speaker == "Говорящий 2", str(segs[1].speaker))

    # Форма v2: longRunningRecognize, времена у слов строками «12.340s».
    v2 = {
        "response": {
            "chunks": [
                {
                    "alternatives": [
                        {
                            "text": "Только на нашем сервере, облако не рассматриваем",
                            "confidence": 1,
                            "words": [
                                {"startTime": "480.100s", "endTime": "480.600s", "word": "Только"},
                                {"startTime": "497.200s", "endTime": "498.800s", "word": "рассматриваем"},
                            ],
                        }
                    ],
                    "channelTag": "1",
                }
            ]
        }
    }
    segs = speechkit_to_segments(v2)
    check("v2: фрагмент разобран", len(segs) == 1)
    check("v2: времена взяты из слов", segs[0].start == 480.1 and segs[0].end == 498.8,
          f"{segs[0].start}–{segs[0].end}")

    # Текст без слов и без времён — анализ должен остаться возможен.
    segs = speechkit_to_segments({"chunks": [{"alternatives": [{"text": "Договорились, спасибо"}]}]})
    check("без таймкодов сегмент всё равно создан", len(segs) == 1 and segs[0].end > segs[0].start)

    # Готовые сегменты Whisper проходят насквозь — на случай, если команда
    # оставит Whisper или будет сравнивать два движка.
    segs = speechkit_to_segments(
        {"segments": [{"id": 0, "start": 1.0, "end": 2.0, "text": "тест", "speaker": "Заказчик"}]}
    )
    check("сегменты Whisper пропускаются как есть", len(segs) == 1 and segs[0].speaker == "Заказчик")

    # Порядок по времени при раздельных дорожках.
    two_channels = [
        {"final": {"alternatives": [{"text": "второй", "startTimeMs": "9000", "endTimeMs": "9500"}], "channelTag": "2"}},
        {"final": {"alternatives": [{"text": "первый", "startTimeMs": "1000", "endTimeMs": "1500"}], "channelTag": "1"}},
    ]
    segs = speechkit_to_segments(two_channels)
    check("дорожки склеены по времени", [s.text for s in segs] == ["первый", "второй"])

    # Непонятный ответ обязан падать, а не возвращать пустоту:
    # иначе анализ «успешно» вернёт ноль требований.
    for bad in ({"foo": "bar"}, {"result": {}}, []):
        try:
            speechkit_to_segments(bad)
            check(f"мусор {bad!r} отвергнут", False)
        except ValueError:
            check(f"мусор {str(bad)[:20]!r} отвергнут", True)

    print(f"\n{'=' * 60}\nПройдено: {ok}, провалено: {fail}\n{'=' * 60}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
