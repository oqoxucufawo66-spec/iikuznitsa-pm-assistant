"""Загрузка материалов задачи в SQLite (закрытый контур).

Исходные файлы организаторов лежат ВНЕ git (PM_DATA_DIR, по умолчанию ./private-data):
правила датасета запрещают публиковать данные в публичных репозиториях.
Исходные листы не меняются: в БД пишутся копии строк + производные расчёты.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from app.db import get_conn, now_ts
from app.pm.settings import PM_DATA_DIR, load_config

PM_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_metrics_ref (
    metric_id TEXT PRIMARY KEY, metric_name TEXT, what_it_means TEXT, formula TEXT, source TEXT,
    segment TEXT, target_value TEXT, bad_signal TEXT, priority_impact TEXT, comment TEXT
);
CREATE TABLE IF NOT EXISTS pm_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, metric_id TEXT, product TEXT, segment TEXT,
    period_type TEXT, release_id TEXT, value REAL, unit TEXT, sample_size REAL, detail_json TEXT,
    comment TEXT, source TEXT NOT NULL DEFAULT 'dataset_xlsx'
);
CREATE INDEX IF NOT EXISTS ix_pm_metrics ON pm_metrics(metric_id, product, date);
CREATE TABLE IF NOT EXISTS pm_releases (
    release_id TEXT PRIMARY KEY, release_date TEXT, release_type TEXT, description TEXT,
    related_task_ids TEXT, affected_metrics TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS pm_backlog (
    task_id TEXT PRIMARY KEY, title TEXT, type TEXT, status TEXT, priority TEXT, created_date TEXT,
    closed_date TEXT, release_id TEXT, description TEXT, linked_metric_ids TEXT, source TEXT,
    tags TEXT NOT NULL DEFAULT '[]', ai_generated INTEGER NOT NULL DEFAULT 0, details_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS pm_backlog_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, field TEXT, old_value TEXT, new_value TEXT,
    reason TEXT, tag TEXT, status TEXT NOT NULL DEFAULT 'proposed', created_at REAL
);
CREATE TABLE IF NOT EXISTS pm_sales (
    week_start TEXT, product_family TEXT, orders_index REAL, orders_index_4w REAL, base_period TEXT,
    channel TEXT, comment TEXT, PRIMARY KEY (week_start, product_family)
);
CREATE TABLE IF NOT EXISTS pm_reference_case (
    step TEXT, what_agent_sees TEXT, expected_action TEXT, expected_result TEXT, sheets_used TEXT
);
CREATE TABLE IF NOT EXISTS pm_competitors (
    site TEXT PRIMARY KEY, scores_json TEXT, total REAL, weighted_total REAL
);
CREATE TABLE IF NOT EXISTS pm_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, product TEXT, sku TEXT, variant TEXT,
    price REAL, store_price REAL, ratio REAL, checked_at TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS pm_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, recipient TEXT, subject TEXT, body TEXT,
    status TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS pm_sources (
    file TEXT PRIMARY KEY, kind TEXT, sha256 TEXT, rows INTEGER, loaded_at REAL
);
"""


def init_pm_db() -> None:
    with get_conn() as conn:
        conn.executescript(PM_SCHEMA)


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except Exception:
        return None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _rows(ws) -> list[list[Any]]:
    return [list(r) for r in ws.iter_rows(values_only=True) if any(c is not None and str(c).strip() != "" for c in r)]


def _header_map(header: list[Any]) -> dict[str, int]:
    return {_s(h): i for i, h in enumerate(header) if _s(h)}


