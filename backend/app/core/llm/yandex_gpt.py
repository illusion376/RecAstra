"""
Провайдер YandexGPT — Yandex Cloud Foundation Models.

    POST {base}/foundationModels/v1/completion
    Authorization: Api-Key <ключ>        (или Bearer <IAM-токен>)

Отличия от OpenAI-совместимого API, из-за которых нужен отдельный класс:
  * своя форма запроса: modelUri вместо model, completionOptions вместо
    плоских параметров, messages с полем "text" вместо "content";
  * maxTokens передаётся строкой, а не числом;
  * ответ лежит в result.alternatives[0].message.text;
  * при авторизации IAM-токеном каталог передаётся заголовком x-folder-id;
  * жёсткие лимиты по RPS — параллелизм стоит держать низким.

У Yandex Cloud есть и OpenAI-совместимая ручка (/v1/chat/completions).
Если она в вашем каталоге работает, подойдёт провайдер openai_compat
без этого файла — см. .env.example. Нативный путь надёжнее: он не зависит
от слоя совместимости и даёт статистику по токенам.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import Settings
from app.core.llm.base import LLMError, LLMFatalError, LLMProvider, extract_json

log = logging.getLogger(__name__)

COMPLETION_PATH = "/foundationModels/v1/completion"


class YandexGPTProvider(LLMProvider):
    name = "yandex_gpt"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self._s = settings

        if not (settings.yandex_api_key or settings.yandex_iam_token):
            log.error(
                "YandexGPT выбран, но не задан ни YANDEX_API_KEY, ни YANDEX_IAM_TOKEN"
            )
        if not settings.yandex_folder_id and not settings.yandex_model.startswith("gpt://"):
            log.error("YandexGPT выбран, но не задан YANDEX_FOLDER_ID")

        self._client = client or httpx.AsyncClient(
            base_url=settings.yandex_base_url.rstrip("/"),
            headers=self._auth_headers(settings),
            timeout=httpx.Timeout(settings.llm_timeout),
        )
        self._sem = asyncio.Semaphore(settings.llm_concurrency)
        # Снимается автоматически, если каталог не понимает jsonObject.
        self._json_mode = settings.yandex_json_mode

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.yandex_api_key:
            headers["Authorization"] = f"Api-Key {settings.yandex_api_key}"
        elif settings.yandex_iam_token:
            headers["Authorization"] = f"Bearer {settings.yandex_iam_token}"
        # При авторизации IAM-токеном каталог нужно указать явно.
        if settings.yandex_folder_id:
            headers["x-folder-id"] = settings.yandex_folder_id
        return headers

    @property
    def model(self) -> str:
        return self._s.yandex_model_uri

    def _build_payload(
        self, system: str, user: str, max_tokens: int | None, temperature: float | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "modelUri": self._s.yandex_model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": (
                    temperature if temperature is not None else self._s.llm_temperature
                ),
                # API ожидает строку, число здесь приводит к ошибке валидации.
                "maxTokens": str(max_tokens or self._s.llm_max_tokens),
            },
            "messages": [
                {"role": "system", "text": system},
                {"role": "user", "text": user},
            ],
        }
        if self._json_mode:
            payload["jsonObject"] = True
        return payload

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(1, self._s.llm_max_retries + 1):
            payload = self._build_payload(system, user, max_tokens, temperature)
            try:
                async with self._sem:
                    resp = await self._client.post(COMPLETION_PATH, json=payload)

                if resp.status_code == 401:
                    # IAM-токен живёт 12 часов — самая частая причина.
                    raise LLMFatalError(
                        "YandexGPT: 401. Проверьте YANDEX_API_KEY или обновите "
                        f"IAM-токен (он истекает через 12 часов). {resp.text[:200]}"
                    )
                if resp.status_code == 403:
                    raise LLMFatalError(
                        "YandexGPT: 403. У сервисного аккаунта нет роли "
                        f"ai.languageModels.user на каталоге. {resp.text[:200]}"
                    )
                if resp.status_code == 400 and self._json_mode:
                    # Каталог не понимает строгий JSON — снимаем и повторяем.
                    log.warning(
                        "YandexGPT отверг jsonObject, продолжаю без него: %s",
                        resp.text[:200],
                    )
                    self._json_mode = False
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise LLMError(f"YandexGPT: HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 400:
                    raise LLMError(f"YandexGPT: HTTP {resp.status_code}: {resp.text[:300]}")

                data = resp.json()
                self.calls += 1
                self._track_usage(data)
                return extract_json(self._extract_text(data))

            except LLMFatalError:
                # Ключ, права, обрыв по токенам — повтор не поможет, выходим сразу.
                raise
            except (httpx.HTTPError, LLMError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt < self._s.llm_max_retries:
                    delay = min(2**attempt, 8)
                    log.warning(
                        "YandexGPT: попытка %s/%s не удалась (%s). Повтор через %sс",
                        attempt,
                        self._s.llm_max_retries,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise LLMError(
            f"YandexGPT недоступен после {self._s.llm_max_retries} попыток: {last_error}"
        )

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """Достать текст ответа, внятно ругаясь на неожиданную форму."""
        result = data.get("result") or data
        alternatives = result.get("alternatives") or []
        if not alternatives:
            raise LLMError(f"YandexGPT вернул ответ без alternatives: {str(data)[:300]}")

        first = alternatives[0]
        status = str(first.get("status", ""))
        # Обрыв по лимиту токенов даёт невалидный JSON — говорим об этом прямо.
        if "TRUNCATED" in status:
            raise LLMFatalError(
                "YandexGPT оборвал ответ по лимиту токенов (status "
                f"{status}). Увеличьте LLM_MAX_TOKENS или уменьшите "
                "OPTIONS.max_chunk_chars."
            )
        if "CONTENT_FILTER" in status:
            raise LLMFatalError("YandexGPT отклонил ответ фильтром контента")

        text = (first.get("message") or {}).get("text")
        if not text:
            raise LLMError(f"YandexGPT вернул пустой текст: {str(first)[:300]}")
        return str(text)

    def _track_usage(self, data: dict[str, Any]) -> None:
        """Токены приходят строками — складываем их для статистики и оценки цены."""
        usage = (data.get("result") or data).get("usage") or {}
        try:
            self.tokens_in += int(usage.get("inputTextTokens", 0) or 0)
            self.tokens_out += int(usage.get("completionTokens", 0) or 0)
        except (TypeError, ValueError):
            pass

    async def aclose(self) -> None:
        await self._client.aclose()
