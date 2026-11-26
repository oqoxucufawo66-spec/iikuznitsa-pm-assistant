from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import httpx
from pptx import Presentation

ROOT = Path(__file__).resolve().parent.parent
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


def load_cloudru_env() -> dict[str, str]:
    env = os.environ.copy()
    secret_file = Path("/workspace/secrets/cloudru-fm.env")
    if secret_file.exists() and not env.get("LLM_API_KEY") and not env.get("CLOUDRU_FM_API_KEY"):
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env["APP_PORT"] = str(PORT)
    env["LLM_PROVIDER"] = env.get("LLM_PROVIDER", "cloudru")
    env["LLM_MODEL"] = env.get("LLM_MODEL", "GigaChat/GigaChat-3-Pro")
    env["LLM_FALLBACK_MODEL"] = env.get("LLM_FALLBACK_MODEL", "ai-sage/GigaChat3-10B-A1.8B")
    env["EMBED_MODEL"] = env.get("EMBED_MODEL", "ai-sage/Giga-Embeddings-instruct-480M")
    env["HEYGEN_DRY_RUN"] = "true"
    return env


def main() -> int:
    db_file = ROOT / "pm_assistant.db"
    if db_file.exists():
        db_file.unlink()

    env = load_cloudru_env()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    lines_md: list[str] = [
        "# Лог сквозного тестирования (tests-log.md)",
        "",
        "Все тесты выполнены локально на стенде `uvicorn` с демо-датасетом **«ТехноВитрина (ДЕМО-ДАННЫЕ)»**.",
        "",
    ]

    try:
        # Wait for health
        health = None
        for _ in range(40):
            try:
                r = httpx.get(f"{BASE}/health", timeout=3.0)
                if r.status_code == 200:
                    health = r.json()
                    break
            except Exception:
                time.sleep(0.5)
        if not health:
            raise RuntimeError("Server failed to start within timeout")

        lines_md.extend([
            "## 1. Старт и индексация базы знаний (JSON + CSV + MD + PDF)",
            f"- `GET /health`: `{json.dumps(health, ensure_ascii=False)}`",
        ])

        prods = httpx.get(f"{BASE}/api/products", timeout=10.0).json()
        assert prods["count"] == 20, f"Expected 20 demo products, got {prods['count']}"
        lines_md.append(f"- Загружено товаров: **{prods['count']}** (16 JSON + 4 CSV), пометка датасета: `{prods['dataset']}`.")
        lines_md.append("")
        lines_md.append("## 2. Прогон 10 вопросов через `/api/chat`")
        lines_md.append("")

        questions = [
            ("Q1 (Товар из JSON, характеристики)", "Какая мощность всасывания, автономность и гарантия у робота-пылесоса AeroClean L10 Pro?"),
            ("Q2 (Товар из CSV, периферия)", "Расскажи про клавиатуру TactilePro 75 Wireless: какие переключатели, цена и автономность?"),
            ("Q3 (Сравнение двух товаров)", "Сравни ноутбуки NordBook Pro 14 OLED и NordBook Air 15 Office по экрану, памяти и цене."),
            ("Q4 (FAQ: оплата и доставка)", "Можно ли оплатить по СБП или в рассрочку и сколько стоит доставка по Москве?"),
            ("Q5 (PDF-регламент B2B)", "Какие ноутбуки протестированы с Astra Linux и за сколько часов отвечают по приоритетной линии B2B Стандарт+?"),
            ("Q6 (Повторный вопрос — перефраз)", "Какая мощность всасывания и гарантия у AeroClean L10 Pro?"),
            ("Q7 (Точный повтор — проверка кэша LLM)", "Какая мощность всасывания, автономность и гарантия у робота-пылесоса AeroClean L10 Pro?"),
            ("Q8 (Эскалация: товар отсутствует)", "Когда появится в наличии веб-камера StreamEye 4K Desk?"),
            ("Q9 (Эскалация: правило ПМ — тендер/опт)", "Нужна корпоративная скидка на 20 ноутбуков NordBook Air 15 под тендер 44-ФЗ."),
            ("Q10 (Эскалация: нет в каталоге)", "Продаёте ли вы двухдверные холодильники LG с ледогенератором?"),
        ]

        results = []
        with httpx.Client(timeout=40.0) as client:
            for label, q in questions:
                resp = client.post(f"{BASE}/api/chat", json={"question": q})
                assert resp.status_code == 200, f"HTTP {resp.status_code} for {label}"
                d = resp.json()
                results.append((label, q, d))
                srcs = ", ".join(s["source_id"] for s in d.get("sources", [])) or "—"
                steps = " -> ".join(t["step"] for t in d.get("trace", []))
                ans_short = d["answer"].replace("\n", " ")
                if len(ans_short) > 280:
                    ans_short = ans_short[:280] + "…"
                lines_md.extend([
                    f"### {label}",
                    f"- **Вопрос:** {q}",
                    f"- **Intent:** `{d['intent']}` | **Confidence:** `{d['confidence']}` | **Повтор:** `{d['is_repeat']}` (кластер hits={d['repeat_cluster_hits']}) | **Эскалация:** `{d['escalated']}` (#PM-{d['escalation_id']})" if d["escalated"] else f"- **Intent:** `{d['intent']}` | **Confidence:** `{d['confidence']}` | **Повтор:** `{d['is_repeat']}` (кластер hits={d['repeat_cluster_hits']}) | **Эскалация:** `False`",
                    f"- **Модель / токены / цена:** `{d['provider']}/{d['model']}` | `{d['tokens_in']}+{d['tokens_out']}` ток. | `{d['cost_rub']} ₽` | `cache_hit={d['cache_hit']}` | `{d['latency_ms']} мс`",
                    f"- **Источники:** {srcs}",
                    f"- **Шаги агента:** `{steps}`",
                    f"- **Ответ:** {ans_short}",
                    "",
                ])

        # Verify expected behaviours
        assert results[0][2]["escalated"] is False
        assert results[1][2]["escalated"] is False
        assert results[2][2]["intent"] == "compare" and results[2][2]["escalated"] is False
        assert results[3][2]["escalated"] is False
        assert results[4][2]["escalated"] is False
        assert results[5][2]["is_repeat"] is True
        assert results[6][2]["is_repeat"] is True and results[6][2]["cache_hit"] is True
        assert results[7][2]["escalated"] is True
        assert results[8][2]["escalated"] is True
        assert results[9][2]["escalated"] is True

        # 3. PPTX generation
        pptx_res = httpx.post(f"{BASE}/api/generate/pptx", json={"sku": "DEMO-LAP-201"}, timeout=20.0).json()
        pptx_path = ROOT / "artifacts" / pptx_res["filename"]
        assert pptx_path.exists()
        prs = Presentation(str(pptx_path))
        assert len(prs.slides) == 6

        # 4. Video script + HeyGen dry-run
        vid_res = httpx.post(f"{BASE}/api/generate/video", json={"sku": "DEMO-VAC-101"}, timeout=40.0).json()
        assert vid_res["heygen_adapter"]["credits_spent"] == 0
        assert vid_res["heygen_adapter"]["payload"]["meta_demo"]["dry_run"] is True

        # 5. Telegram webhook stub
        tg_res = httpx.post(
            f"{BASE}/api/telegram/webhook",
            json={"message": {"chat": {"id": 12345}, "from": {"username": "sales_mgr"}, "text": "Сколько стоит NetMesh AX3000?"}},
            timeout=30.0,
        ).json()
        assert tg_res["ok"] is True and "NetMesh" in tg_res["text"] or "6490" in tg_res["text"] or "6 490" in tg_res["text"]

        # 6. Stats & Escalations
        stats = httpx.get(f"{BASE}/api/stats", timeout=10.0).json()
        escs = httpx.get(f"{BASE}/api/escalations", timeout=10.0).json()

        lines_md.extend([
            "## 3. Генерация материалов и адаптеры",
            f"- **PPTX (`POST /api/generate/pptx`):** `{pptx_res['filename']}`, слайдов: **{len(prs.slides)}** (проверено чтением через `python-pptx`).",
            f"- **Видеосценарий + HeyGen dry-run (`POST /api/generate/video`):** `{vid_res['files']['script_url']}`, `{vid_res['files']['payload_url']}`, модель `{vid_res['llm_usage']['model']}`, токены `{vid_res['llm_usage']['tokens_in']}+{vid_res['llm_usage']['tokens_out']}`, `credits_spent={vid_res['heygen_adapter']['credits_spent']}`.",
            f"- **Telegram-адаптер (`POST /api/telegram/webhook`):** `ok={tg_res['ok']}`, длина ответа {len(tg_res['text'])} симв.",
            "",
            "## 4. Итоговая статистика (`/api/stats` и `/api/escalations`)",
            f"- Всего вопросов (включая Telegram): **{stats['total_questions']}**",
            f"- Повторяющихся вопросов: **{stats['repeat_questions']} ({stats['repeat_share_pct']}%)**",
            f"- Кэш-попаданий LLM: **{stats['cache_hits']}**",
            f"- Эскалаций продакт-менеджеру: **{stats['escalated_questions']}** (открыто: {stats['open_escalations']})",
            f"- Суммарно токенов: **{stats['tokens_in_total']} + {stats['tokens_out_total']}**, итого стоимость: **{stats['total_cost_rub']} ₽**, средняя цена 1 вопроса: **{stats['avg_cost_per_question_rub']} ₽**",
            f"- Средняя задержка: **{stats['avg_latency_ms']} мс**",
            "",
            "Все автопроверки пройдены успешно (`EXIT 0`).",
        ])

        (ROOT / "tests-log.md").write_text("\n".join(lines_md), encoding="utf-8")
        print("E2E OK! Stats:", json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
