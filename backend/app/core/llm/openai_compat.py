"""
Провайдер для любого сервиса с OpenAI-совместимым /chat/completions.

Проверено на: OpenAI, Ollama, LM Studio, vLLM, DeepSeek, OpenRouter.
Смена провайдера = две строки в .env, код не трогаем:

    LLM_PROVIDER=openai_compat
    LLM_BASE_URL=http://localhost:11434/v1     # Ollama
    LLM_MODEL=qwen2.5:14b-instruct
    LLM_API_KEY=                                # локальным не нужен
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import Settings
from app.core.llm.base import LLMError, LLMFatalError, LLMProvider, extract_json

log = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._s = settings
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout),
        )
        # Ограничиваем параллелизм, чтобы не ловить 429 и не душить локальную модель.
        self._sem = asyncio.Semaphore(settings.llm_concurrency)

        # Настройки, которые подбираются на лету по отказам провайдера.
        # Рассуждающие модели (семейство GPT-5) вместо max_tokens требуют
        # max_completion_tokens и не принимают температуру, отличную от 1;
        # старые модели, наоборот, не знают max_completion_tokens. Держать
        # это в конфиге значит заставить команду угадывать — проще один раз
        # получить отказ и запомнить, что модель принимает.
        self._token_param = settings.llm_token_param
        self._send_temperature = True
        self._json_mode = settings.llm_supports_json_mode

    def _adapt_payload(self, payload: dict[str, Any], error_text: str) -> bool:
        """
        Подстроить запрос под отказ провайдера. True — есть что менять,
        имеет смысл повторить; False — отказ не про параметры.
        """
        low = error_text.lower()

        if "max_completion_tokens" in low and self._token_param == "max_tokens":
            log.warning("Модель требует max_completion_tokens вместо max_tokens, переключаюсь")
            self._token_param = "max_completion_tokens"
            payload["max_completion_tokens"] = payload.pop("max_tokens", self._s.llm_max_tokens)
            return True

        if "temperature" in low and self._send_temperature:
            log.warning("Модель не принимает заданную температуру, убираю параметр")
            self._send_temperature = False
            payload.pop("temperature", None)
            return True

        if "response_format" in low and self._json_mode:
            log.warning("Провайдер отверг response_format, продолжаю без него")
            self._json_mode = False
            payload.pop("response_format", None)
            return True

        return False

    @property
    def model(self) -> str:
        return self._s.llm_model

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._send_temperature:
            payload["temperature"] = (
                temperature if temperature is not None else self._s.llm_temperature
            )
        payload[self._token_param] = max_tokens or self._s.llm_max_tokens
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None

        for attempt in range(1, self._s.llm_max_retries + 1):
            received_response = False
            try:
                async with self._sem:
                    resp = await self._client.post("/chat/completions", json=payload)

                if resp.status_code in (401, 403):
                    raise LLMFatalError(
                        f"HTTP {resp.status_code}: проверьте LLM_API_KEY и права. "
                        f"{resp.text[:200]}"
                    )
                if resp.status_code >= 500:
                    raise LLMFatalError(f"HTTP {resp.status_code}: результат обработки неизвестен. Автоповтор отключён, чтобы не дублировать возможное списание.")
                if resp.status_code == 429:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 400:
                    # Провайдеры расходятся в мелочах, и заранее угадать нельзя.
                    # Подстраиваемся под отказ и пробуем снова; настройка
                    # запоминается, так что цена — один запрос за весь запуск.
                    if self._adapt_payload(payload, resp.text):
                        continue
                    raise LLMFatalError(f"HTTP {resp.status_code}: запрос отклонён")

                received_response = True
                data = resp.json()
                self.calls += 1
                usage = data.get("usage") or {}
                self.tokens_in += int(usage.get("prompt_tokens", 0) or 0)
                self.tokens_out += int(usage.get("completion_tokens", 0) or 0)
                choice = data["choices"][0]
                if choice.get("finish_reason") in ("length", "content_filter"):
                    raise LLMFatalError(f"Ответ не завершён: finish_reason={choice['finish_reason']}. Автоповтор отключён; проверьте лимит ответа или ограничения модели.")
                content = choice["message"]["content"]
                if not isinstance(content, str):
                    raise LLMFatalError("Модель не вернула текст ответа. Автоповтор отключён.")
                return extract_json(content)

            except LLMFatalError:
                raise
            except (httpx.HTTPError, LLMError, KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
                reason = type(exc).__name__
                safe_network_retry = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                if received_response or (isinstance(exc, httpx.HTTPError) and not safe_network_retry):
                    log.error("LLM: %s; response_received=%s; автоповтор отключён: запрос мог быть оплачен", reason, received_response)
                    raise LLMFatalError(f"{reason}: ответ LLM не получен или не удалось разобрать. Запрос мог быть оплачен; автоматический повтор отключён.") from exc
                last_error = exc
                if attempt < self._s.llm_max_retries:
                    delay = min(2**attempt, 8)
                    log.warning(
                        "Вызов LLM не удался (попытка %s/%s): %s. Повтор через %sс",
                        attempt,
                        self._s.llm_max_retries,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise LLMError(f"LLM недоступна после {self._s.llm_max_retries} попыток: {type(last_error).__name__ if last_error else 'несовместимые параметры запроса'}")

    async def aclose(self) -> None:
        await self._client.aclose()
