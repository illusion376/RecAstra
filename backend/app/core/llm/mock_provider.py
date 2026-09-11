"""
Mock-провайдер: детерминированное извлечение по лингвистическим правилам.

Зачем он нужен, хотя настоящая работа — за LLM:

  1. LLM на хакатоне ещё не выбрана, а фронту API нужно уже сейчас.
     С этим провайдером бэкенд поднимается без ключей и сети и отдаёт
     реалистичные данные с настоящими цитатами и таймкодами.
  2. Это аварийный запасной вариант на демо: если на защите отвалится сеть
     или кончится квота, продукт не превращается в белый экран —
     достаточно поменять LLM_PROVIDER=mock.
  3. Это база для сравнения: видно, что именно LLM даёт сверх правил.

Правила работают по маркерам русской деловой речи. Качество ниже, чем у LLM,
поэтому confidence здесь намеренно занижен.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.core.llm.base import LLMProvider

# Маркеры типов. Порядок проверки — от самого специфичного к общему,
# иначе «если система должна…» уедет не в тот тип.
RULES: list[tuple[str, tuple[str, ...], float]] = [
    (
        "agreement",
        (
            "договорились", "договорились о", "решили что", "мы решили",
            "остановимся на", "фиксируем", "делаем так", "принято",
            "по итогу решили", "сошлись на",
        ),
        0.72,
    ),
    (
        "open_question",
        (
            "надо уточнить", "нужно уточнить", "уточним", "пока не знаем",
            "пока непонятно", "непонятно пока", "вернемся к этому",
            "вернёмся к этому", "надо спросить", "спрошу у", "под вопросом",
            "открытый вопрос", "не решили", "надо подумать", "уточнить у",
            "я не знаю", "не уверен", "не уверена", "это к ", "вопрос к ",
        ),
        0.70,
    ),
    (
        "constraint",
        (
            "нельзя", "запрещено", "не более", "не менее", "не должно",
            "не должен", "только на", "исключительно", "не рассматриваем",
            "не поддерживаем", "ограничен", "ограничение", "максимум",
            "не позднее", "обязательно на", "не выше", "не ниже",
        ),
        0.68,
    ),
    (
        "condition",
        (
            "если ", "при условии", "в случае если", "в случае когда",
            "кроме случаев", "при этом если", "когда сумма", "в зависимости от",
        ),
        0.62,
    ),
    (
        "user_scenario",
        (
            "сначала", "потом он", "затем он", "после этого", "первым шагом",
            "по шагам", "открывает", "нажимает", "выбирает период",
            "заходит в", "переходит в", "пошагово",
        ),
        0.60,
    ),
    (
        "functional_requirement",
        (
            "должен иметь возможность", "система должна", "должен", "должна",
            "должно", "нужно чтобы", "нужна возможность", "хотим чтобы",
            "хотелось бы чтобы", "необходимо чтобы", "чтобы можно было",
            "надо сделать", "надо реализовать", "реализовать", "требуется",
            "нам нужен", "нам нужна", "нам нужно",
        ),
        0.65,
    ),
]

# Роли ищем словарём — в разговоре они называются прямо.
ROLE_WORDS = [
    "бухгалтер", "главбух", "юрист", "администратор", "админ", "руководитель",
    "сотрудник", "менеджер", "клиент", "аудитор", "оператор", "аналитик",
    "директор", "кадровик", "снабженец", "кладовщик", "супервайзер",
    "модератор", "подрядчик", "заказчик",
]

_LINE_RE = re.compile(r"^\[#(\d+)\s*\|\s*([\d:]+)\s*\|\s*([^\]]+)\]\s*(.*)$")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[а-яёa-z0-9-]+", re.IGNORECASE)

MUST_MARKERS = ("обязательно", "критично", "без этого никак", "must", "должен", "должна")
COULD_MARKERS = ("было бы неплохо", "хотелось бы", "в идеале", "желательно", "если успеем")


class MockProvider(LLMProvider):
    name = "mock"

    @property
    def model(self) -> str:
        return "rule-based-ru"

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        # Определяем, какой из промптов конвейера нас вызвал.
        if "карту разговора" in system or '"topics"' in system:
            return self._build_map(user)
        if '"contradictions"' in system:
            return {"contradictions": []}
        if '"same_pairs"' in system:
            # Отличить перефразировку от разного требования правилами нельзя —
            # именно поэтому эта проверка и отдана модели. Без неё ничего
            # не склеиваем: лишний дубль безопаснее потерянного требования.
            return {"same_pairs": []}
        return {"items": self._extract(user)}

    # --- Карта разговора --------------------------------------------------

    def _build_map(self, user: str) -> dict[str, Any]:
        text = user
        words = [
            w.casefold()
            for w in _WORD_RE.findall(text)
            if len(w) > 5
        ]
        top = [w for w, _ in Counter(words).most_common(8)]
        roles = sorted({r.capitalize() for r in ROLE_WORDS if r in text.casefold()})
        summary = "Разбор встречи выполнен без языковой модели, по набору правил."
        if top:
            summary += " Ключевые темы определены по частотности терминов: " + ", ".join(top[:5]) + "."
        return {
            "summary": summary,
            "topics": [
                {"title": w.capitalize(), "summary": f"Обсуждение: {w}.", "segment_ids": []}
                for w in top[:5]
            ],
            "roles_mentioned": roles,
            "glossary": {},
        }

    # --- Извлечение -------------------------------------------------------

    def _extract(self, user: str) -> list[dict[str, Any]]:
        fragment = self._take_fragment(user)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        for seg_id, speaker, line_text in self._parse_lines(fragment):
            for sentence in self._split_sentences(line_text):
                low = sentence.casefold()
                if len(sentence) < 25:
                    continue

                item_type, confidence = self._classify(low)
                if item_type is None:
                    continue

                key = low[:60]
                if key in seen:
                    continue
                seen.add(key)

                role = self._find_role(low)
                items.append(
                    {
                        "type": item_type,
                        "title": self._make_title(sentence),
                        "text": self._to_spec_style(sentence, item_type),
                        "role": role,
                        "actor": self._actor(speaker),
                        "priority": self._priority(low, item_type),
                        "confidence": confidence,
                        "quote": sentence.strip(),
                        "segment_ids": [seg_id],
                        "user_story": None,
                        "acceptance_criteria": [],
                        "tags": self._tags(low),
                    }
                )

        # Роли добавляем отдельными элементами — их в кейсе просят как тип.
        items.extend(self._extract_roles(fragment, seen))
        return items

    # --- Вспомогательное --------------------------------------------------

    @staticmethod
    def _take_fragment(user: str) -> str:
        """Работаем только с рабочим фрагментом, контекстные блоки игнорируем."""
        marker = "ФРАГМЕНТ РАЗГОВОРА ДЛЯ АНАЛИЗА"
        idx = user.find(marker)
        if idx == -1:
            return user
        body = user[idx:]
        newline = body.find("\n")
        return body[newline + 1 :] if newline != -1 else body

    @staticmethod
    def _parse_lines(fragment: str) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for line in fragment.splitlines():
            m = _LINE_RE.match(line.strip())
            if m:
                out.append((int(m.group(1)), m.group(3).strip(), m.group(4).strip()))
        return out

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        return [s.strip() for s in _SENT_RE.split(text) if s.strip()]

    @staticmethod
    def _classify(low: str) -> tuple[str | None, float]:
        # Вопрос — не утверждение. «Где система должна работать?» содержит
        # слово «должна», но требованием не является: это специалист уточняет
        # условия, а само требование прозвучит в ответе заказчика.
        if low.rstrip().endswith("?"):
            return None, 0.0
        for item_type, markers, conf in RULES:
            if any(m in low for m in markers):
                return item_type, conf
        # Вопросительный знак сам по себе открытым вопросом не делает:
        # на встрече специалист задаёт уточняющие вопросы постоянно, и почти
        # на все получает ответ здесь же. Открытый вопрос — это только то,
        # что осталось нерешённым, а это видно по маркерам выше.
        return None, 0.0

    @staticmethod
    def _find_role(low: str) -> str | None:
        for r in ROLE_WORDS:
            if r in low:
                return r.capitalize()
        return None

    @staticmethod
    def _actor(speaker: str) -> str | None:
        low = speaker.casefold()
        if "заказчик" in low:
            return "customer"
        if "специалист" in low:
            return "specialist"
        return None

    @staticmethod
    def _priority(low: str, item_type: str) -> str | None:
        if item_type != "functional_requirement":
            return None
        if any(m in low for m in COULD_MARKERS):
            return "could"
        if any(m in low for m in MUST_MARKERS):
            return "must"
        return "should"

    @staticmethod
    def _make_title(sentence: str) -> str:
        clean = sentence.strip().rstrip(".!?")
        if len(clean) <= 80:
            return clean
        cut = clean[:80].rsplit(" ", 1)[0]
        return cut + "…"

    @staticmethod
    def _to_spec_style(sentence: str, item_type: str) -> str:
        text = sentence.strip()
        text = text[0].upper() + text[1:] if text else text
        if not text.endswith((".", "!", "?")):
            text += "."
        prefix = {
            "functional_requirement": "",
            "constraint": "Ограничение: ",
            "condition": "Условие: ",
            "open_question": "Требует уточнения: ",
            "agreement": "Договорённость: ",
            "user_scenario": "Сценарий: ",
            "user_role": "",
        }.get(item_type, "")
        return prefix + text

    @staticmethod
    def _tags(low: str) -> list[str]:
        vocab = {
            "авторизация": ("вход", "логин", "пароль", "учетн", "учётн", "sso"),
            "отчёты": ("отчет", "отчёт", "выгрузк", "экспорт"),
            "интеграция": ("интеграц", "api", "обмен", "1с", "црм", "crm"),
            "уведомления": ("уведомлен", "оповещен", "письм", "почт"),
            "доступ": ("доступ", "прав", "роли"),
            "документы": ("документ", "договор", "накладн", "счет", "счёт"),
        }
        return [tag for tag, keys in vocab.items() if any(k in low for k in keys)][:3]

    def _extract_roles(self, fragment: str, seen: set[str]) -> list[dict[str, Any]]:
        found: dict[str, tuple[int, str]] = {}
        for seg_id, _speaker, line_text in self._parse_lines(fragment):
            low = line_text.casefold()
            for r in ROLE_WORDS:
                if r in low and r not in found:
                    found[r] = (seg_id, line_text)

        out: list[dict[str, Any]] = []
        for role, (seg_id, quote) in found.items():
            key = f"role::{role}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "type": "user_role",
                    "title": role.capitalize(),
                    "text": f"Роль «{role}» упоминается в разговоре как пользователь системы.",
                    "role": role.capitalize(),
                    "actor": None,
                    "priority": None,
                    "confidence": 0.5,
                    "quote": quote.strip(),
                    "segment_ids": [seg_id],
                    "user_story": None,
                    "acceptance_criteria": [],
                    "tags": ["роли"],
                }
            )
        return out
