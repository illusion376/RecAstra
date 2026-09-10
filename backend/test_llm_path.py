"""
Проверка пути настоящей LLM — без обращения к сети.

Подменяем провайдер заглушкой, которая возвращает ответы в том виде, в каком
их даёт языковая модель: с обёрткой ```json, с лишним текстом вокруг,
с выдуманной цитатой и с элементом неизвестного типа. Проверяем, что конвейер
всё это переживает и что механизм отлова галлюцинаций срабатывает.

    python test_llm_path.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from app.config import get_settings  # noqa: E402
from app.core.llm.base import LLMProvider, extract_json  # noqa: E402
from app.core.pipeline import AnalysisPipeline  # noqa: E402
from app.schemas import Analysis, AnalyzeOptions, TranscriptSegment  # noqa: E402

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


# --- Заглушка провайдера ---------------------------------------------------


class StubProvider(LLMProvider):
    """Отдаёт заранее заготовленные ответы «как настоящая модель»."""

    name = "stub"

    @property
    def model(self) -> str:
        return "stub-model"

    async def complete_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if '"topics"' in system:
            return {
                "summary": "Обсуждали систему согласования договоров.",
                "topics": [{"title": "Маршрут согласования", "summary": "Кто и в каком порядке согласует.", "segment_ids": [5, 6]}],
                "roles_mentioned": ["Менеджер", "Юрист"],
                "glossary": {"контрагент": "вторая сторона договора"},
            }
        if '"contradictions"' in system:
            return {"contradictions": []}
        if '"same_pairs"' in system:
            # Имитируем то, ради чего этот проход и добавлен: модель узнаёт
            # перефразировку там, где лексическая мера её не видит.
            ids = re.findall(r"([AB])=(item_[0-9a-f]+): (.+)", user)
            pairs = []
            for n in range(0, len(ids) - 1, 2):
                (_, id_a, text_a), (_, id_b, text_b) = ids[n], ids[n + 1]
                if "учётн" in text_a.casefold() and "учётн" in text_b.casefold():
                    pairs.append({"id_a": id_a, "id_b": id_b, "confident": True})
            return {"same_pairs": pairs}

        return {
            "items": [
                {
                    # 1. Нормальный элемент с дословной цитатой.
                    "type": "functional_requirement",
                    "title": "Вход по корпоративной учётной записи",
                    "text": "Пользователь должен иметь возможность входить через корпоративную учётную запись.",
                    "role": "Сотрудник",
                    "actor": "customer",
                    "priority": "must",
                    "confidence": 0.95,
                    "quote": "Пользователь должен входить через нашу корпоративную учётную запись",
                    "segment_ids": [38],
                    "user_story": "Как сотрудник, я хочу входить по корпоративной учётке, чтобы не заводить лишний пароль.",
                    "acceptance_criteria": ["Отдельная учётная запись не создаётся"],
                    "tags": ["авторизация"],
                },
                {
                    # 2. Цитата с изменённой пунктуацией — нечёткий поиск обязан её найти.
                    "type": "constraint",
                    "title": "Только на своём сервере",
                    "text": "Система разворачивается только внутри контура заказчика, облако не рассматривается.",
                    "role": None,
                    "actor": "customer",
                    "priority": None,
                    "confidence": 0.9,
                    "quote": "Только на нашем сервере внутри контура — облако не рассматриваем вообще",
                    "segment_ids": [34],
                    "tags": ["инфраструктура"],
                },
                {
                    # 3. Галлюцинация: такого в разговоре не было.
                    "type": "functional_requirement",
                    "title": "Двухфакторная аутентификация",
                    "text": "Система должна поддерживать двухфакторную аутентификацию по SMS.",
                    "role": "Сотрудник",
                    "confidence": 0.9,
                    "quote": "обязательно нужна двухфакторная аутентификация по смс для всех пользователей системы",
                    "segment_ids": [38],
                    "tags": ["безопасность"],
                },
                {
                    # 4. Неизвестный тип — должен быть отброшен молча.
                    "type": "nonfunctional_requirement",
                    "title": "Мусор",
                    "text": "Этого типа в схеме нет.",
                    "quote": "договор",
                    "segment_ids": [2],
                },
                {
                    # 5. Дубль первого элемента другими словами.
                    "type": "functional_requirement",
                    "title": "Авторизация через корпоративную учётку",
                    "text": "Вход в систему выполняется по корпоративной учётной записи пользователя.",
                    "role": "Сотрудник",
                    "confidence": 0.8,
                    "quote": "отдельные пароли заводить не хотим, это лишняя головная боль",
                    "segment_ids": [38],
                    "tags": ["авторизация", "SSO"],
                },
            ]
        }


def main() -> int:
    print("\n=== 1. Разбор «грязного» ответа модели ===")

    check(
        "чистый JSON",
        extract_json('{"items": []}') == {"items": []},
    )
    check(
        "JSON в ```json-обёртке",
        extract_json('```json\n{"a": 1}\n```') == {"a": 1},
    )
    check(
        "JSON с болтовнёй вокруг",
        extract_json('Конечно! Вот результат:\n{"a": 2}\nНадеюсь, помог.') == {"a": 2},
    )
    check(
        "фигурные скобки внутри строк не ломают разбор",
        extract_json('{"a": "текст со { скобкой"}')["a"] == "текст со { скобкой",
    )
    check(
        "массив верхнего уровня заворачивается в items",
        extract_json('[{"x": 1}]') == {"items": [{"x": 1}]},
    )
    try:
        extract_json("никакого json тут нет")
        check("текст без JSON даёт ошибку", False)
    except Exception:
        check("текст без JSON даёт ошибку", True)

    print("\n=== 2. Конвейер на ответах, похожих на настоящую LLM ===")

    payload = json.loads(
        (Path(__file__).parent / "samples" / "transcript_sample.json").read_text(encoding="utf-8")
    )
    segments = [TranscriptSegment(**s) for s in payload["segments"]]

    # Крупный чанк, чтобы заглушка отработала один раз и было видно дедупликацию.
    analysis = Analysis(options=AnalyzeOptions(max_chunk_chars=20000))
    pipeline = AnalysisPipeline(StubProvider(), get_settings())
    analysis, items = asyncio.run(pipeline.run(analysis, segments))

    by_title = {i.title: i for i in items}

    check("неизвестный тип отброшен", "Мусор" not in by_title, f"итого элементов: {len(items)}")

    auth = by_title.get("Вход по корпоративной учётной записи")
    check("дословная цитата подтверждена", bool(auth and auth.source and auth.source.verified))
    if auth and auth.source:
        check(
            "цитата привязана к сегменту и таймкоду",
            auth.source.segment_ids == [38] and auth.source.start is not None,
            f"сегменты {auth.source.segment_ids}, старт {auth.source.start}",
        )
        check("user story сохранена", bool(auth.user_story))

    constraint = by_title.get("Только на своём сервере")
    check(
        "цитата с изменённой пунктуацией найдена нечётким поиском",
        bool(constraint and constraint.source and constraint.source.verified),
        f"score={constraint.source.match_score if constraint and constraint.source else '-'}",
    )

    fake = by_title.get("Двухфакторная аутентификация")
    check("галлюцинация не отброшена, а помечена", fake is not None)
    if fake and fake.source:
        check("галлюцинация помечена verified=false", fake.source.verified is False)
        check(
            "у галлюцинации понижена уверенность",
            fake.confidence <= 0.4,
            f"confidence={fake.confidence}",
        )

    check(
        "перефразированный дубль схлопнут проходом модели",
        ("Авторизация через корпоративную учётку" in by_title)
        != ("Вход по корпоративной учётной записи" in by_title),
        "оба варианта авторизации не должны остаться порознь",
    )
    survivor = by_title.get("Вход по корпоративной учётной записи") or by_title.get(
        "Авторизация через корпоративную учётку"
    )
    check(
        "при слиянии объединились теги обеих формулировок",
        bool(survivor and "SSO" in survivor.tags and "авторизация" in survivor.tags),
        f"теги: {survivor.tags if survivor else '-'}",
    )

    check(
        "в статистике учтены неподтверждённые",
        analysis.stats.unverified_items >= 1,
        f"unverified={analysis.stats.unverified_items}",
    )
    check(
        "карта разговора построена",
        bool(analysis.conversation_map and analysis.conversation_map.topics),
    )
    check("статистика заполнена", analysis.stats.llm_calls > 0 and analysis.stats.chunks > 0)

    print(f"\n{'=' * 60}\nПройдено: {ok}, провалено: {fail}\n{'=' * 60}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
