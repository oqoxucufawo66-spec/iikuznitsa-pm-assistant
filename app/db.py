from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from app.config import DB_PATH, ARTIFACTS_DIR


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    brand TEXT NOT NULL,
    price_rub INTEGER NOT NULL,
    warranty_months INTEGER NOT NULL DEFAULT 12,
    in_stock INTEGER NOT NULL DEFAULT 1,
    specs_json TEXT NOT NULL,
    key_benefits_json TEXT NOT NULL,
    target_audience TEXT NOT NULL DEFAULT '',
    package_contents TEXT NOT NULL DEFAULT '',
    objections_json TEXT NOT NULL DEFAULT '[]',
    source_file TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 1,
    raw_text TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_file TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding_json TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS faq_clusters (
    cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_q TEXT UNIQUE NOT NULL,
    canonical_q TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    last_asked_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web',
    question TEXT NOT NULL,
    normalized_q TEXT NOT NULL,
    cluster_id INTEGER,
    is_repeat INTEGER NOT NULL DEFAULT 0,
    intent TEXT NOT NULL,
    answer TEXT NOT NULL,
    escalated INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.0,
    sources_json TEXT NOT NULL DEFAULT '[]',
    trace_json TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_rub REAL NOT NULL DEFAULT 0.0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pm_escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    reason TEXT NOT NULL,
    related_sku TEXT DEFAULT '',
    manager_name TEXT DEFAULT 'Менеджер по продажам',
    status TEXT NOT NULL DEFAULT 'open',
    pm_reply TEXT DEFAULT '',
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response_text TEXT NOT NULL,
    tokens_saved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""


def init_db(db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def get_conn(db_path: Path | None = None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_ts() -> float:
    return time.time()