def find_files() -> dict[str, list[Path]]:
    d = PM_DATA_DIR
    out: dict[str, list[Path]] = {"dataset": [], "competitive_xlsx": [], "competitive_pptx": [], "research": []}
    if not d.exists():
        return out
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.name.startswith((".", "~$")):
            continue
        n = p.name.lower()
        if p.suffix.lower() == ".zip":
            target = p.with_suffix("")
            if not target.exists():
                with zipfile.ZipFile(p) as zf:
                    zf.extractall(target)
            continue
        if p.suffix.lower() == ".xlsx" and "датасет" in n:
            out["dataset"].append(p)
        elif p.suffix.lower() == ".xlsx" and "конкурент" in n:
            out["competitive_xlsx"].append(p)
        elif p.suffix.lower() == ".pptx":
            out["competitive_pptx"].append(p)
        elif p.suffix.lower() in (".xlsx", ".md", ".txt", ".csv"):
            out["research"].append(p)
    # zip мог распаковаться после обхода
    for p in sorted(d.rglob("*.xlsx")):
        n = p.name.lower()
        if "датасет" in n or "конкурент" in n or p.name.startswith("~$"):
            continue
        if p not in out["research"]:
            out["research"].append(p)
    return out


def load_dataset(path: Path) -> dict[str, int]:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    counts: dict[str, int] = {}
    ts = now_ts()
    with get_conn() as conn:
        for tbl in ("pm_metrics_ref", "pm_releases", "pm_sales", "pm_reference_case"):
            conn.execute(f"DELETE FROM {tbl}")
        conn.execute("DELETE FROM pm_metrics WHERE source='dataset_xlsx'")
        conn.execute("DELETE FROM pm_backlog WHERE ai_generated=0")
        for ws in wb.worksheets:
            rows = _rows(ws)
            if not rows:
                continue
            name = ws.title
            if name == "Метрики_справочник":
                h = _header_map(rows[0])
                for r in rows[1:]:
                    g = lambda k: _s(r[h[k]]) if k in h and h[k] < len(r) else ""
                    if not g("metric_id"):
                        continue
                    conn.execute("INSERT OR REPLACE INTO pm_metrics_ref VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        g("metric_id"), g("metric_name"), g("what_it_means"), g("formula_or_logic"), g("source"),
                        g("segment"), g("target_value"), g("bad_signal"), g("priority_impact"), g("comment")))
                counts["metrics_ref"] = len(rows) - 1
            elif name == "Метрики_данные":
                h = _header_map(rows[0])
                batch = []
                for r in rows[1:]:
                    g = lambda k: r[h[k]] if k in h and h[k] < len(r) else None
                    if not _s(g("metric_id")):
                        continue
                    batch.append((_s(g("date"))[:10], _s(g("metric_id")), _s(g("product")), _s(g("segment")),
                                  _s(g("period_type")), _s(g("release_id")), _f(g("value")), _s(g("unit")),
                                  _f(g("sample_size")), _s(g("detail_json")), _s(g("comment"))))
                conn.executemany(
                    "INSERT INTO pm_metrics(date,metric_id,product,segment,period_type,release_id,value,unit,sample_size,detail_json,comment,source)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,'dataset_xlsx')", batch)
                counts["metrics_rows"] = len(batch)
            elif name == "Релизы":
                h = _header_map(rows[0])
                for r in rows[1:]:
                    g = lambda k: _s(r[h[k]]) if k in h and h[k] < len(r) else ""
                    if not g("release_id").startswith("R-"):
                        continue
                    conn.execute("INSERT OR REPLACE INTO pm_releases VALUES (?,?,?,?,?,?,?)", (
                        g("release_id"), g("release_date")[:10], g("release_type"), g("description"),
                        g("related_task_ids"), g("affected_metrics"), g("status")))
                counts["releases"] = len(rows) - 1
            elif name == "Бэклог_задачи":
                h = _header_map(rows[0])
                n = 0
                for r in rows[1:]:
                    g = lambda k: _s(r[h[k]]) if k in h and h[k] < len(r) else ""
                    if not g("task_id").startswith("T-"):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO pm_backlog(task_id,title,type,status,priority,created_date,closed_date,release_id,description,linked_metric_ids,source,tags,ai_generated,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]',0,?)",
                        (g("task_id"), g("title"), g("type"), g("status"), g("priority"), g("created_date")[:10],
                         g("closed_date")[:10], g("release_id"), g("description_short"), g("linked_metric_ids"), g("source"), ts))
                    n += 1
                counts["backlog"] = n
            elif name == "Продажи":
                h = _header_map(rows[0])
                n = 0
                for r in rows[1:]:
                    g = lambda k: r[h[k]] if k in h and h[k] < len(r) else None
                    if not _s(g("product_family")):
                        continue
                    conn.execute("INSERT OR REPLACE INTO pm_sales VALUES (?,?,?,?,?,?,?)", (
                        _s(g("week_start"))[:10], _s(g("product_family")), _f(g("orders_index")), _f(g("orders_index_4w")),
                        _s(g("base_period")), _s(g("channel")), _s(g("comment"))))
                    n += 1
                counts["sales_weeks"] = n
            elif name == "Эталонный_кейс":
                for r in rows:
                    if len(r) >= 5 and re.fullmatch(r"\d+", _s(r[0]) or "x"):
                        conn.execute("INSERT INTO pm_reference_case VALUES (?,?,?,?,?)", tuple(_s(c) for c in r[:5]))
                counts["reference_steps"] = conn.execute("SELECT COUNT(*) FROM pm_reference_case").fetchone()[0]
            elif name in ("Исследования", "Конкурентный_анализ") and len(rows) > 1:
                # если организаторы заполнят листы в новой версии — индексируем как исследования
                _index_table_rows(conn, rows, doc_type="research" if name == "Исследования" else "competitive",
                                  study=f"Датасет / {name}", source_file=path.name)
        conn.execute("INSERT OR REPLACE INTO pm_sources VALUES (?,?,?,?,?)",
                     (path.name, "dataset", _sha(path), sum(counts.values()), ts))
    return counts


