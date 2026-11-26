from __future__ import annotations
import json
from typing import Any
from app.config import ARTIFACTS_DIR, Settings, get_settings
from app.db import get_conn, now_ts
from app.llm_client import LLMError, chat_completion


def generate_video_script_and_avatar_job(sku: str, dry_run: bool | None = None) -> dict[str, Any]:
    st: Settings = get_settings()
    is_dry_run = st.heygen_dry_run if dry_run is None else dry_run

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        if not row:
            raise ValueError(f"Товар {sku} не найден")
        product = dict(row)

    specs = json.loads(product["specs_json"])
    benefits = json.loads(product["key_benefits_json"])
    objections = json.loads(product["objections_json"])

    used_model = "template"
    tokens_in = tokens_out = 0
    cost_rub = 0.0

    prompt = (
        f"Напиши сценарий короткого видеообзора (60–70 секунд) для цифрового аватара продакт-менеджера о товаре (ДЕМО-ДАННЫЕ).\n"
        f"Данные карточки:\n{product['raw_text']}\n\n"
        "Верни строго 5 сцен в читаемом формате с таймкодами (00:00-00:10, 00:10-00:25, 00:25-00:45, 00:45-01:00, 01:00-01:10), "
        "указывая текст диктора для аватара и короткую экранную плашку. Не выдумывай характеристик, которых нет в карточке."
    )

    try:
        llm_res = chat_completion(
            messages=[
                {"role": "system", "content": "Ты сценарист обучающих видеообзоров для отдела продаж. Строго опирайся на факты карточки."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=750,
            temperature=0.2,
            use_cache=True,
            settings=st,
        )
        script_markdown = llm_res.text
        used_model = f"{llm_res.provider}/{llm_res.model}"
        tokens_in, tokens_out, cost_rub = llm_res.tokens_in, llm_res.tokens_out, llm_res.cost_rub
    except LLMError:
        b1 = benefits[0] if benefits else product["name"]
        b2 = benefits[1] if len(benefits) > 1 else f"Гарантия {product['warranty_months']} месяцев"
        top_specs = ", ".join(f"{k} — {v}" for k, v in list(specs.items())[:3])
        obj_txt = (
            f"Если клиент сомневается («{objections[0]['objection']}»), напомните: {objections[0]['answer']}"
            if objections
            else f"Отметим гарантию {product['warranty_months']} месяцев и понятную комплектацию."
        )
        script_markdown = (
            f"### Сценарий видеообзора (ДЕМО-ДАННЫЕ): {product['name']}\n"
            f"- **00:00–00:10 [Хук / Аватар в кадре]:** Коллеги, разбираем {product['name']} ({product['sku']}) по цене {product['price_rub']} ₽. Это решение для: {product['target_audience']}.\n"
            f"- **00:10–00:25 [Ключевая выгода]:** Главное преимущество — {b1}. Дополнительно: {b2}.\n"
            f"- **00:25–00:45 [Цифры и характеристики]:** Ключевые параметры из карточки: {top_specs}.\n"
            f"- **00:45–01:00 [Отработка возражения]:** {obj_txt}\n"
            f"- **01:00–01:10 [Призыв к действию]:** Полная спецификация и сравнение с аналогами уже в ИИ-помощнике — используйте в звонках с клиентами!"
        )

    # Extract clean spoken narration text (without markdown/timings/screen overlays)
    import re as _re
    narration_lines = []
    for line in script_markdown.splitlines():
        clean = _re.sub(r"\*\*", "", line).strip().lstrip("-# ").strip()
        if not clean:
            continue
        if "Текст диктора:" in clean:
            spoken = clean.split("Текст диктора:", 1)[1].strip()
            if spoken:
                narration_lines.append(spoken)
    if not narration_lines:
        for line in script_markdown.splitlines():
            clean = _re.sub(r"\*\*", "", line).strip().lstrip("-# ").strip()
            if clean and not clean.startswith(("Сценарий", "Сцена ", "Таймкод:", "Экранная плашка:")):
                narration_lines.append(clean)
    narration_text = (
        f"Демонстрационный обзор товара {product['name'].replace(' (ДЕМО-ДАННЫЕ)', '')}. "
        + " ".join(narration_lines)
    )[:1800]

    # HeyGen v2 Video Generate API payload (dry-run — no credits spent!)
    heygen_payload = {
        "title": f"[ДЕМО-ДАННЫЕ] Видеообзор {product['sku']} — {product['name']}",
        "caption": False,
        "test": True,
        "dimension": {"width": 1280, "height": 720},
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": st.heygen_avatar_id,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": narration_text,
                    "voice_id": st.heygen_voice_id,
                    "speed": 1.0,
                },
                "background": {"type": "color", "value": "#0F2240"},
            }
        ],
        "meta_demo": {
            "dry_run": True,
            "endpoint": "POST https://api.heygen.com/v2/video/generate",
            "note": "Режим dry-run: запрос сформирован и провалидирован, платный вызов API и списание кредитов не выполнялись.",
        },
    }

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    script_file = f"{sku}_video_script.md"
    payload_file = f"{sku}_heygen_dry_run.json"
    (ARTIFACTS_DIR / script_file).write_text(script_markdown, encoding="utf-8")
    (ARTIFACTS_DIR / payload_file).write_text(
        json.dumps(heygen_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO materials(sku, kind, title, file_path, meta_json, created_at) VALUES(?,?,?,?,?,?)",
            (
                sku,
                "video_script_heygen_dry_run",
                f"Видеосценарий + HeyGen dry-run: {product['name']}",
                str(ARTIFACTS_DIR / payload_file),
                json.dumps({"dry_run": True, "model": used_model, "tokens_in": tokens_in, "tokens_out": tokens_out}, ensure_ascii=False),
                now_ts(),
            ),
        )

    return {
        "sku": sku,
        "product_name": product["name"],
        "script_markdown": script_markdown,
        "narration_preview": narration_text,
        "heygen_adapter": {
            "mode": "dry_run" if is_dry_run else "dry_run_enforced_no_spend",
            "endpoint": "https://api.heygen.com/v2/video/generate",
            "credits_spent": 0,
            "api_key_configured": bool(st.heygen_api_key),
            "payload": heygen_payload,
        },
        "files": {
            "script_url": f"/api/artifacts/{script_file}",
            "payload_url": f"/api/artifacts/{payload_file}",
        },
        "llm_usage": {
            "model": used_model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_rub": cost_rub,
        },
    }
