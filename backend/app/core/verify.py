"""
Этап 5а — проверка цитат и привязка к таймкодам.

Механический детектор галлюцинаций. Модель обязана приложить к каждому
элементу дословную цитату; здесь мы проверяем, что такая фраза действительно
звучала. Если не нашли — элемент не выбрасываем молча, а помечаем
verified=false и понижаем confidence, чтобы пользователь увидел флаг
«проверить вручную». Это одновременно закрывает пункт 5 кейса: найденная
позиция даёт номера сегментов и таймкод фрагмента.

Точное совпадение бывает редко: модель почти всегда чуть правит пунктуацию.
Поэтому ищем нечётко, но не перебором всех окон (это слишком медленно),
а по якорным словам: берём из цитаты самые редкие слова, находим их места
в транскрипте и сравниваем только эти кандидатные окна.
"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Optional

from app.core.normalize import TranscriptDoc
from app.schemas import SourceRef

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е").replace("Ё", "Е")).strip().casefold()


class QuoteIndex:
    """Индекс слов транскрипта: слово → позиции. Строится один раз на анализ."""

    def __init__(self, doc: TranscriptDoc) -> None:
        self.doc = doc
        self.raw = doc.full_text
        self.norm = _norm(self.raw)

        self.words: list[str] = []
        self.starts: list[int] = []
        for m in _WORD_RE.finditer(self.norm):
            self.words.append(m.group(0))
            self.starts.append(m.start())

        self.positions: dict[str, list[int]] = defaultdict(list)
        for i, w in enumerate(self.words):
            self.positions[w].append(i)

    def find(self, quote: str, threshold: float = 0.62) -> tuple[float, int, int]:
        """
        Найти цитату. Возвращает (score, char_start, char_end).
        score == 0 означает «не нашли».
        """
        q_norm = _norm(quote)
        if len(q_norm) < 8:
            return 0.0, -1, -1

        # 1. Точное вхождение — самый частый удачный случай.
        pos = self.norm.find(q_norm)
        if pos != -1:
            return 1.0, pos, pos + len(q_norm)

        q_words = _WORD_RE.findall(q_norm)
        if len(q_words) < 3:
            return 0.0, -1, -1

        # 2. Якоря — самые редкие слова цитаты: по ним сужаем поиск.
        anchors = sorted(
            {w for w in q_words if len(w) > 3},
            key=lambda w: len(self.positions.get(w, ())) or 10**6,
        )
        anchors = [a for a in anchors if self.positions.get(a)][:5]
        if not anchors:
            return 0.0, -1, -1

        span = len(q_words)
        candidates: set[int] = set()
        for a in anchors:
            for p in self.positions[a][:200]:  # частое слово не разгоняем
                candidates.add(max(0, p - span))
                candidates.add(max(0, p - span // 2))
                candidates.add(p)

        best_score, best_i, best_j = 0.0, -1, -1
        for start_idx in sorted(candidates):
            end_idx = min(start_idx + span + 4, len(self.words))
            if end_idx - start_idx < 3:
                continue
            c_start = self.starts[start_idx]
            last = end_idx - 1
            c_end = self.starts[last] + len(self.words[last])
            window = self.norm[c_start:c_end]

            # Быстрый отсев по длине: сильно разные длины не совпадут.
            if not (0.5 <= len(window) / max(len(q_norm), 1) <= 2.0):
                continue

            ratio = SequenceMatcher(None, q_norm, window).quick_ratio()
            if ratio < threshold:
                continue
            ratio = SequenceMatcher(None, q_norm, window).ratio()
            if ratio > best_score:
                best_score, best_i, best_j = ratio, c_start, c_end

        if best_score < threshold:
            return 0.0, -1, -1
        return best_score, best_i, best_j


def verify_quote(
    quote: str,
    index: QuoteIndex,
    fallback_segment_ids: Optional[list[int]] = None,
    threshold: float = 0.62,
) -> SourceRef:
    """
    Построить SourceRef: проверить цитату и подтянуть сегменты и таймкоды.

    Если цитата не нашлась, но модель указала номера сегментов — используем их
    как запасной якорь. Пользователь всё равно сможет открыть фрагмент,
    просто с пометкой «не подтверждено».
    """
    doc = index.doc
    score, c_start, c_end = index.find(quote, threshold=threshold)

    if score > 0:
        seg_ids = doc.segments_in_range(c_start, c_end)
        if not seg_ids and fallback_segment_ids:
            seg_ids = fallback_segment_ids
        start, end = doc.time_range(seg_ids)
        return SourceRef(
            quote=quote.strip(),
            segment_ids=seg_ids,
            start=start,
            end=end,
            char_start=c_start,
            char_end=c_end,
            verified=True,
            match_score=round(score, 3),
        )

    seg_ids = [s for s in (fallback_segment_ids or []) if doc.segment_by_id(s)]
    start, end = doc.time_range(seg_ids)
    return SourceRef(
        quote=quote.strip(),
        segment_ids=seg_ids,
        start=start,
        end=end,
        verified=False,
        match_score=0.0,
    )