def _chunk_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _insert_chunk(conn, doc_type: str, source_id: str, title: str, source_file: str, section: str, content: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO chunks(chunk_id, doc_type, source_id, source_title, source_file, section, content, embedding_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?,NULL,?)",
        (_chunk_id(doc_type, source_id, section, content[:200]), doc_type, source_id, title, source_file, section, content, now_ts()))


def _index_table_rows(conn, rows: list[list[Any]], doc_type: str, study: str, source_file: str, max_chars: int = 1800) -> int:
    """Широкая таблица интервью: 1 строка = 1 респондент, колонки = вопросы. Пишем пары «вопрос: ответ»."""
    if not rows:
        return 0
    # строка заголовка — та, где больше всего непустых ячеек среди первых 4
    hdr_idx = max(range(min(4, len(rows))), key=lambda i: sum(1 for c in rows[i] if _s(c)))
    header = [_s(c) for c in rows[hdr_idx]]
    n = 0
    for ri, r in enumerate(rows[hdr_idx + 1:], start=1):
        pairs = []
        for ci, c in enumerate(r):
            val = _s(c)
            if not val or val in ("-", "–", "—"):
                continue
            q = header[ci] if ci < len(header) and header[ci] else f"Колонка {ci + 1}"
            pairs.append(f"{q}: {val}")
        if len(pairs) < 2:
            continue
        text = "\n".join(pairs)
        part = 0
        while text:
            piece, text = text[:max_chars], text[max_chars:]
            part += 1
            sid = f"{study} · №{ri}" + (f" ч.{part}" if part > 1 or text else "")
            _insert_chunk(conn, doc_type, sid, sid, source_file, study, piece)
            n += 1
    return n


