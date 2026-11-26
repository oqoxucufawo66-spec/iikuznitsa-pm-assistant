from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass
from app.db import get_conn
from app.llm_client import embed_texts

_STOP_WORDS = {
    "и", "в", "во", "на", "с", "со", "к", "по", "о", "об", "для", "от", "до", "из", "у",
    "как", "что", "это", "ли", "или", "не", "а", "но", "же", "бы", "ли", "есть", "какой",
    "какая", "какие", "сколько", "можно", "нужно", "мне", "нам", "под", "при", "про", "чем",
}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower().replace("ё", "е"))
    out = []
    for w in words:
        if w in _STOP_WORDS or len(w) < 2:
            continue
        # Light stem for Russian/English matching (prefix up to 5-6 chars for long words)
        out.append(w)
    return out


def _stem(w: str) -> str:
    if len(w) >= 6 and re.search(r"[а-я]", w):
        return w[:5]
    if len(w) >= 5 and re.search(r"[а-я]", w):
        return w[:4]
    return w


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if (na > 0 and nb > 0) else 0.0


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_type: str
    source_id: str
    source_title: str
    source_file: str
    section: str
    content: str
    score: float
    lexical_score: float
    vector_score: float


def search_knowledge_base(
    query: str,
    top_k: int = 5,
    doc_type_filter: str | None = None,
    use_vector: bool = True,
) -> list[RetrievedChunk]:
    q_tokens = tokenize(query)
    q_stems = {_stem(t) for t in q_tokens}
    q_upper = query.upper()

    with get_conn() as conn:
        if doc_type_filter:
            rows = conn.execute("SELECT * FROM chunks WHERE doc_type=?", (doc_type_filter,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM chunks").fetchall()

    if not rows:
        return []

    q_vec: list[float] | None = None
    has_vectors = any(r["embedding_json"] for r in rows)
    if use_vector and has_vectors:
        vecs = embed_texts([query])
        if vecs:
            q_vec = vecs[0]

    results: list[RetrievedChunk] = []
    for r in rows:
        content = r["content"]
        title = r["source_title"]
        c_tokens = tokenize(content + " " + title)
        c_stems = {_stem(t) for t in c_tokens}

        if not q_stems:
            lex = 0.0
        else:
            exact_hits = len(set(q_tokens) & set(c_tokens))
            stem_hits = len(q_stems & c_stems)
            title_stems = {_stem(t) for t in tokenize(title)}
            title_hits = len(q_stems & title_stems)
            lex = (exact_hits * 1.2 + stem_hits * 0.9 + title_hits * 1.1) / (len(q_stems) + 1.5)
            # Boost direct SKU mention
            if r["source_id"].upper() in q_upper and len(r["source_id"]) >= 5:
                lex += 0.9
            lex = min(lex, 1.5)

        vec_score = 0.0
        if q_vec and r["embedding_json"]:
            try:
                c_vec = json.loads(r["embedding_json"])
                vec_score = max(0.0, _cosine(q_vec, c_vec))
            except Exception:
                vec_score = 0.0

        if q_vec and r["embedding_json"]:
            combined = 0.58 * vec_score + 0.42 * min(lex, 1.0)
        else:
            combined = min(lex, 1.0)

        results.append(
            RetrievedChunk(
                chunk_id=r["chunk_id"],
                doc_type=r["doc_type"],
                source_id=r["source_id"],
                source_title=r["source_title"],
                source_file=r["source_file"],
                section=r["section"],
                content=content,
                score=round(combined, 4),
                lexical_score=round(lex, 4),
                vector_score=round(vec_score, 4),
            )
        )

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:top_k]
