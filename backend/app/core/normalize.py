"""
Этап 1 конвейера — нормализация транскрипции.

Whisper отдаёт сырой поток: обрывки фраз, слова-паразиты, зацикленные повторы
на тишине и характерные галлюцинации («Субтитры сделал DimaTorzok» и подобные).
Если скормить это модели как есть, качество извлечения падает, а токены
тратятся впустую.

Результат этапа — TranscriptDoc: очищенные сегменты, склеенные реплики
и склеенный текст с картой «смещение в тексте → id сегмента». Карта нужна,
чтобы потом привязать цитату к таймкоду (пункт 5 кейса).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Optional

from app.schemas import SpeakerRole, TranscriptSegment

# Фразы, которые Whisper дорисовывает на тишине и музыке. Встречаются
# в русских моделях устойчиво и не несут смысла.
HALLUCINATION_PATTERNS = [
    r"субтитры\s+сделал\w*\s+.*",
    r"субтитры\s+создавал\w*\s+.*",
    r"редактор\s+субтитров\s+.*",
    r"продолжение\s+следует\s*\.{0,3}",
    r"спасибо\s+за\s+просмотр\s*[!.]*",
    r"подписывайтесь\s+на\s+канал.*",
    r"ставьте\s+лайк.*",
    r"^\s*\[?\s*музыка\s*\]?\s*$",
    r"^\s*\[?\s*аплодисменты\s*\]?\s*$",
    r"^\s*\(\s*тишина\s*\)\s*$",
]
_HALLUCINATION_RE = re.compile("|".join(HALLUCINATION_PATTERNS), re.IGNORECASE)

# Слова-паразиты. Убираем только когда они стоят отдельным «словом-вставкой»,
# не трогая случаи, где они часть смысла.
FILLER_WORDS = {
    "э",
    "ээ",
    "эээ",
    "а-а",
    "ааа",
    "мм",
    "ммм",
    "эм",
    "ну",
    "вот",
    "как бы",
    "типа",
    "короче",
    "значит",
    "в общем",
    "так сказать",
    "это самое",
}
_FILLER_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(w) for w in sorted(FILLER_WORDS, key=len, reverse=True)) + r")(?![\w-])[\s,]*",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")


def clean_text(raw: str, drop_fillers: bool = True) -> str:
    """Очистить текст одного сегмента. Пустая строка = сегмент выбрасываем."""
    text = _WS_RE.sub(" ", raw or "").strip()
    if not text:
        return ""

    if _HALLUCINATION_RE.fullmatch(text.strip(" .!?")) or _HALLUCINATION_RE.match(text):
        cleaned = _HALLUCINATION_RE.sub("", text).strip()
        if len(cleaned) < 3:
            return ""
        text = cleaned

    if drop_fillers:
        stripped = _FILLER_RE.sub("", text).strip(" ,")
        # Если после чистки почти ничего не осталось — значит сегмент состоял
        # из одних паразитов, выбрасываем его целиком.
        if len(stripped) < 3:
            return ""
        text = _WS_RE.sub(" ", stripped)

    return text.strip(" ,")


def _is_loop(prev: str, cur: str) -> bool:
    """Whisper иногда зацикливает одну фразу подряд десятки раз."""
    return bool(prev) and prev.casefold() == cur.casefold()


@dataclass
class Turn:
    """Реплика — склейка соседних сегментов одного говорящего."""

    index: int
    speaker: str
    role: SpeakerRole
    text: str
    start: float
    end: float
    segment_ids: list[int] = field(default_factory=list)


@dataclass
class TranscriptDoc:
    """Нормализованная транскрипция + всё, что нужно для трассировки."""

    segments: list[TranscriptSegment]
    turns: list[Turn]
    full_text: str
    # Параллельные массивы: для каждого сегмента — его смещение в full_text.
    _offsets: list[int] = field(default_factory=list)
    _segment_ids: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def segment_by_id(self, seg_id: int) -> Optional[TranscriptSegment]:
        for s in self.segments:
            if s.id == seg_id:
                return s
        return None

    def segment_id_at(self, char_pos: int) -> Optional[int]:
        """По смещению в full_text вернуть id сегмента — основа трассировки."""
        if not self._offsets:
            return None
        idx = bisect_right(self._offsets, char_pos) - 1
        if idx < 0:
            idx = 0
        if idx >= len(self._segment_ids):
            return None
        return self._segment_ids[idx]

    def segments_in_range(self, char_start: int, char_end: int) -> list[int]:
        """Все сегменты, попадающие в диапазон символов."""
        if not self._offsets:
            return []
        out: list[int] = []
        lo = max(bisect_right(self._offsets, char_start) - 1, 0)
        for i in range(lo, len(self._offsets)):
            if self._offsets[i] > char_end:
                break
            out.append(self._segment_ids[i])
        return out

    def time_range(self, segment_ids: list[int]) -> tuple[Optional[float], Optional[float]]:
        if not segment_ids:
            return None, None
        wanted = set(segment_ids)
        found = [s for s in self.segments if s.id in wanted]
        if not found:
            return None, None
        return min(s.start for s in found), max(s.end for s in found)


def _guess_role(speaker: Optional[str], text: str) -> SpeakerRole:
    """
    Диаризации может не быть. Тогда пытаемся угадать роль по лексике:
    заказчик формулирует «хочу / нам нужно», специалист — «сделаем / реализуем».
    Это подсказка для промпта, а не факт: в результат идёт как поле actor.
    """
    if speaker:
        low = speaker.casefold()
        if any(k in low for k in ("заказчик", "customer", "client", "клиент")):
            return SpeakerRole.CUSTOMER
        if any(k in low for k in ("специалист", "spec", "dev", "разработ", "тех")):
            return SpeakerRole.SPECIALIST

    low = text.casefold()
    customer_markers = ("мы хотим", "нам нужно", "хотелось бы", "нам важно", "у нас есть задача")
    specialist_markers = ("мы сделаем", "реализуем", "с технической точки", "можем сделать", "по срокам")
    if any(m in low for m in customer_markers):
        return SpeakerRole.CUSTOMER
    if any(m in low for m in specialist_markers):
        return SpeakerRole.SPECIALIST
    return SpeakerRole.UNKNOWN


def _join_texts(left: str, right: str) -> str:
    """
    Склеить два куска речи.

    Распознавание сплошь и рядом не ставит точку в конце сегмента. Если просто
    соединить такие куски пробелом, две разные мысли превратятся в одно
    предложение — и дальше по конвейеру уедут как одно требование вместо двух.
    Поэтому недостающую границу восстанавливаем сами.
    """
    left = left.rstrip()
    if not left:
        return right.strip()
    if not left.endswith((".", "!", "?", ",", ":", ";", "—", "-")):
        left += "."
    return f"{left} {right.strip()}".strip()


def build_turns(
    segments: list[TranscriptSegment], max_gap: float = 2.0
) -> list[Turn]:
    """
    Склеить сегменты в реплики.

    Границей реплики считаем смену говорящего или паузу. Пауза важна и при
    диаризации: два куска одного человека с разницей в три минуты — это две
    разные реплики о разном, склеивать их нельзя. Но внутри одной реплики
    человек делает паузы длиннее, чем длится зазор между сегментами Whisper,
    поэтому при известном говорящем допуск шире.
    """
    turns: list[Turn] = []
    for seg in segments:
        speaker = seg.speaker or "SPEAKER"
        same_speaker = bool(turns) and turns[-1].speaker == speaker
        allowed_gap = max_gap * 3 if seg.speaker is not None else max_gap
        small_gap = bool(turns) and (seg.start - turns[-1].end) <= allowed_gap

        if turns and same_speaker and small_gap:
            t = turns[-1]
            t.text = _join_texts(t.text, seg.text)
            t.end = seg.end
            t.segment_ids.append(seg.id)
        else:
            turns.append(
                Turn(
                    index=len(turns),
                    speaker=speaker,
                    role=SpeakerRole.UNKNOWN,
                    text=seg.text,
                    start=seg.start,
                    end=seg.end,
                    segment_ids=[seg.id],
                )
            )

    for t in turns:
        t.role = _guess_role(t.speaker if t.speaker != "SPEAKER" else None, t.text)
    return turns


def normalize(
    raw_segments: list[TranscriptSegment], drop_fillers: bool = True
) -> TranscriptDoc:
    """Главная функция этапа."""
    cleaned: list[TranscriptSegment] = []
    prev_text = ""

    for seg in raw_segments:
        text = clean_text(seg.text, drop_fillers=drop_fillers)
        if not text or _is_loop(prev_text, text):
            continue
        cleaned.append(
            TranscriptSegment(
                id=seg.id,
                start=seg.start,
                end=max(seg.end, seg.start),
                text=text,
                speaker=seg.speaker,
            )
        )
        prev_text = text

    # Склеиваем текст и параллельно запоминаем, где начинается каждый сегмент.
    parts: list[str] = []
    offsets: list[int] = []
    seg_ids: list[int] = []
    cursor = 0
    for seg in cleaned:
        offsets.append(cursor)
        seg_ids.append(seg.id)
        parts.append(seg.text)
        cursor += len(seg.text) + 1  # +1 на пробел-разделитель

    doc = TranscriptDoc(
        segments=cleaned,
        turns=build_turns(cleaned),
        full_text=" ".join(parts),
        _offsets=offsets,
        _segment_ids=seg_ids,
    )
    return doc