def load_research_file(path: Path) -> int:
    """Новые исследования/интервью: xlsx (широкие таблицы), md/txt/csv. Закрытый контур: просто положить файл и вызвать reindex."""
    ts = now_ts()
    n = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_type='research' AND source_file=?", (path.name,))
        if path.suffix.lower() == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
            for ws in wb.worksheets:
                rows = _rows(ws)
                study = f"{path.parent.name}/{path.stem} / {ws.title}" if path.parent != PM_DATA_DIR else f"{path.stem} / {ws.title}"
                n += _index_table_rows(conn, rows, "research", study.replace("Транскрибации интервью/", ""), path.name)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for i in range(0, len(text), 1800):
                sid = f"{path.stem} · ч.{i // 1800 + 1}"
                _insert_chunk(conn, "research", sid, sid, path.name, path.stem, text[i:i + 1800])
                n += 1
        conn.execute("INSERT OR REPLACE INTO pm_sources VALUES (?,?,?,?,?)", (path.name, "research", _sha(path), n, ts))
    return n


def load_competitive_xlsx(path: Path) -> dict[str, int]:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ts = now_ts()
    counts = {"chunks": 0, "competitors": 0, "prices": 0}
    with get_conn() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_type='competitive' AND source_file=?", (path.name,))
        conn.execute("DELETE FROM pm_competitors")
        conn.execute("DELETE FROM pm_prices")
        for ws in wb.worksheets:
            rows = _rows(ws)
            if not rows:
                continue
            if ws.title == "Итоги":
                blocks = None
                mode = None
                for r in rows:
                    first = _s(r[0])
                    if first == "Без учёта веса":
                        mode = "raw"; continue
                    if first == "С учетом веса":
                        mode = "w"; continue
                    if first == "Конкурент":
                        blocks = [_s(c) for c in r[1:8]]; continue
                    if first.startswith("Легенда"):
                        mode = None
                    if mode and blocks and "." in first and len(r) > 7:
                        scores = {blocks[i]: _s(r[i + 1]) for i in range(len(blocks)) if blocks[i]}
                        tot = _f(r[7])
                        if mode == "raw":
                            conn.execute("INSERT OR REPLACE INTO pm_competitors(site,scores_json,total,weighted_total) VALUES (?,?,?,COALESCE((SELECT weighted_total FROM pm_competitors WHERE site=?),NULL))",
                                         (first, json.dumps(scores, ensure_ascii=False), tot, first))
                            counts["competitors"] += 1
                        else:
                            conn.execute("UPDATE pm_competitors SET weighted_total=? WHERE site=?", (tot, first))
            if ws.title == "03_Цена":
                hdr_idx = max(range(min(4, len(rows))), key=lambda i: sum(1 for c in rows[i] if _s(c)))
                for r in rows[hdr_idx + 1:]:
                    if len(r) < 11 or not _s(r[0]) or "." not in _s(r[0]):
                        continue
                    price, store_price, ratio = _f(r[8]), _f(r[9]), _f(r[10])
                    if price is None:
                        continue
                    conn.execute("INSERT INTO pm_prices(site,product,sku,variant,price,store_price,ratio,checked_at,url) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (_s(r[0]), _s(r[1]), _s(r[2]), (_s(r[6]) + " " + _s(r[7])).strip(), price, store_price, ratio,
                                  _s(r[13])[:10] if len(r) > 13 else "", _s(r[5])))
                    counts["prices"] += 1
            # все листы — в поиск, пачками строк
            hdr_idx = max(range(min(4, len(rows))), key=lambda i: sum(1 for c in rows[i] if _s(c)))
            header = [_s(c) for c in rows[hdr_idx]]
            pack: list[str] = []
            part = 0
            body = rows[hdr_idx + 1:]
            for i, r in enumerate(body):
                cells = [f"{header[ci] or ci}: {_s(c)}" for ci, c in enumerate(r) if _s(c) and ci < len(header) + 5]
                if cells:
                    pack.append("; ".join(cells)[:700])
                if len("\n".join(pack)) > 1600 or i == len(body) - 1:
                    if pack:
                        part += 1
                        sid = f"Конкуренты / {ws.title} · ч.{part}"
                        _insert_chunk(conn, "competitive", sid, sid, path.name, ws.title, "\n".join(pack))
                        counts["chunks"] += 1
                    pack = []
        conn.execute("INSERT OR REPLACE INTO pm_sources VALUES (?,?,?,?,?)", (path.name, "competitive", _sha(path), sum(counts.values()), ts))
    return counts


