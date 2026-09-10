"""
Этап 5б — дедупликация.

Одно и то же требование заказчик проговаривает по три раза за встречу,
плюс чанки перекрываются. Без схлопывания дублей в ТЗ приедет список
из тридцати пунктов, где десять — одно и то же. Это прямо бьёт по критерию
«корректность работы».

Считаем схожесть без эмбеддингов, чтобы не зависеть от того, какая модель
окажется под рукой: комбинация меры Жаккара по значимым словам
и посимвольного сходства. Для русского добавлено грубое усечение окончаний —
«отчёты» и «отчётов» должны совпасть.

Если позже появятся эмбеддинги, менять нужно только similarity().
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.core.chunking import STOPWORDS
from app.schemas import Item, ItemType

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Частотные русские окончания. Полноценная лемматизация тут избыточна:
# нам нужно лишь сблизить формы одного слова.
_SUFFIXES = (
    "ениями", "ениям", "ениях", "ования", "ованию", "ование", "ами", "ями",
    "ого", "ему", "ому", "ыми", "ими", "ает", "ают", "ать", "ить", "ешь",
    "ия", "ии", "ие", "ый", "ой", "ей", "ем", "ом", "ах", "ях", "ов", "ев",
    "ы", "и", "а", "я", "у", "ю", "е", "о", "ь",
)


def _stem(word: str) -> str:
    w = word.casefold().replace("ё", "е")
    if len(w) <= 4:
        return w
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _key_words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in _WORD_RE.findall(text)
        if len(w) > 2 and w.casefold() not in STOPWORDS
    }


def similarity(a: Item, b: Item) -> float:
    """0..1. Единственное место, которое надо менять при переходе на эмбеддинги."""
    # Описания ролей формулируются шаблонно («Роль X — пользователь системы»),
    # поэтому по полному тексту юрист и бухгалтер выглядят как один и тот же
    # элемент. Для ролей сравниваем только название.
    if a.type == ItemType.USER_ROLE and b.type == ItemType.USER_ROLE:
        na = (a.role or a.title).casefold().strip()
        nb = (b.role or b.title).casefold().strip()
        if not na or not nb:
            return 0.0
        return 1.0 if _stem(na) == _stem(nb) else SequenceMatcher(None, na, nb).ratio()

    wa, wb = _key_words(f"{a.title} {a.text}"), _key_words(f"{b.title} {b.text}")
    if not wa or not wb:
        return 0.0

    jaccard = len(wa & wb) / len(wa | wb)
    # Покрытие: короткая формулировка, целиком входящая в длинную, — тоже дубль.
    coverage = len(wa & wb) / min(len(wa), len(wb))
    chars = SequenceMatcher(None, a.text.casefold(), b.text.casefold()).ratio()

    return max(0.55 * jaccard + 0.45 * chars, 0.7 * coverage + 0.3 * chars)


def _merge(primary: Item, dup: Item) -> Item:
    """
    Слить дубль в основной элемент.

    Повтор одной и той же мысли в разговоре — признак важности, поэтому
    уверенность слегка растёт. Из двух формулировок оставляем более полную,
    источники объединяем — пользователь увидит все места, где это звучало.
    """
    if len(dup.text) > len(primary.text) * 1.25 and dup.confidence >= primary.confidence:
        primary.title, primary.text = dup.title, dup.text

    primary.tags = sorted(set(primary.tags) | set(dup.tags))
    primary.acceptance_criteria = list(
        dict.fromkeys(primary.acceptance_criteria + dup.acceptance_criteria)
    )
    primary.role = primary.role or dup.role
    primary.actor = primary.actor or dup.actor
    primary.priority = primary.priority or dup.priority
    primary.user_story = primary.user_story or dup.user_story

    if primary.source and dup.source:
        primary.source.segment_ids = sorted(
            set(primary.source.segment_ids) | set(dup.source.segment_ids)
        )
        # Подтверждённая цитата всегда лучше неподтверждённой.
        if dup.source.verified and not primary.source.verified:
            primary.source = dup.source
    elif dup.source and not primary.source:
        primary.source = dup.source

    primary.confidence = min(1.0, max(primary.confidence, dup.confidence) + 0.05)
    return primary


def candidate_pairs(
    items: list[Item], low: float = 0.55, high: float = 0.82
) -> list[tuple[Item, Item, float]]:
    """
    «Серая зона»: пары, похожие настолько, что могут быть одним и тем же,
    но недостаточно, чтобы склеивать их автоматически.

    Замер на реальных формулировках показал, что лексической мерой эти классы
    не разделяются: перефразировка «корпоративная учётная запись» /
    «корпоративная учётка» даёт 0.55, а разные требования «уведомление
    на почту» / «уведомление в телеграм» — 0.80. Порогом тут не обойтись,
    поэтому такие пары уходят на решение языковой модели (см. pipeline).
    """
    out: list[tuple[Item, Item, float]] = []
    by_type: dict[str, list[Item]] = {}
    for it in items:
        by_type.setdefault(it.type.value, []).append(it)

    for group in by_type.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                score = similarity(a, b)
                if low <= score < high:
                    out.append((a, b, score))

    out.sort(key=lambda p: -p[2])
    return out


def merge_pair(primary: Item, dup: Item) -> Item:
    """Публичная обёртка над слиянием — нужна для подтверждённых моделью пар."""
    return _merge(primary, dup)


def deduplicate(items: list[Item], threshold: float = 0.82) -> tuple[list[Item], int]:
    """
    Схлопнуть дубли. Сравниваем только внутри одного типа: похожие
    формулировки требования и договорённости — это разные сущности.

    Возвращает (уникальные элементы, сколько схлопнуто).
    """
    by_type: dict[str, list[Item]] = {}
    for it in items:
        by_type.setdefault(it.type.value, []).append(it)

    result: list[Item] = []
    merged_count = 0

    for group in by_type.values():
        # Сначала самые уверенные — они станут «основными» при слиянии.
        group.sort(key=lambda i: (-i.confidence, len(i.text)))
        kept: list[Item] = []
        for candidate in group:
            match = None
            for existing in kept:
                if similarity(existing, candidate) >= threshold:
                    match = existing
                    break
            if match is not None:
                _merge(match, candidate)
                merged_count += 1
            else:
                kept.append(candidate)
        result.extend(kept)

    return result, merged_count
