"""
Этап 2 конвейера — смысловая сегментация.

Нарезать транскрипт «по 4000 символов» нельзя: требование запросто разорвётся
пополам между чанками и потеряется. Поэтому режем по границам тем.

Границу оцениваем по четырём признакам, без эмбеддингов и внешних моделей:
  1) длина паузы между репликами;
  2) смена говорящего;
  3) дискурсивные маркеры смены темы («давайте перейдём», «теперь про…»);
  4) лексический разрыв — насколько слабо пересекается словарь соседних окон.

Дальше жадно набираем чанк до целевого размера и режем в точке
с максимальной оценкой границы рядом с этим размером. Соседние чанки
перекрываются одной репликой, чтобы мысль на стыке не потерялась.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.normalize import TranscriptDoc, Turn

# Маркеры, которыми в живой речи объявляют переход к новой теме.
TOPIC_SHIFT_MARKERS = [
    "давайте перейдем",
    "давайте перейдём",
    "теперь про",
    "теперь давайте",
    "следующий вопрос",
    "следующий момент",
    "идем дальше",
    "идём дальше",
    "хорошо, а",
    "ладно, а",
    "окей, а",
    "еще один вопрос",
    "ещё один вопрос",
    "что касается",
    "по поводу",
    "вернемся к",
    "вернёмся к",
    "отдельная тема",
    "и последнее",
    "кстати",
]

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Стоп-слова: не несут темы, только шумят при сравнении словарей окон.
STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
}


def _content_words(text: str) -> set[str]:
    return {
        w.casefold()
        for w in _WORD_RE.findall(text)
        if len(w) > 3 and w.casefold() not in STOPWORDS
    }


@dataclass
class Chunk:
    """Смысловой фрагмент разговора — единица работы для LLM."""

    index: int
    text: str
    turns: list[Turn] = field(default_factory=list)
    segment_ids: list[int] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    # Хвост предыдущего чанка. Идёт в промпт как контекст, но извлекать
    # из него запрещено — иначе получим дубли на каждом стыке.
    overlap_text: str = ""

    @property
    def char_len(self) -> int:
        return len(self.text)


def _boundary_score(prev: Turn, cur: Turn, window_prev: str, window_cur: str) -> float:
    """Насколько сильно между двумя репликами «пахнет» сменой темы. 0..1."""
    score = 0.0

    pause = max(cur.start - prev.end, 0.0)
    score += min(pause / 4.0, 1.0) * 0.30

    if prev.speaker != cur.speaker:
        score += 0.15

    low = cur.text.casefold()
    if any(low.startswith(m) or f" {m}" in low[:80] for m in TOPIC_SHIFT_MARKERS):
        score += 0.35

    a, b = _content_words(window_prev), _content_words(window_cur)
    if a and b:
        jaccard = len(a & b) / len(a | b)
        score += (1.0 - jaccard) * 0.20
    else:
        score += 0.10

    return min(score, 1.0)


def chunk_transcript(
    doc: TranscriptDoc,
    max_chars: int = 4000,
    min_chars: int = 900,
    overlap_chars: int = 400,
) -> list[Chunk]:
    """Разрезать транскрипт на смысловые чанки."""
    turns = doc.turns
    if not turns:
        return []

    # Заранее считаем оценку каждой границы: окно ±3 реплики.
    scores: list[float] = [0.0] * len(turns)
    for i in range(1, len(turns)):
        w_prev = " ".join(t.text for t in turns[max(0, i - 3) : i])
        w_cur = " ".join(t.text for t in turns[i : i + 3])
        scores[i] = _boundary_score(turns[i - 1], turns[i], w_prev, w_cur)

    chunks: list[Chunk] = []
    start_i = 0

    while start_i < len(turns):
        size = 0
        end_i = start_i
        # Набираем реплики, пока помещаемся в max_chars.
        while end_i < len(turns) and size + len(turns[end_i].text) <= max_chars:
            size += len(turns[end_i].text) + 1
            end_i += 1
        if end_i == start_i:  # одна реплика длиннее лимита — берём как есть
            end_i = start_i + 1

        # Если это не последний чанк — ищем лучшую границу в хвостовой трети,
        # чтобы не разрезать посреди обсуждения одной функции.
        if end_i < len(turns):
            search_from = max(start_i + 1, start_i + int((end_i - start_i) * 0.6))
            best_i, best_score = end_i, -1.0
            for i in range(search_from, end_i + 1):
                if i >= len(scores):
                    break
                # Не режем, если получится совсем короткий чанк.
                if sum(len(t.text) for t in turns[start_i:i]) < min_chars:
                    continue
                if scores[i] > best_score:
                    best_i, best_score = i, scores[i]
            if best_score > 0.0:
                end_i = best_i

        body = turns[start_i:end_i]
        seg_ids = [sid for t in body for sid in t.segment_ids]
        overlap = ""
        if chunks:
            prev_text = chunks[-1].text
            overlap = prev_text[-overlap_chars:] if len(prev_text) > overlap_chars else prev_text

        chunks.append(
            Chunk(
                index=len(chunks),
                text=render_turns(body),
                turns=body,
                segment_ids=seg_ids,
                start=body[0].start,
                end=body[-1].end,
                overlap_text=overlap,
            )
        )
        start_i = end_i

    return chunks


def render_turns(turns: list[Turn]) -> str:
    """
    Собрать реплики в текст для промпта.

    Формат `[#12 | 03:41 | Заказчик] текст` даёт модели три вещи сразу:
    номер сегмента для ссылки, таймкод и роль говорящего.
    """
    lines: list[str] = []
    role_label = {
        "customer": "Заказчик",
        "specialist": "Специалист",
        "unknown": "Участник",
    }
    for t in turns:
        mm, ss = divmod(int(t.start), 60)
        first_seg = t.segment_ids[0] if t.segment_ids else "?"
        who = role_label.get(t.role.value, "Участник")
        if t.speaker and t.speaker != "SPEAKER":
            who = f"{who} ({t.speaker})"
        lines.append(f"[#{first_seg} | {mm:02d}:{ss:02d} | {who}] {t.text}")
    return "\n".join(lines)