def load_competitive_pptx(path: Path) -> int:
    from pptx import Presentation
    prs = Presentation(str(path))
    n = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_type='competitive' AND source_file=?", (path.name,))
        for i, s in enumerate(prs.slides, 1):
            texts = []
            for sh in s.shapes:
                if sh.has_text_frame and sh.text_frame.text.strip():
                    texts.append(sh.text_frame.text.strip())
                if getattr(sh, "has_table", False) and sh.has_table:
                    for row in sh.table.rows:
                        texts.append(" | ".join(c.text for c in row.cells))
            if not texts:
                continue
            title = texts[0].split("\n")[0][:80]
            sid = f"Конкурентный анализ (pptx) · слайд {i}: {title}"
            _insert_chunk(conn, "competitive", sid, sid, path.name, f"слайд {i}", "\n".join(texts)[:3500])
            n += 1
        conn.execute("INSERT OR REPLACE INTO pm_sources VALUES (?,?,?,?,?)", (path.name, "competitive_pptx", _sha(path), n, now_ts()))
    return n


def load_all(force: bool = False) -> dict[str, Any]:
    init_pm_db()
    files = find_files()
    res: dict[str, Any] = {"data_dir": str(PM_DATA_DIR), "files": {k: [p.name for p in v] for k, v in files.items()}}
    with get_conn() as conn:
        loaded = {r["file"]: r["sha256"] for r in conn.execute("SELECT file, sha256 FROM pm_sources").fetchall()}

    def need(p: Path) -> bool:
        return force or loaded.get(p.name) != _sha(p)

    for p in files["dataset"]:
        if need(p):
            res.setdefault("dataset", {})[p.name] = load_dataset(p)
    for p in files["competitive_xlsx"]:
        if need(p):
            res.setdefault("competitive", {})[p.name] = load_competitive_xlsx(p)
    for p in files["competitive_pptx"]:
        if need(p):
            res.setdefault("pptx", {})[p.name] = load_competitive_pptx(p)
    for p in files["research"]:
        if need(p):
            res.setdefault("research", {})[p.name] = load_research_file(p)
    res["status"] = status()
    return res


def status() -> dict[str, Any]:
    init_pm_db()
    with get_conn() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]
        return {
            "metrics_rows": q("SELECT COUNT(*) FROM pm_metrics"),
            "metrics_ref": q("SELECT COUNT(*) FROM pm_metrics_ref"),
            "releases": q("SELECT COUNT(*) FROM pm_releases"),
            "backlog": q("SELECT COUNT(*) FROM pm_backlog"),
            "backlog_ai": q("SELECT COUNT(*) FROM pm_backlog WHERE ai_generated=1"),
            "sales_weeks": q("SELECT COUNT(*) FROM pm_sales"),
            "research_chunks": q("SELECT COUNT(*) FROM chunks WHERE doc_type='research'"),
            "competitive_chunks": q("SELECT COUNT(*) FROM chunks WHERE doc_type='competitive'"),
            "competitors": q("SELECT COUNT(*) FROM pm_competitors"),
            "prices": q("SELECT COUNT(*) FROM pm_prices"),
            "metrika_api_rows": q("SELECT COUNT(*) FROM pm_metrics WHERE source='metrika_api'"),
            "sources": [dict(r) for r in conn.execute("SELECT file, kind, rows, loaded_at FROM pm_sources ORDER BY kind, file").fetchall()],
            "loaded": q("SELECT COUNT(*) FROM pm_sales") > 0,
        }
