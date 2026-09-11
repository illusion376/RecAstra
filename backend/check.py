"""
Проверка «глазами»: прогоняет разговор через конвейер и печатает результат
так, как его увидит пользователь продукта.

Автотесты (smoke_test.py) отвечают на вопрос «не сломалось ли». Этот скрипт
отвечает на другой: «а осмысленное ли оно». Их надо смотреть вместе —
зелёные тесты при бессмысленных требованиях означают, что сломано молча.

    python check.py                          # свой транскрипт из samples/
    python check.py --file мой_разговор.json # свой файл
    python check.py --url http://localhost:8000   # против запущенного сервера
                                             # (токен — в переменной API_TOKEN)
    python check.py --show-source            # показать цитаты и таймкоды

Кириллица печатается корректно: скрипт сам переключает кодировку консоли,
поэтому Windows-терминал не портит вывод.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Служебные логи приложения здесь только мешают: скрипт про результат,
# а не про ход выполнения. Ошибки при этом видны.
logging.basicConfig(level=logging.WARNING)
for noisy in ("app", "app.core.pipeline", "app.api.routes", "httpx"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

# Windows-консоль по умолчанию не в UTF-8 — иначе весь вывод будет нечитаем.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from app.schemas import ITEM_TYPE_LABELS  # noqa: E402

ROOT = Path(__file__).parent
DEFAULT_SAMPLE = ROOT / "samples" / "transcript_sample.json"

BAR = "─" * 78


def mmss(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


class LocalClient:
    """Гоняет приложение в этом же процессе — сервер поднимать не нужно."""

    def __init__(self) -> None:
        # Внутри процесса вход не нужен: проверяем анализ, а не авторизацию.
        os.environ.setdefault("AUTH_REQUIRED", "false")
        os.environ.setdefault("DATABASE_PATH", ":memory:")  # прогон не оставляет следов в базе
        from fastapi.testclient import TestClient

        from app.main import app

        self._ctx = TestClient(app)
        self.client = self._ctx.__enter__()

    def get(self, path: str):
        return self.client.get(path).json()

    def post(self, path: str, payload: dict):
        r = self.client.post(path, json=payload)
        return r.status_code, r.json()

    def close(self) -> None:
        self._ctx.__exit__(None, None, None)


class RemoteClient:
    """Ходит по HTTP в уже запущенный сервер."""

    def __init__(self, base: str) -> None:
        import httpx

        self.base = base.rstrip("/")
        # Токен из POST /auth/login: API_TOKEN=... python check.py --url ...
        token = os.environ.get("API_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(timeout=120.0, headers=headers)

    def get(self, path: str):
        return self.client.get(self.base + path).json()

    def post(self, path: str, payload: dict):
        r = self.client.post(self.base + path, json=payload)
        return r.status_code, r.json()

    def close(self) -> None:
        self.client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка работоспособности анализа")
    parser.add_argument("--file", type=Path, default=DEFAULT_SAMPLE, help="JSON с транскрипцией")
    parser.add_argument("--url", default=None, help="Адрес запущенного сервера")
    parser.add_argument("--show-source", action="store_true", help="Показывать цитаты и таймкоды")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"Файл не найден: {args.file}")
        return 1

    payload = json.loads(args.file.read_text(encoding="utf-8"))
    client = RemoteClient(args.url) if args.url else LocalClient()

    try:
        print(BAR)
        health = client.get("/api/v1/health")
        where = args.url or "внутри процесса, без сервера"
        print(f"Сервис:    {where}")
        print(f"Провайдер: {health['provider']} / {health['model']}")
        if health["provider"] == "mock":
            print(
                "           ВНИМАНИЕ: работает извлечение по правилам, без языковой\n"
                "           модели. Формулировки будут грубее, чем на YandexGPT —\n"
                "           это ожидаемо. Для честной проверки качества укажите\n"
                "           LLM_PROVIDER=yandex_gpt в .env"
            )
        print(f"Транскрипт: {args.file.name}, сегментов: {len(payload.get('segments', []))}")
        print(BAR)

        started = time.perf_counter()
        code, created = client.post("/api/v1/analysis", payload)
        if code != 202:
            print(f"Запуск анализа не удался: HTTP {code}\n{json.dumps(created, ensure_ascii=False)[:800]}")
            return 1

        analysis_id = created["analysis_id"]
        print(f"Анализ запущен: {analysis_id}")

        state: dict = {}
        last_stage = ""
        deadline = time.time() + 900
        while time.time() < deadline:
            state = client.get(f"/api/v1/analysis/{analysis_id}")
            stage = state["progress"]["stage"]
            if stage and stage != last_stage:
                print(f"  {state['progress']['percent']:3d}%  {stage}")
                last_stage = stage
            if state["status"] in ("done", "failed"):
                break
            time.sleep(0.4)

        if state.get("status") != "done":
            print(f"\nАнализ не завершился: {state.get('status')}")
            print(f"Причина: {state.get('error')}")
            return 1

        stats = state["stats"]
        print(BAR)
        print(
            f"Готово за {stats['elapsed_sec']}с "
            f"(суммарно {round(time.perf_counter() - started, 1)}с). "
            f"Вызовов модели: {stats['llm_calls']}, чанков: {stats['chunks']}"
        )
        if stats.get("tokens_in") or stats.get("tokens_out"):
            print(f"Токенов: вход {stats['tokens_in']}, выход {stats['tokens_out']}")

        result = client.get(f"/api/v1/analysis/{analysis_id}/result")
        items = result["items"]
        grouped = result["grouped"]

        print(BAR)
        for type_key, label in ITEM_TYPE_LABELS.items():
            block = grouped.get(type_key, [])
            if not block:
                continue
            print(f"\n{label.upper()} — {len(block)}")
            for item in block:
                flags = []
                src = item.get("source") or {}
                if src and not src.get("verified"):
                    flags.append("ЦИТАТА НЕ НАЙДЕНА")
                if item["confidence"] < 0.6:
                    flags.append(f"уверенность {item['confidence']}")
                if item.get("priority"):
                    flags.append(item["priority"])
                mark = f"   [{', '.join(flags)}]" if flags else ""

                print(f"  • {item['text']}{mark}")
                if item.get("role"):
                    print(f"      роль: {item['role']}")
                if item.get("user_story"):
                    print(f"      story: {item['user_story']}")
                if args.show_source and src.get("quote"):
                    print(f"      источник [{mmss(src.get('start'))}]: «{src['quote']}»")

        if result["analysis"].get("contradictions"):
            print(f"\nНАЙДЕННЫЕ ПРОТИВОРЕЧИЯ — {len(result['analysis']['contradictions'])}")
            by_id = {i["id"]: i for i in items}
            for c in result["analysis"]["contradictions"]:
                a, b = by_id.get(c["item_id_a"]), by_id.get(c["item_id_b"])
                if a and b:
                    print(f"  • {a['title']}  ↔  {b['title']}")
                    print(f"      {c['explanation']}")

        # Итог: на что смотреть, чтобы понять, что перед вами.
        print("\n" + BAR)
        unverified = stats["unverified_items"]
        print(f"Всего элементов: {stats['items_total']}")
        print(f"С подтверждённой цитатой: {stats['items_total'] - unverified}")
        if unverified:
            print(
                f"Без подтверждённой цитаты: {unverified} — эти формулировки модель,\n"
                "  скорее всего, придумала. В интерфейсе они помечаются отдельно."
            )
        empty_types = [
            ITEM_TYPE_LABELS[t] for t in ITEM_TYPE_LABELS if not grouped.get(t)
        ]
        if empty_types:
            print(f"Пустые категории: {', '.join(empty_types)}")
        print(BAR)
        print(
            "Что проверить глазами:\n"
            "  1. Требования сформулированы как требования, а не как куски речи?\n"
            "  2. Ничего важного из разговора не пропало?\n"
            "  3. Нет дублей одного и того же разными словами?\n"
            "  4. Типы расставлены верно (ограничение не уехало в требования)?\n"
            "  5. Запустите с --show-source: цитата действительно обосновывает пункт?"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
