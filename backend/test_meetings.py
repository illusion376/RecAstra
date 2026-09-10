"""
Проверка адаптера /meetings — соответствие контракту из API.md.

Сверяется именно то, что описано в контракте: обязательные поля, названия
контейнеров, форма расшифровки, замена анализа целиком, поведение Range
при раздаче аудио.

Языковая модель не задействована: провайдер принудительно mock, чтобы
прогон был бесплатным, быстрым и не зависел от сети.

    python test_meetings.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["LLM_PROVIDER"] = "mock"
os.environ["MEDIA_DIR"] = str(Path(__file__).parent / "media_test")

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ok = 0
fail = 0

REQUIRED_MEETING = {"id", "title", "date", "filename", "duration", "status", "transcript"}
REQUIRED_CARD = {"id", "kind", "title", "description", "source"}
KNOWN_KINDS = {"functional", "scenarios", "constraints", "conditions", "questions", "agreements", "roles"}


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ OK ] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def wait_ready(client: TestClient, mid: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/meetings/{mid}").json()
        if body["status"] in ("ready", "failed"):
            return body
        time.sleep(0.2)
    return body


def main() -> int:
    sample = json.loads(
        (Path(__file__).parent / "samples" / "transcript_sample.json").read_text(encoding="utf-8")
    )

    with TestClient(app) as client:
        print("\n=== 1. Пустой список ===")
        r = client.get("/meetings")
        check("GET /meetings отвечает", r.status_code == 200, f"HTTP {r.status_code}")
        check("при отсутствии встреч возвращается []", r.json() == [], str(r.json())[:60])

        print("\n=== 2. Создание встречи из расшифровки ===")
        r = client.post(
            "/meetings/from-transcript",
            json={
                "title": "Сервис онлайн-записи",
                "filename": "meeting.mp3",
                "stt_response": {"segments": sample["segments"]},
            },
        )
        check("создание принято с 202", r.status_code == 202, f"HTTP {r.status_code}")
        created = r.json()
        mid = created["id"]

        check(
            "в ответе все обязательные поля",
            REQUIRED_MEETING <= set(created),
            f"нет: {REQUIRED_MEETING - set(created)}",
        )
        check("статус processing", created["status"] == "processing", created["status"])
        # Контракт требует списки, а не null. Расшифровка при этом может быть
        # уже заполнена: она пришла в запросе, и фронту полезно показать текст,
        # пока идёт анализ. А вот анализа, пока статус processing, быть не должно.
        check("transcript — список, не null", isinstance(created["transcript"], list))
        check("анализ пуст, пока идёт обработка", created.get("analysis") == [],
              str(created.get("analysis"))[:60])
        check("date в формате ISO с часовым поясом",
              "T" in created["date"] and ("+" in created["date"] or created["date"].endswith("Z")),
              created["date"])
        check("audio_url отсутствует, а не равен null",
              "audio_url" not in created, str(created.get("audio_url")))

        print("\n=== 3. Готовая встреча ===")
        meeting = wait_ready(client, mid)
        check("встреча дошла до ready", meeting["status"] == "ready", str(meeting.get("error"))[:80])
        check("duration заполнена", meeting["duration"] > 0, str(meeting["duration"]))
        check("расшифровка не пустая", len(meeting["transcript"]) > 0, f"{len(meeting['transcript'])} реплик")
        check("карточки анализа есть", len(meeting["analysis"]) > 0, f"{len(meeting['analysis'])} карточек")

        line = meeting["transcript"][0]
        check("реплика: все четыре поля", {"id", "speaker", "start", "text"} <= set(line), str(line)[:80])
        check("реплика: id строкой", isinstance(line["id"], str), repr(line["id"]))
        check("реплика: start числом, не «12:34»", isinstance(line["start"], (int, float)), repr(line["start"]))
        check("реплика: speaker не пустой", bool(line["speaker"]), repr(line["speaker"]))
        ids = [l["id"] for l in meeting["transcript"]]
        check("реплика: id уникальны", len(ids) == len(set(ids)))

        cards = meeting["analysis"]
        check("карточка: обязательные поля у всех",
              all(REQUIRED_CARD <= set(c) for c in cards))
        check("карточка: kind только из контракта",
              all(c["kind"] in KNOWN_KINDS for c in cards),
              str(sorted({c["kind"] for c in cards})))
        check("карточка: id уникальны во всём массиве",
              len({c["id"] for c in cards}) == len(cards))
        check("карточка: source — число или null",
              all(c["source"] is None or isinstance(c["source"], (int, float)) for c in cards))
        check("карточка: title непустой", all(c["title"] for c in cards))
        check("карточка: role не приходит как null",
              all(c.get("role") is None or isinstance(c["role"], str) for c in cards)
              and all("role" not in c or c["role"] is not None for c in cards))

        with_quote = [c for c in cards if c.get("quote")]
        check("карточки несут цитату сверх контракта", len(with_quote) > 0,
              f"{len(with_quote)}/{len(cards)}")
        check("карточки несут отметку verified",
              any("verified" in c for c in cards))

        kinds = {c["kind"] for c in cards}
        print(f"        контейнеры: {', '.join(sorted(kinds))}")

        print("\n=== 4. Список содержит полные объекты ===")
        listing = client.get("/meetings").json()
        check("во встрече из списка есть расшифровка",
              len(listing) == 1 and len(listing[0]["transcript"]) > 0,
              "урезанного {id,title} фронту недостаточно")
        check("во встрече из списка есть анализ", len(listing[0]["analysis"]) > 0)

        print("\n=== 5. Сохранение правок целиком ===")
        edited = [dict(c) for c in cards[:3]]
        edited[0]["description"] = "Обновлённое описание требования."
        edited[1]["needs_clarification"] = True

        r = client.put(f"/meetings/{mid}/analysis", json={"analysis": edited})
        check("PUT отвечает 200", r.status_code == 200, f"HTTP {r.status_code}")
        after = r.json()
        check("осталось ровно то, что прислали", len(after["analysis"]) == 3,
              f"{len(after['analysis'])} карточек")
        check("правка описания сохранена",
              after["analysis"][0]["description"] == "Обновлённое описание требования."
              or any(c["description"] == "Обновлённое описание требования." for c in after["analysis"]))
        check("отметка «требует уточнения» сохранена",
              any(c.get("needs_clarification") for c in after["analysis"]))
        check("расшифровка не пострадала", len(after["transcript"]) == len(meeting["transcript"]))
        check("возвращается полный Meeting", REQUIRED_MEETING <= set(after))

        r = client.put(f"/meetings/{mid}/analysis", json={"analysis": []})
        check("пустой массив очищает анализ", r.json()["analysis"] == [])

        dup = [dict(cards[0]), dict(cards[0])]
        r = client.put(f"/meetings/{mid}/analysis", json={"analysis": dup})
        check("повторяющиеся id отвергаются", r.status_code == 422, f"HTTP {r.status_code}")

        print("\n=== 6. Загрузка файла ===")
        r = client.post("/meetings", files={"file": ("test.txt", b"not audio", "text/plain")},
                        data={"title": "Плохой формат"})
        check("чужой формат отвергается", r.status_code == 415, f"HTTP {r.status_code}")

        fake_audio = b"ID3" + b"\x00" * 60000
        r = client.post("/meetings", files={"file": ("meeting.mp3", fake_audio, "audio/mpeg")},
                        data={"title": "Запись со звуком"})
        check("аудио принято с 202", r.status_code == 202, f"HTTP {r.status_code}")
        audio_meeting = r.json()
        aid = audio_meeting["id"]
        check("audio_url появился сразу", "audio_url" in audio_meeting,
              audio_meeting.get("audio_url", "нет"))

        print("\n=== 6б. Ручка /transcribe (перенесена из transcriber.py) ===")
        r = client.post("/transcribe", files={"file": ("x.txt", b"nope", "text/plain")})
        check("чужой формат отвергается", r.status_code == 415, f"HTTP {r.status_code}")

        r = client.post("/transcribe", files={"file": ("a.mp3", fake_audio, "audio/mpeg")})
        # Распознавание не настроено — честный 503, а не пустой список сегментов.
        check("без настроенного распознавания — 503", r.status_code == 503, f"HTTP {r.status_code}")
        check("в ответе объяснено, чего не хватает",
              "NEXARA_API_KEY" in r.json().get("detail", ""), r.json().get("detail", "")[:60])

        leftovers = list((Path(__file__).parent / "media_test").glob("tmp_*"))
        check("временный файл убран за собой", not leftovers, str(leftovers)[:60])

        print("\n=== 7. Раздача аудио и перемотка ===")
        url = audio_meeting["audio_url"]
        r = client.get(url)
        check("файл отдаётся целиком", r.status_code == 200 and len(r.content) == len(fake_audio))
        check("объявлена поддержка перемотки", r.headers.get("accept-ranges") == "bytes")
        check("Content-Type звуковой", r.headers.get("content-type") == "audio/mpeg",
              str(r.headers.get("content-type")))

        r = client.get(url, headers={"Range": "bytes=100-199"})
        check("Range отдаёт 206", r.status_code == 206, f"HTTP {r.status_code}")
        check("отдан запрошенный кусок", len(r.content) == 100, f"{len(r.content)} байт")
        check("Content-Range проставлен",
              r.headers.get("content-range") == f"bytes 100-199/{len(fake_audio)}",
              str(r.headers.get("content-range")))

        r = client.get(url, headers={"Range": "bytes=-100"})
        check("хвостовой Range работает", r.status_code == 206 and len(r.content) == 100)

        r = client.get(url, headers={"Range": f"bytes={len(fake_audio) + 10}-"})
        check("Range за пределами файла даёт 416", r.status_code == 416, f"HTTP {r.status_code}")

        r = client.get("/media/../.env")
        check("выход за пределы папки не проходит", r.status_code == 404, f"HTTP {r.status_code}")

        print("\n=== 8. Ошибки ===")
        check("неизвестная встреча — 404", client.get("/meetings/nope").status_code == 404)
        check("PUT в неизвестную встречу — 404",
              client.put("/meetings/nope/analysis", json={"analysis": []}).status_code == 404)

        failed = wait_ready(client, aid, timeout=30)
        check("без распознавания встреча падает с внятной причиной",
              failed["status"] == "failed" and "распознавание" in (failed.get("error") or "").lower(),
              (failed.get("error") or "")[:70])
        check("у упавшей встречи списки пустые, но поля на месте",
              failed["transcript"] == [] and failed.get("analysis") == []
              and REQUIRED_MEETING <= set(failed))

    print(f"\n{'=' * 60}\nПройдено: {ok}, провалено: {fail}\n{'=' * 60}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
