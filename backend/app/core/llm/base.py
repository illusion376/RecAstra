"""
Интерфейс LLM-провайдера.

Провайдер на хакатоне ещё не выбран, поэтому конвейер не знает, с чем работает.
Он умеет ровно одно: попросить JSON по схеме и получить словарь.
Всё остальное — дело реализации.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

# Модели любят обрамлять JSON тройными кавычками и предварять его болтовнёй.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


class LLMFatalError(LLMError):
    """
    Ошибка, которую повтор не исправит: неверный ключ, отсутствие прав,
    обрыв ответа по лимиту токенов, срабатывание фильтра контента.

    Повторять такие запросы вредно: на разговоре в двадцать чанков три
    попытки с паузами превращают мгновенный отказ в минуты ожидания —
    ровно в тот момент, когда на защите нужно быстро понять, что не так.
    """


class LLMProvider(ABC):
    """Базовый класс. Считает вызовы — они попадают в статистику анализа."""

    name: str = "base"

    def __init__(self) -> None:
        self.calls: int = 0
        # Заполняют те провайдеры, что отдают usage. Нужны, чтобы на защите
        # можно было назвать цену одного анализа.
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Вернуть распарсенный JSON-объект. Бросить LLMError, если не вышло."""

    async def aclose(self) -> None:  # pragma: no cover - не у всех есть ресурсы
        return None


def extract_json(raw: str) -> dict[str, Any]:
    """
    Достать JSON-объект из ответа модели.

    Работает и когда включён json mode (тогда это просто json.loads),
    и когда модель обернула ответ в ```json, и когда добавила текст вокруг.
    """
    if not raw or not raw.strip():
        raise LLMError("Модель вернула пустой ответ")

    text = raw.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    except json.JSONDecodeError:
        pass

    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        except json.JSONDecodeError:
            text = fenced.group(1)

    # Последняя попытка: вырезать самый внешний {...} по балансу скобок,
    # игнорируя скобки внутри строк.
    start = text.find("{")
    if start == -1:
        raise LLMError(f"В ответе модели нет JSON-объекта: {text[:200]!r}")

    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Невалидный JSON от модели: {exc}") from exc

    raise LLMError("JSON от модели оборвался — вероятно, упёрлись в лимит токенов")
