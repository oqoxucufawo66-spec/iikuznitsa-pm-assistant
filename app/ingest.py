from __future__ import annotations
import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from app.config import DATA_DIR
from app.db import get_conn, now_ts
from app.llm_client import embed_texts


def _product_to_text(p: dict[str, Any]) -> str:
    specs_str = "; ".join(f"{k}: {v}" for k, v in (p.get("specs") or {}).items())
    benefits_str = "; ".join(p.get("key_benefits") or [])
    obj_str = "; ".join(
        f"Возражение «{o.get('objection', '')}» -> {o.get('answer', '')}"
        for o in (p.get("objections") or [])
    )
    stock_str = "В наличии" if p.get("in_stock", True) else "Нет в наличии"
    parts = [
        f"Артикул (SKU): {p['sku']}",
        f"Название: {p['name']}",
        f"Категория: {p.get('category', '')} | Бренд: {p.get('brand', '')}",
        f"Цена: {p['price_rub']} ₽ | Гарантия: {p.get('warranty_months', 12)} мес. | Статус: {stock_str}",
        f"Характеристики: {specs_str}",
        f"Преимущества: {benefits_str}",
        f"Целевая аудитория: {p.get('target_audience', '')}",
        f"Комплектация: {p.get('package_contents', '')}",
    ]
    if obj_str:
        parts.append(f"Частые возражения: {obj_str}")
    return "\n".join(parts)


