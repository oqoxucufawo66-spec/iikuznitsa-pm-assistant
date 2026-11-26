"""Интеграция с Яндекс Метрикой (Reporting API v1).

Режимы:
- live: METRIKA_TOKEN + METRIKA_COUNTER_ID заданы → тянем агрегаты по дням через https://api-metrika.yandex.net/stat/v1/data
  и пишем в pm_metrics (source='metrika_api'). Соответствие metric_id → запрос Метрики задаётся в config (metrika.queries).
- test: ключа нет → используются тестовые агрегаты Метрики из листа «Метрики_данные» (организаторы разрешили тестовые данные).
"""
from __future__ import annotations
import datetime as dt
import os
from typing import Any

import httpx

from app.db import get_conn
from app.pm.settings import load_config

API = "https://api-metrika.yandex.net/stat/v1/data"

# Маппинг metric_id → запрос Метрики задаётся в config (metrika.queries); ниже — нейтральный пример.
DEFAULT_QUERIES: dict[str, dict[str, Any]] = {
    "visits": {"metrics": "ym:s:visits", "dimensions": "ym:s:date,ym:s:deviceCategory", "unit": "visits"},
}


def status() -> dict[str, Any]:
    token = os.environ.get("METRIKA_TOKEN", "")
    counter = os.environ.get("METRIKA_COUNTER_ID", "")
    with get_conn() as conn:
        api_rows = conn.execute("SELECT COUNT(*) FROM pm_metrics WHERE source='metrika_api'").fetchone()[0]
        test_rows = conn.execute("SELECT COUNT(*) FROM pm_metrics WHERE source='dataset_xlsx'").fetchone()[0]
        rng = conn.execute("SELECT MIN(date), MAX(date) FROM pm_metrics").fetchone()
    return {"mode": "live" if (token and counter) else "test", "counter_id": counter or None,
            "api_rows": api_rows, "test_rows": test_rows, "date_from": rng[0], "date_to": rng[1],
            "queries": list((load_config().get("metrika", {}).get("queries") or DEFAULT_QUERIES).keys())}


def parse_response(js: dict[str, Any], metric_id: str, product: str, unit: str) -> list[tuple]:
    """Ответ Reporting API → строки pm_metrics. Первая dimension — дата, вторая (если есть) — сегмент."""
    rows = []
    for item in js.get("data", []):
        dims = item.get("dimensions", [])
        date = (dims[0].get("name") if dims else "") or ""
        seg = (dims[1].get("name") or dims[1].get("id") or "all") if len(dims) > 1 else "all"
        seg = {"Smartphones": "mobile", "PC": "desktop", "Tablets": "tablet", "Смартфоны": "mobile", "ПК": "desktop", "Планшеты": "tablet"}.get(seg, str(seg).lower())
        val = (item.get("metrics") or [0])[0]
        rows.append((date[:10], metric_id, product, seg, "live", "", float(val or 0), unit, float(val or 0), "", "Яндекс Метрика API"))
    return rows


def sync(date_from: str | None = None, date_to: str | None = None, product: str = "", params: dict[str, str] | None = None) -> dict[str, Any]:
    token = os.environ.get("METRIKA_TOKEN", "")
    counter = os.environ.get("METRIKA_COUNTER_ID", "")
    if not (token and counter):
        return {"ok": False, "mode": "test", "detail": "METRIKA_TOKEN/METRIKA_COUNTER_ID не заданы — используются тестовые данные датасета"}
    date_to = date_to or dt.date.today().isoformat()
    date_from = date_from or (dt.date.today() - dt.timedelta(days=30)).isoformat()
    queries = load_config().get("metrika", {}).get("queries") or DEFAULT_QUERIES
    inserted, errors = 0, []
    with httpx.Client(timeout=30.0, headers={"Authorization": f"OAuth {token}"}) as client:
        for mid, q in queries.items():
            p = {"ids": counter, "date1": date_from, "date2": date_to, "metrics": q["metrics"].format(**(params or {})),
                 "dimensions": q.get("dimensions", "ym:s:date"), "limit": 10000, "accuracy": "full"}
            if q.get("filters"):
                p["filters"] = q["filters"].format(**(params or {}))
            try:
                r = client.get(API, params=p)
                if r.status_code >= 400:
                    errors.append({"metric_id": mid, "http": r.status_code})
                    continue
                rows = parse_response(r.json(), mid, product or q.get("product", ""), q.get("unit", "visits"))
            except Exception as exc:
                errors.append({"metric_id": mid, "error": type(exc).__name__})
                continue
            with get_conn() as conn:
                conn.execute("DELETE FROM pm_metrics WHERE source='metrika_api' AND metric_id=? AND date BETWEEN ? AND ?", (mid, date_from, date_to))
                conn.executemany("INSERT INTO pm_metrics(date,metric_id,product,segment,period_type,release_id,value,unit,sample_size,detail_json,comment,source)"
                                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,'metrika_api')", rows)
            inserted += len(rows)
    return {"ok": not errors, "mode": "live", "inserted": inserted, "errors": errors}
