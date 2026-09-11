"""
Сквозная проверка основного сценария без поднятия сервера.

Проходит ровно тот путь, которым пойдёт фронт:
запуск анализа → опрос статуса → результат → правки → источник → поиск.

    python smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Тесты всегда идут на mock, что бы ни стояло в .env. Иначе прогон начинает
# ходить в платную модель, длится минуты вместо секунды и зависит от сети —
# а проверять здесь надо логику приложения, а не качество нейросети.
# Переменные окружения приоритетнее .env, поэтому хватает установки до импорта.
os.environ["LLM_PROVIDER"] = "mock"
os.environ["AUTH_REQUIRED"] = "false"  # авторизацию проверяет test_auth.py

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from app.main import app  # noqa: E402

SAMPLE = Path(__file__).parent / "samples" / "transcript_sample.json"

ok_count = 0
fail_count = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  [ OK ] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))

    with TestClient(app) as client:
        print("\n=== 0. Служебные эндпоинты ===")
        r = client.get("/api/v1/health")
        check("health отвечает", r.status_code == 200, str(r.json()))

        r = client.get("/api/v1/meta/enums")
        check("справочники отдаются", len(r.json()["item_types"]) == 7)

        print("\n=== 1. Запуск анализа ===")
        r = client.post("/api/v1/analysis", json=payload)
        check("анализ принят", r.status_code == 202, f"HTTP {r.status_code}")
        if r.status_code != 202:
            print(r.text[:2000])
            return 1
        analysis_id = r.json()["analysis_id"]
        print(f"        analysis_id = {analysis_id}")

        print("\n=== 2. Опрос статуса ===")
        deadline = time.time() + 60
        state = {}
        while time.time() < deadline:
            state = client.get(f"/api/v1/analysis/{analysis_id}").json()
            if state["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        check("анализ завершился", state.get("status") == "done", str(state.get("error")))
        if state.get("status") != "done":
            return 1

        stats = state["stats"]
        print(
            f"        сегментов: {stats['segments']}, чанков: {stats['chunks']}, "
            f"вызовов LLM: {stats['llm_calls']}, время: {stats['elapsed_sec']}с, "
            f"провайдер: {stats['provider']}/{stats['model']}"
        )

        print("\n=== 3. Результат ===")
        r = client.get(f"/api/v1/analysis/{analysis_id}/result")
        check("результат отдаётся", r.status_code == 200)
        result = r.json()
        items = result["items"]
        check("элементы извлечены", len(items) > 0, f"{len(items)} шт.")

        by_type = result["analysis"]["stats"]["items_by_type"]
        print("        по типам:")
        for t, n in by_type.items():
            print(f"          {t:26s} {n}")
        check(
            "заполнено больше одного типа",
            sum(1 for n in by_type.values() if n > 0) >= 2,
            f"непустых типов: {sum(1 for n in by_type.values() if n > 0)}",
        )

        traced = [i for i in items if i["source"] and i["source"]["segment_ids"]]
        check("у элементов есть привязка к сегментам", len(traced) > 0, f"{len(traced)}/{len(items)}")

        verified = [i for i in items if i["source"] and i["source"]["verified"]]
        check("цитаты подтверждены в транскрипте", len(verified) > 0, f"{len(verified)}/{len(items)}")

        timed = [i for i in items if i["source"] and i["source"]["start"] is not None]
        check("у элементов есть таймкоды", len(timed) > 0, f"{len(timed)}/{len(items)}")

        print("\n        Примеры извлечённого:")
        for item in items[:5]:
            src = item["source"] or {}
            ts = f"{int(src.get('start') or 0)//60:02d}:{int(src.get('start') or 0)%60:02d}"
            print(f"          [{item['type']}] {item['title'][:70]}")
            print(f"            цитата ({ts}, verified={src.get('verified')}): «{(src.get('quote') or '')[:80]}…»")

        print("\n=== 4. Правка результата (пункт 4 кейса) ===")
        item_id = items[0]["id"]

        r = client.patch(
            f"/api/v1/analysis/{analysis_id}/items/{item_id}",
            json={"text": "Отредактированная формулировка требования.", "priority": "must"},
        )
        check("редактирование работает", r.status_code == 200 and r.json()["origin"] == "edited")

        r = client.post(
            f"/api/v1/analysis/{analysis_id}/items/{item_id}/flag",
            json={"needs_clarification": True, "comment": "проверить у юристов"},
        )
        check("пометка «требует уточнения»", r.json()["status"] == "needs_clarification")

        r = client.post(
            f"/api/v1/analysis/{analysis_id}/items",
            json={
                "type": "functional_requirement",
                "title": "Добавлено вручную",
                "text": "Система должна поддерживать массовую выгрузку договоров.",
                "priority": "should",
            },
        )
        check("добавление вручную", r.status_code == 201 and r.json()["origin"] == "manual")
        manual_id = r.json()["id"]

        r = client.delete(f"/api/v1/analysis/{analysis_id}/items/{manual_id}")
        check("мягкое удаление", r.status_code == 204)
        r = client.get(f"/api/v1/analysis/{analysis_id}/items", params={"status": "rejected"})
        check("удалённый попал в rejected", r.json()["total"] == 1)

        r = client.get(
            f"/api/v1/analysis/{analysis_id}/items",
            params={"type": "functional_requirement"},
        )
        check("фильтр по типу работает", r.status_code == 200, f"{r.json()['total']} шт.")

        print("\n=== 5. Связь с исходным разговором (пункт 5 кейса) ===")
        traceable = next((i for i in items if i["source"] and i["source"]["segment_ids"]), None)
        if traceable:
            r = client.get(f"/api/v1/analysis/{analysis_id}/items/{traceable['id']}/source")
            body = r.json()
            check("фрагмент разговора отдаётся", r.status_code == 200 and len(body["segments"]) > 0)
            check("контекст вокруг фрагмента есть", len(body["context_before"]) + len(body["context_after"]) > 0)
            if body["segments"]:
                print(f"        фрагмент: «{body['segments'][0]['text'][:90]}…»")

        print("\n=== 6. Поиск по транскрипции ===")
        r = client.get(f"/api/v1/analysis/{analysis_id}/search", params={"q": "договор"})
        check("поиск находит совпадения", r.json()["total"] > 0, f"{r.json()['total']} сегментов")
        check("подсветка проставлена", "<mark>" in r.json()["hits"][0]["highlight"])

        print("\n=== 7. Обработка ошибок ===")
        check("404 на неизвестный анализ", client.get("/api/v1/analysis/an_nope").status_code == 404)
        check("404 на неизвестный элемент", client.get(f"/api/v1/analysis/{analysis_id}/items/item_nope/source").status_code == 404)
        check("422 на пустые сегменты", client.post("/api/v1/analysis", json={"segments": []}).status_code == 422)

        print("\n=== 8. Вход из Yandex SpeechKit ===")
        # Whisper и SpeechKit отдают разные форматы; проверяем, что второй
        # проходит весь путь до готовых требований без ручной конвертации.
        speechkit_payload = {
            "stt_response": {
                "result": [
                    {
                        "final": {
                            "alternatives": [
                                {
                                    "text": "Нам нужно чтобы согласование договоров шло в одной системе, "
                                    "а не по почте, потому что мы теряем версии",
                                    "startTimeMs": "15200",
                                    "endTimeMs": "31800",
                                }
                            ],
                            "speakerTag": "1",
                        }
                    },
                    {
                        "final": {
                            "alternatives": [
                                {
                                    "text": "Договор нельзя удалить, только отменить, у нас срок хранения семь лет",
                                    "startTimeMs": "210000",
                                    "endTimeMs": "224600",
                                }
                            ],
                            "speakerTag": "1",
                        }
                    },
                ]
            },
            "meta": {"title": "Встреча из SpeechKit", "language": "ru"},
        }

        r = client.post("/api/v1/transcripts/from-stt", json=speechkit_payload)
        converted = r.json()
        check("конвертация SpeechKit отрабатывает", r.status_code == 200 and len(converted) == 2)
        check(
            "таймкоды и спикер перенесены",
            converted[0]["start"] == 15.2 and converted[0]["speaker"] == "Говорящий 1",
            f"{converted[0]['start']}s, {converted[0]['speaker']}",
        )

        r = client.post("/api/v1/analysis/from-stt", json=speechkit_payload)
        check("анализ из SpeechKit принят", r.status_code == 202, f"HTTP {r.status_code}")
        sk_id = r.json()["analysis_id"]

        deadline = time.time() + 60
        sk_state = {}
        while time.time() < deadline:
            sk_state = client.get(f"/api/v1/analysis/{sk_id}").json()
            if sk_state["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        check("анализ из SpeechKit завершился", sk_state.get("status") == "done", str(sk_state.get("error")))

        sk_items = client.get(f"/api/v1/analysis/{sk_id}/result").json()["items"]
        check("из записи SpeechKit извлечены требования", len(sk_items) > 0, f"{len(sk_items)} шт.")
        # Два сегмента разнесены на три минуты и оба без точки в конце.
        # Если реплики склеятся, две разные мысли станут одним требованием.
        sk_types = {i["type"] for i in sk_items}
        check(
            "далёкие сегменты не слиплись в одно требование",
            len(sk_items) >= 2 and "constraint" in sk_types,
            f"типы: {sorted(sk_types)}",
        )
        check(
            "цитаты подтверждены и на этом входе",
            any(i["source"] and i["source"]["verified"] for i in sk_items),
        )

        r = client.post(
            "/api/v1/analysis/from-stt", json={"stt_response": {"foo": "bar"}}
        )
        check("неразобранный ответ SpeechKit даёт 422", r.status_code == 422, r.json().get("detail", "")[:60])

        r = client.get("/openapi.json")
        check("OpenAPI-схема генерируется", r.status_code == 200, f"{len(r.json()['paths'])} путей")

    print(f"\n{'=' * 60}\nПройдено: {ok_count}, провалено: {fail_count}\n{'=' * 60}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