def _upsert_product(conn, p: dict[str, Any], source_file: str, ts: float) -> dict[str, str]:
    raw_text = _product_to_text(p)
    conn.execute(
        """
        INSERT OR REPLACE INTO products (
            sku, name, category, brand, price_rub, warranty_months, in_stock,
            specs_json, key_benefits_json, target_audience, package_contents,
            objections_json, source_file, is_demo, raw_text, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            p["sku"],
            p["name"],
            p.get("category", "Разное"),
            p.get("brand", "DemoBrand"),
            int(float(p["price_rub"])),
            int(float(p.get("warranty_months", 12))),
            1 if p.get("in_stock", True) else 0,
            json.dumps(p.get("specs") or {}, ensure_ascii=False),
            json.dumps(p.get("key_benefits") or [], ensure_ascii=False),
            p.get("target_audience", ""),
            p.get("package_contents", ""),
            json.dumps(p.get("objections") or [], ensure_ascii=False),
            source_file,
            raw_text,
            ts,
        ),
    )
    return {
        "chunk_id": f"prod:{p['sku']}",
        "doc_type": "product",
        "source_id": p["sku"],
        "source_title": p["name"],
        "source_file": source_file,
        "section": f"Карточка товара {p['sku']}",
        "content": raw_text,
    }


def _parse_csv_specs(raw: str) -> dict[str, str]:
    specs: dict[str, str] = {}
    for item in (raw or "").split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            specs[k.strip()] = v.strip()
    return specs


def _split_markdown_sections(text: str, filename: str, doc_type: str = "faq") -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    lines = text.splitlines()
    current_title = filename
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        cid = hashlib.md5(f"{filename}:{current_title}:{body[:80]}".encode()).hexdigest()[:12]
        chunks.append(
            {
                "chunk_id": f"{doc_type}:{cid}",
                "doc_type": doc_type,
                "source_id": filename,
                "source_title": f"{filename} — {current_title}",
                "source_file": filename,
                "section": current_title,
                "content": f"[{current_title}]\n{body}",
            }
        )

    for line in lines:
        m = re.match(r"^#{1,3}\s+(.+)$", line.strip())
        if m:
            flush()
            current_title = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    flush()

    if not chunks and text.strip():
        # Split plain text/PDF into ~900-char chunks with overlap
        clean = re.sub(r"\n{3,}", "\n\n", text.strip())
        step = 850
        for idx, start in enumerate(range(0, len(clean), step), 1):
            part = clean[start : start + 1000].strip()
            if part:
                cid = hashlib.md5(f"{filename}:{idx}:{part[:60]}".encode()).hexdigest()[:12]
                chunks.append(
                    {
                        "chunk_id": f"{doc_type}:{cid}",
                        "doc_type": doc_type,
                        "source_id": filename,
                        "source_title": f"{filename} (раздел {idx})",
                        "source_file": filename,
                        "section": f"Фрагмент {idx}",
                        "content": part,
                    }
                )
    return chunks


def extract_pdf_text(path: Path) -> str:
    try:
        out = subprocess.check_output(["pdftotext", "-layout", str(path), "-"], stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace")
    except Exception:
        return ""


def ingest_file(path: Path, compute_embeddings: bool = True) -> dict[str, int]:
    ext = path.suffix.lower()
    fname = path.name
    ts = now_ts()
    chunks_to_save: list[dict[str, str]] = []
    products_count = 0

    with get_conn() as conn:
        if ext == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("products", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for p in items:
                    if isinstance(p, dict) and p.get("sku") and p.get("name"):
                        chunks_to_save.append(_upsert_product(conn, p, fname, ts))
                        products_count += 1
        elif ext == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = (row.get("sku") or "").strip()
                    name = (row.get("name") or "").strip()
                    if not sku or not name:
                        continue
                    in_stock_raw = str(row.get("in_stock", "true")).strip().lower()
                    p = {
                        "sku": sku,
                        "name": name,
                        "category": (row.get("category") or "Разное").strip(),
                        "brand": (row.get("brand") or "DemoBrand").strip(),
                        "price_rub": int(float(row.get("price_rub") or 0)),
                        "warranty_months": int(float(row.get("warranty_months") or 12)),
                        "in_stock": in_stock_raw in ("1", "true", "yes", "да"),
                        "specs": _parse_csv_specs(row.get("specs") or ""),
                        "key_benefits": [b.strip() for b in (row.get("key_benefits") or "").split(";") if b.strip()],
                        "target_audience": (row.get("target_audience") or "").strip(),
                        "package_contents": (row.get("package_contents") or "").strip(),
                        "objections": [],
                    }
                    chunks_to_save.append(_upsert_product(conn, p, fname, ts))
                    products_count += 1
        elif ext in (".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks_to_save.extend(_split_markdown_sections(text, fname, doc_type="faq"))
        elif ext == ".pdf":
            text = extract_pdf_text(path)
            chunks_to_save.extend(_split_markdown_sections(text, fname, doc_type="doc"))

        embeddings: list[list[float]] | None = None
        if compute_embeddings and chunks_to_save:
            texts = [c["content"][:1800] for c in chunks_to_save]
            # Batch in groups of 16
            all_vecs: list[list[float]] = []
            ok = True
            for i in range(0, len(texts), 16):
                vecs = embed_texts(texts[i : i + 16])
                if vecs is None:
                    ok = False
                    break
                all_vecs.extend(vecs)
            if ok and len(all_vecs) == len(chunks_to_save):
                embeddings = all_vecs

        for idx, ch in enumerate(chunks_to_save):
            emb_json = json.dumps(embeddings[idx]) if embeddings else None
            conn.execute(
                """
                INSERT OR REPLACE INTO chunks (
                    chunk_id, doc_type, source_id, source_title, source_file,
                    section, content, embedding_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ch["chunk_id"],
                    ch["doc_type"],
                    ch["source_id"],
                    ch["source_title"],
                    ch["source_file"],
                    ch["section"],
                    ch["content"],
                    emb_json,
                    ts,
                ),
            )

    return {"products": products_count, "chunks": len(chunks_to_save)}


def ingest_directory(directory: Path | None = None, compute_embeddings: bool = True) -> dict[str, Any]:
    target = directory or DATA_DIR
    total_products = 0
    total_chunks = 0
    files_processed: list[str] = []

    if not target.exists():
        return {"products": 0, "chunks": 0, "files": []}

    for path in sorted(target.iterdir()):
        if path.is_file() and path.suffix.lower() in (".json", ".csv", ".md", ".txt", ".pdf"):
            res = ingest_file(path, compute_embeddings=compute_embeddings)
            total_products += res["products"]
            total_chunks += res["chunks"]
            files_processed.append(path.name)

    return {"products": total_products, "chunks": total_chunks, "files": files_processed}
