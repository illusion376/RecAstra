"""
Проверка адаптера на реальном ответе Nexara.

Данные взяты дословно из ответа, который прислал коллега: реплики с полем
"time" вида "00:00:29", меткой говорящего и без конца реплики.

    python test_nexara.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Тест запускает анализ через API — без этого он ушёл бы в платную модель,
# указанную в .env, и тратил бы деньги на каждом прогоне.
os.environ["LLM_PROVIDER"] = "mock"
os.environ["AUTH_REQUIRED"] = "false"  # авторизацию проверяет test_auth.py
os.environ["DATABASE_PATH"] = ":memory:"  # не трогать рабочую базу

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.stt import stt_to_segments  # noqa: E402
from app.main import app  # noqa: E402

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


# Ровно то, что пришло от Nexara.
NEXARA = {
    "segments": [
        {"speaker": "speaker_0", "time": "00:00:24", "text": "думала, ты скажешь саморазвитие."},
        {"speaker": "speaker_1", "time": "00:00:29", "text": "Так я уже развелась."},
        {"speaker": "speaker_0", "time": "00:00:31", "text": "Развилась. Развелась, это если бы"},
        {"speaker": "speaker_1", "time": "00:00:34", "text": "у тебя был муж."},
        {
            "speaker": "speaker_0",
            "time": "00:00:36",
            "text": "Хорошо, значит. То есть получать удовольствие, да? Да. От того, что ты делаешь. "
            "Для меня... наверное, важна любовь в большом смысле, в большом понимании, то есть "
            "любовь к себе, к другим, к тому, что делаешь, ну и согласно с тобой тоже получение удовольствия.",
        },
    ]
}


def main() -> int:
    print("\n=== 1. Разбор ответа Nexara ===")

    segs = stt_to_segments(NEXARA)
    check("все реплики разобраны", len(segs) == 5, f"{len(segs)} сегментов")
    check("id проставлены подряд", [s.id for s in segs] == [0, 1, 2, 3, 4])

    check(
        "«00:00:29» переведено в секунды",
        segs[1].start == 29.0,
        f"start={segs[1].start}",
    )
    check(
        "«00:00:36» переведено в секунды",
        segs[4].start == 36.0,
        f"start={segs[4].start}",
    )

    check(
        "speaker_0 стал читаемой меткой",
        segs[0].speaker == "Говорящий 1",
        str(segs[0].speaker),
    )
    check(
        "нумерация говорящих с единицы, а не с нуля",
        "Говорящий 0" not in {s.speaker for s in segs},
        str(sorted({s.speaker for s in segs})),
    )

    print("\n=== 2. Восстановление конца реплик ===")
    # Nexara конец не сообщает. Взять началом следующей реплики нельзя:
    # тогда зазор всегда нулевой и пропадает сигнал о паузах, по которому
    # конвейер режет разговор на темы.
    check("все сегменты имеют положительную длительность", all(s.end > s.start for s in segs))
    check(
        "сегменты не наезжают друг на друга",
        all(segs[i].end <= segs[i + 1].start for i in range(len(segs) - 1)),
        " ".join(f"{s.start}-{round(s.end, 1)}" for s in segs),
    )
    gaps = [round(segs[i + 1].start - segs[i].end, 2) for i in range(len(segs) - 1)]
    check(
        "паузы между репликами сохранились",
        any(g > 0.1 for g in gaps),
        f"зазоры: {gaps}",
    )
    check(
        "длинная реплика получила длинную оценку",
        segs[4].end - segs[4].start > 15,
        f"{round(segs[4].end - segs[4].start, 1)}с на {len(segs[4].text)} символов",
    )

    print("\n=== 3. Через API ===")
    with TestClient(app) as client:
        r = client.post("/api/v1/transcripts/from-stt", json={"stt_response": NEXARA})
        check("ручка конвертации отвечает", r.status_code == 200, f"HTTP {r.status_code}")
        body = r.json()
        check("вернулись все сегменты", len(body) == 5)

        r = client.post(
            "/api/v1/analysis/from-stt",
            json={"stt_response": NEXARA, "meta": {"title": "Тест Nexara"}},
        )
        check("анализ принят", r.status_code == 202, f"HTTP {r.status_code}")

        # Старое имя поля и старая ручка не должны сломаться у тех,
        # кто уже успел на них завязаться.
        r = client.post(
            "/api/v1/analysis/from-speechkit", json={"speechkit_response": NEXARA}
        )
        check("прежняя ручка и прежнее имя поля работают", r.status_code == 202, f"HTTP {r.status_code}")

        r = client.post("/api/v1/transcripts/from-stt", json={"stt_response": {"nonsense": 1}})
        check("мусор отвергается с 422", r.status_code == 422)

    print("\n=== 4. Прочие формы ответа не сломались ===")

    # Первый ответ Nexara, без разбивки.
    segs = stt_to_segments({"text": " Алло, проверка. Как это будет работать? Тест."})
    check("плоский текст разобран", len(segs) == 1 and segs[0].text.startswith("Алло"))

    # Whisper со start/end.
    segs = stt_to_segments(
        {"segments": [{"id": 0, "start": 1.0, "end": 2.5, "text": "тест", "speaker": "Заказчик"}]}
    )
    check(
        "сегменты Whisper проходят как есть",
        len(segs) == 1 and segs[0].end == 2.5 and segs[0].speaker == "Заказчик",
    )

    # SpeechKit v3.
    segs = stt_to_segments(
        {
            "result": [
                {
                    "final": {
                        "alternatives": [
                            {"text": "Только на нашем сервере", "startTimeMs": "480100", "endTimeMs": "484000"}
                        ],
                        "speakerTag": "2",
                    }
                }
            ]
        }
    )
    check("SpeechKit v3 всё ещё разбирается", len(segs) == 1 and segs[0].start == 480.1)

    # SpeechKit v2.
    segs = stt_to_segments(
        {
            "response": {
                "chunks": [
                    {
                        "alternatives": [
                            {
                                "text": "Облако не рассматриваем",
                                "words": [
                                    {"startTime": "497.200s", "endTime": "498.800s", "word": "Облако"}
                                ],
                            }
                        ]
                    }
                ]
            }
        }
    )
    check("SpeechKit v2 всё ещё разбирается", len(segs) == 1 and segs[0].start == 497.2)

    print(f"\n{'=' * 60}\nПройдено: {ok}, провалено: {fail}\n{'=' * 60}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
