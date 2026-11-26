from __future__ import annotations
from typing import Any
from app.agent import run_agent
from app.db import get_conn


def handle_telegram_update(update: dict[str, Any]) -> dict[str, Any]:
    """Адаптер-заглушка для Telegram Bot API (webhook/polling)."""
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id", "unknown")
    user = msg.get("from") or {}
    username = user.get("username") or user.get("first_name") or "Менеджер TG"

    if not text:
        return {"ok": True, "action": "ignored_empty"}

    if text.startswith("/start") or text.startswith("/help"):
        reply = (
            "Привет! Я ИИ-помощник продакт-менеджера (ДЕМО-ДАННЫЕ).\n"
            "Спросите о характеристиках товара, сравнении моделей, доставке, оплате или гарантии.\n"
            "Команды: /catalog — список товаров, /help — справка."
        )
        return {"ok": True, "chat_id": chat_id, "method": "sendMessage", "text": reply}

    if text.startswith("/catalog"):
        with get_conn() as conn:
            rows = conn.execute("SELECT sku, name, price_rub FROM products ORDER BY category, sku LIMIT 10").fetchall()
        lines = ["Каталог (ДЕМО-ДАННЫЕ, первые 10):"]
        for r in rows:
            lines.append(f"• {r['sku']} — {r['name']} ({r['price_rub']} ₽)")
        return {"ok": True, "chat_id": chat_id, "method": "sendMessage", "text": "\n".join(lines)}

    res = run_agent(
        question=text,
        session_id=f"tg:{chat_id}",
        channel="telegram",
        manager_name=f"@{username}",
    )
    src_line = ""
    if res.sources:
        src_line = "\n\nИсточники: " + ", ".join(s["source_id"] for s in res.sources[:3])

    return {
        "ok": True,
        "chat_id": chat_id,
        "method": "sendMessage",
        "text": res.answer + src_line,
        "escalated": res.escalated,
        "escalation_id": res.escalation_id,
    }
