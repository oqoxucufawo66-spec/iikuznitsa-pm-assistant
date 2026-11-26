from __future__ import annotations
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from app.agent import run_agent
from app.config import ARTIFACTS_DIR, DATA_DIR, get_settings
from app.db import get_conn, init_db, now_ts
from app.ingest import ingest_directory, ingest_file
from app.pptx_gen import generate_sales_pptx
from app.telegram_stub import handle_telegram_update
from app.video_avatar import generate_video_script_and_avatar_job

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    if cnt == 0 and DATA_DIR.exists():
        ingest_directory(DATA_DIR, compute_embeddings=True)
    yield


app = FastAPI(
    title="ИИ-помощник продакт-менеджера (ИИкузница — ДЕМО-ДАННЫЕ)",
    version="0.1.0",
    lifespan=lifespan,
)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=2000)
    session_id: str = "web-demo"
    manager_name: str = "Менеджер по продажам"


class GenerateRequest(BaseModel):
    sku: str


class EscalationResolveRequest(BaseModel):
    pm_reply: str = Field(..., min_length=2)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, Any]:
    st = get_settings()
    with get_conn() as conn:
        prod_n = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        chunk_n = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    return {
        "status": "ok",
        "dataset": st.dataset_label,
        "provider": st.provider,
        "model": st.model,
        "ru_hosted": st.ru_hosted,
        "heygen_dry_run": st.heygen_dry_run,
        "products_count": prod_n,
        "chunks_count": chunk_n,
    }


@app.post("/api/chat")
def api_chat(req: ChatRequest) -> dict[str, Any]:
    res = run_agent(
        question=req.question,
        session_id=req.session_id,
        channel="web",
        manager_name=req.manager_name,
    )
    return {
        "answer": res.answer,
        "escalated": res.escalated,
        "escalation_id": res.escalation_id,
        "escalation_reason": res.escalation_reason,
        "intent": res.intent,
        "confidence": res.confidence,
        "is_repeat": res.is_repeat,
        "repeat_cluster_hits": res.repeat_cluster_hits,
        "sources": res.sources,
        "trace": res.trace,
        "provider": res.provider,
        "model": res.model,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "cost_rub": res.cost_rub,
        "cache_hit": res.cache_hit,
        "latency_ms": res.latency_ms,
    }


@app.get("/api/products")
def api_products() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY category, sku").fetchall()
    items = []
    for r in rows:
        items.append({
            "sku": r["sku"],
            "name": r["name"],
            "category": r["category"],
            "brand": r["brand"],
            "price_rub": r["price_rub"],
            "warranty_months": r["warranty_months"],
            "in_stock": bool(r["in_stock"]),
            "specs": json.loads(r["specs_json"]),
            "key_benefits": json.loads(r["key_benefits_json"]),
            "target_audience": r["target_audience"],
            "source_file": r["source_file"],
            "is_demo": bool(r["is_demo"]),
        })
    return {"dataset": "ДЕМО-ДАННЫЕ", "count": len(items), "items": items}


@app.post("/api/ingest")
async def api_ingest(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".json", ".csv", ".md", ".txt", ".pdf"):
        raise HTTPException(status_code=400, detail="Поддерживаются только JSON, CSV, MD, TXT и PDF")
    upload_dir = DATA_DIR / "uploaded"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / Path(file.filename).name
    content = await file.read()
    dest.write_bytes(content)
    res = ingest_file(dest, compute_embeddings=True)
    return {"ok": True, "filename": dest.name, **res}


@app.post("/api/reingest-demo")
def api_reingest_demo() -> dict[str, Any]:
    res = ingest_directory(DATA_DIR, compute_embeddings=True)
    return {"ok": True, **res}


@app.post("/api/generate/pptx")
def api_generate_pptx(req: GenerateRequest) -> dict[str, Any]:
    try:
        return generate_sales_pptx(req.sku)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/generate/video")
def api_generate_video(req: GenerateRequest) -> dict[str, Any]:
    try:
        return generate_video_script_and_avatar_job(req.sku, dry_run=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/artifacts/{filename}")
def api_artifact(filename: str):
    safe_name = Path(filename).name
    target = ARTIFACTS_DIR / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path=str(target), filename=safe_name)


@app.get("/api/escalations")
def api_escalations() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pm_escalations ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/escalations/{esc_id}/resolve")
def api_resolve_escalation(esc_id: int, req: EscalationResolveRequest) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pm_escalations WHERE id=?", (esc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Обращение не найдено")
        conn.execute(
            "UPDATE pm_escalations SET status='resolved', pm_reply=?, resolved_at=? WHERE id=?",
            (req.pm_reply.strip(), now_ts(), esc_id),
        )
    return {"ok": True, "id": esc_id, "status": "resolved"}


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    st = get_settings()
    with get_conn() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_q,
                SUM(CASE WHEN is_repeat=1 THEN 1 ELSE 0 END) AS repeat_q,
                SUM(CASE WHEN escalated=1 THEN 1 ELSE 0 END) AS escalated_q,
                SUM(CASE WHEN cache_hit=1 THEN 1 ELSE 0 END) AS cache_hits,
                COALESCE(SUM(tokens_in), 0) AS sum_tin,
                COALESCE(SUM(tokens_out), 0) AS sum_tout,
                COALESCE(SUM(cost_rub), 0.0) AS sum_cost_rub,
                COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms
            FROM chat_logs
            """
        ).fetchone()
        top_clusters = conn.execute(
            "SELECT canonical_q, hit_count FROM faq_clusters ORDER BY hit_count DESC, cluster_id ASC LIMIT 8"
        ).fetchall()
        materials = conn.execute("SELECT id, sku, kind, title, created_at FROM materials ORDER BY id DESC LIMIT 10").fetchall()
        open_esc = conn.execute("SELECT COUNT(*) AS n FROM pm_escalations WHERE status='open'").fetchone()["n"]

    total_q = totals["total_q"] or 0
    repeat_q = totals["repeat_q"] or 0
    repeat_pct = round((repeat_q / total_q) * 100, 1) if total_q else 0.0
    avg_cost = round((totals["sum_cost_rub"] / total_q), 6) if total_q else 0.0

    return {
        "provider": st.provider,
        "model": st.model,
        "ru_hosted": st.ru_hosted,
        "total_questions": total_q,
        "repeat_questions": repeat_q,
        "repeat_share_pct": repeat_pct,
        "escalated_questions": totals["escalated_q"] or 0,
        "open_escalations": open_esc,
        "cache_hits": totals["cache_hits"] or 0,
        "tokens_in_total": totals["sum_tin"],
        "tokens_out_total": totals["sum_tout"],
        "total_cost_rub": round(totals["sum_cost_rub"], 6),
        "avg_cost_per_question_rub": avg_cost,
        "avg_latency_ms": round(totals["avg_latency_ms"], 1),
        "top_repeated_clusters": [dict(r) for r in top_clusters],
        "recent_materials": [dict(r) for r in materials],
    }


@app.post("/api/telegram/webhook")
def api_telegram_webhook(update: dict[str, Any]) -> dict[str, Any]:
    return handle_telegram_update(update)
