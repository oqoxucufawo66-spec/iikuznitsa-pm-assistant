from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Any
from app.config import get_settings
from app.db import get_conn, now_ts
from app.llm_client import LLMError, chat_completion
from app.rag import RetrievedChunk, _stem, search_knowledge_base, tokenize


@dataclass
class AgentResult:
    answer: str
    escalated: bool
    escalation_id: int | None
    escalation_reason: str
    intent: str
    confidence: float
    is_repeat: bool
    repeat_cluster_hits: int
    sources: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_rub: float
    cache_hit: bool
    latency_ms: int


def _normalize_q(q: str) -> str:
    tokens = [_stem(t) for t in tokenize(q)]
    return " ".join(sorted(set(tokens))) if tokens else re.sub(r"\s+", " ", q.strip().lower())


def _detect_and_record_repeat(question: str) -> tuple[int, bool, int]:
    norm = _normalize_q(question)
    q_set = set(norm.split())
    ts = now_ts()

    with get_conn() as conn:
        rows = conn.execute("SELECT cluster_id, normalized_q, canonical_q, hit_count FROM faq_clusters").fetchall()
        matched_id = None
        matched_hits = 1
        for r in rows:
            if r["normalized_q"] == norm:
                matched_id = r["cluster_id"]
                matched_hits = r["hit_count"] + 1
                break
            c_set = set(r["normalized_q"].split())
            if q_set and c_set:
                inter = len(q_set & c_set)
                jacc = inter / len(q_set | c_set)
                overlap = inter / min(len(q_set), len(c_set))
                if jacc >= 0.55 or (min(len(q_set), len(c_set)) >= 4 and overlap >= 0.80):
                    matched_id = r["cluster_id"]
                    matched_hits = r["hit_count"] + 1
                    break

        if matched_id is not None:
            conn.execute(
                "UPDATE faq_clusters SET hit_count=?, last_asked_at=? WHERE cluster_id=?",
                (matched_hits, ts, matched_id),
            )
            return matched_id, True, matched_hits
        else:
            cur = conn.execute(
                "INSERT INTO faq_clusters(normalized_q, canonical_q, hit_count, last_asked_at) VALUES(?,?,1,?)",
                (norm, question.strip(), ts),
            )
            return int(cur.lastrowid), False, 1


def _plan_intent(question: str) -> tuple[str, str | None]:
    ql = question.lower()
    # Explicit PM-only business rules from FAQ
    if any(k in ql for k in ("тендер", "44-фз", "223-фз", "кастомн", "индивидуальн")):
        return "pm_rule_escalation", "Требуется согласование индивидуальных условий / тендера (44-ФЗ/223-ФЗ) с продакт-менеджером."
    if re.search(r"\b(1[0-9]|[2-9][0-9])\s*(шт|штук|единиц|ноутбук|монитор|пылесос)", ql) and any(
        w in ql for w in ("скидк", "опт", "кп", "коммерческ", "парти")
    ):
        return "pm_rule_escalation", "Запрос оптовой скидки на партию от 10 единиц — согласует продакт-менеджер."
    if any(k in ql for k in ("сравни", "отлич", "что лучше", "разниц", "vs")):
        return "compare", None
    if any(k in ql for k in ("достав", "оплат", "сбп", "рассроч", "возврат", "эдо", "подмен", "b2b", "юрлиц", "приоритетн")):
        return "policy_faq", None
    if " или " in ql and any(k in ql for k in ("pro", "air", "lite", "max", "мини", "макс", "ax3000", "ax5400", "27q", "34 ")):
        return "compare", None
    return "product_qa", None


def _get_product_cards(skus: list[str]) -> list[dict[str, Any]]:
    if not skus:
        return []
    placeholders = ",".join("?" for _ in skus)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM products WHERE sku IN ({placeholders})", skus).fetchall()
    by_sku = {r["sku"]: dict(r) for r in rows}
    return [by_sku[s] for s in skus if s in by_sku]


def _create_escalation(question: str, reason: str, related_sku: str, manager_name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO pm_escalations(question, reason, related_sku, manager_name, status, created_at)
            VALUES(?, ?, ?, ?, 'open', ?)
            """,
            (question.strip(), reason, related_sku, manager_name, now_ts()),
        )
        return int(cur.lastrowid)


def _deterministic_fallback_answer(question: str, chunks: list[RetrievedChunk], intent: str) -> str:
    lines = ["Ответ сформирован из базы знаний (ДЕМО-ДАННЫЕ, локальный режим без LLM):"]
    for ch in chunks[:3]:
        snippet = ch.content.strip().replace("\n", " | ")
        if len(snippet) > 420:
            snippet = snippet[:420] + "…"
        lines.append(f"• [{ch.source_id}] {ch.source_title}: {snippet}")
    return "\n".join(lines)


def run_agent(
    question: str,
    session_id: str = "web-default",
    channel: str = "web",
    manager_name: str = "Менеджер по продажам",
) -> AgentResult:
    t0 = time.time()
    trace: list[dict[str, Any]] = []
    settings = get_settings()

    # Step 1: Repeat detection
    cluster_id, is_repeat, repeat_hits = _detect_and_record_repeat(question)
    trace.append({
        "step": "1_repeat_detector",
        "is_repeat": is_repeat,
        "cluster_id": cluster_id,
        "cluster_hits": repeat_hits,
    })

    # Step 2: Planner
    intent, rule_escalation = _plan_intent(question)
    trace.append({
        "step": "2_planner",
        "intent": intent,
        "rule_escalation": rule_escalation,
    })

    # Step 3: Tool execution (RAG search)
    if intent == "compare":
        chunks = search_knowledge_base(question, top_k=4, doc_type_filter="product")
        # Keep top 2-3 products for structured comparison
        skus = [c.source_id for c in chunks[:3] if c.score >= 0.18]
        cards = _get_product_cards(skus)
        trace.append({
            "step": "3_tool_compare_products",
            "skus_retrieved": skus,
            "scores": [c.score for c in chunks[:3]],
        })
    else:
        chunks = search_knowledge_base(question, top_k=5)
        product_skus = [c.source_id for c in chunks if c.doc_type == "product"][:3]
        cards = _get_product_cards(product_skus)
        trace.append({
            "step": "3_tool_search_kb",
            "chunks_count": len(chunks),
            "top_scores": [{"id": c.source_id, "score": c.score} for c in chunks[:3]],
        })

    top_score = chunks[0].score if chunks else 0.0
    related_sku = next((c.source_id for c in chunks if c.doc_type == "product"), "")

    # Step 4: Check escalation triggers before LLM
    ql = question.lower()
    out_of_stock_date_q = (
        any(c["in_stock"] == 0 for c in cards[:1])
        and any(w in ql for w in ("когда", "поставк", "появит", "ожидает", "будет в наличии", "предзаказ"))
    )

    if rule_escalation:
        esc_id = _create_escalation(question, rule_escalation, related_sku, manager_name)
        answer = (
            f"Не знаю — передаю продакт-менеджеру (обращение #PM-{esc_id}).\n"
            f"Причина: {rule_escalation}"
        )
        trace.append({"step": "4_confidence_gate", "decision": "escalate_rule", "escalation_id": esc_id})
        latency = int((time.time() - t0) * 1000)
        _save_chat_log(
            session_id, channel, question, _normalize_q(question), cluster_id, is_repeat,
            intent, answer, True, top_score, [], trace, settings.provider, settings.model,
            0, 0, 0.0, False, latency
        )
        return AgentResult(
            answer=answer, escalated=True, escalation_id=esc_id, escalation_reason=rule_escalation,
            intent=intent, confidence=top_score, is_repeat=is_repeat, repeat_cluster_hits=repeat_hits,
            sources=[], trace=trace, provider=settings.provider, model=settings.model,
            tokens_in=0, tokens_out=0, cost_rub=0.0, cache_hit=False, latency_ms=latency,
        )

    if out_of_stock_date_q:
        reason = f"Товар {cards[0]['sku']} ({cards[0]['name']}) отсутствует на складе; точная дата поставки не указана в базе знаний."
        esc_id = _create_escalation(question, reason, cards[0]["sku"], manager_name)
        answer = (
            f"Не знаю — передаю продакт-менеджеру (обращение #PM-{esc_id}).\n"
            f"По карточке [{cards[0]['sku']}] товар сейчас «Нет в наличии», а сроков новой поставки в базе знаний нет."
        )
        trace.append({"step": "4_confidence_gate", "decision": "escalate_oos_date", "escalation_id": esc_id})
        latency = int((time.time() - t0) * 1000)
        sources_out = [{"source_id": cards[0]["sku"], "source_title": cards[0]["name"], "source_file": cards[0]["source_file"], "score": top_score}]
        _save_chat_log(
            session_id, channel, question, _normalize_q(question), cluster_id, is_repeat,
            intent, answer, True, top_score, sources_out, trace, settings.provider, settings.model,
            0, 0, 0.0, False, latency
        )
        return AgentResult(
            answer=answer, escalated=True, escalation_id=esc_id, escalation_reason=reason,
            intent=intent, confidence=top_score, is_repeat=is_repeat, repeat_cluster_hits=repeat_hits,
            sources=sources_out, trace=trace, provider=settings.provider, model=settings.model,
            tokens_in=0, tokens_out=0, cost_rub=0.0, cache_hit=False, latency_ms=latency,
        )

    # Retrieval confidence threshold
    if top_score < 0.27 or not chunks:
        reason = f"Низкая релевантность найденных документов (score={top_score:.2f} < 0.27) — точных данных в базе знаний нет."
        esc_id = _create_escalation(question, reason, related_sku, manager_name)
        answer = (
            f"Не знаю — передаю продакт-менеджеру (обращение #PM-{esc_id}).\n"
            f"В базе знаний (ДЕМО-ДАННЫЕ) не найдено подтверждённой информации для ответа на этот вопрос."
        )
        trace.append({"step": "4_confidence_gate", "decision": "escalate_low_score", "top_score": top_score, "escalation_id": esc_id})
        latency = int((time.time() - t0) * 1000)
        _save_chat_log(
            session_id, channel, question, _normalize_q(question), cluster_id, is_repeat,
            intent, answer, True, top_score, [], trace, settings.provider, settings.model,
            0, 0, 0.0, False, latency
        )
        return AgentResult(
            answer=answer, escalated=True, escalation_id=esc_id, escalation_reason=reason,
            intent=intent, confidence=top_score, is_repeat=is_repeat, repeat_cluster_hits=repeat_hits,
            sources=[], trace=trace, provider=settings.provider, model=settings.model,
            tokens_in=0, tokens_out=0, cost_rub=0.0, cache_hit=False, latency_ms=latency,
        )

    # Step 5: Grounded LLM Synthesis
    relevant_chunks = [c for c in chunks if c.score >= max(0.22, top_score * 0.45)][:4]
    context_blocks = []
    for idx, ch in enumerate(relevant_chunks, 1):
        context_blocks.append(
            f"[Фрагмент {idx} | ID: {ch.source_id} | Источник: {ch.source_title} | Файл: {ch.source_file} | score={ch.score}]\n{ch.content}"
        )
    context_text = "\n\n---\n\n".join(context_blocks)

    sys_prompt = (
        "Ты — ИИ-агент помощник продакт-менеджера интернет-магазина «ТехноВитрина» (все товары и условия — ДЕМО-ДАННЫЕ). "
        "Твоя задача — помогать менеджеру по продажам отвечать клиенту быстро и строго по базе знаний.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай ТОЛЬКО фактами из переданных фрагментов базы знаний. Не додумывай характеристики, цены, сроки поставок и совместимость.\n"
        "2. Обязательно указывай ссылки на источники в формате [Источник: <ID>].\n"
        "3. Если спрашивают сравнение — сделай краткое структурированное сравнение по ключевым параметрам и ценам.\n"
        "4. Если в переданных фрагментах НЕТ точного ответа на вопрос (например, товар другой категории, нестандартная совместимость, отсутствующий параметр), "
        "НЕ гадай, а начни ответ строго со строки: ESCALATE: <краткая причина по-русски>.\n"
        "5. Пиши по-русски, кратко и деловито, чтобы менеджер мог сразу использовать ответ в разговоре с клиентом."
    )
    user_prompt = f"Вопрос менеджера/клиента: {question}\n\nБаза знаний:\n{context_text}"

    tin = tout = 0
    cost = 0.0
    cache_hit = False
    used_provider = settings.provider
    used_model = settings.model

    try:
        llm_res = chat_completion(
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=700,
            temperature=0.1,
            use_cache=True,
            settings=settings,
        )
        raw_answer = llm_res.text
        tin, tout, cost, cache_hit = llm_res.tokens_in, llm_res.tokens_out, llm_res.cost_rub, llm_res.cache_hit
        used_provider, used_model = llm_res.provider, llm_res.model
        trace.append({
            "step": "5_llm_synthesis",
            "provider": used_provider,
            "model": used_model,
            "cache_hit": cache_hit,
            "tokens_in": tin,
            "tokens_out": tout,
            "cost_rub": cost,
        })
    except LLMError as exc:
        raw_answer = _deterministic_fallback_answer(question, relevant_chunks, intent)
        used_provider = "fallback"
        used_model = f"deterministic ({exc})"
        trace.append({"step": "5_llm_synthesis_fallback", "error": str(exc)})

    # Check if LLM itself triggered ESCALATE:
    if raw_answer.strip().upper().startswith("ESCALATE:"):
        reason = raw_answer.split(":", 1)[1].strip() or "Модель определила недостаток фактов в базе знаний."
        esc_id = _create_escalation(question, reason, related_sku, manager_name)
        final_answer = (
            f"Не знаю — передаю продакт-менеджеру (обращение #PM-{esc_id}).\n"
            f"Причина: {reason}"
        )
        escalated = True
        trace.append({"step": "6_llm_escalation_gate", "decision": "escalate_by_llm", "escalation_id": esc_id})
    else:
        final_answer = raw_answer
        escalated = False
        esc_id = None
        reason = ""
        trace.append({"step": "6_final_answer", "decision": "answered", "citations_count": len(relevant_chunks)})

    sources_out = [
        {
            "source_id": c.source_id,
            "source_title": c.source_title,
            "source_file": c.source_file,
            "section": c.section,
            "score": c.score,
        }
        for c in relevant_chunks
    ]
    latency = int((time.time() - t0) * 1000)

    _save_chat_log(
        session_id, channel, question, _normalize_q(question), cluster_id, is_repeat,
        intent, final_answer, escalated, top_score, sources_out, trace, used_provider, used_model,
        tin, tout, cost, cache_hit, latency
    )

    return AgentResult(
        answer=final_answer,
        escalated=escalated,
        escalation_id=esc_id,
        escalation_reason=reason,
        intent=intent,
        confidence=top_score,
        is_repeat=is_repeat,
        repeat_cluster_hits=repeat_hits,
        sources=sources_out,
        trace=trace,
        provider=used_provider,
        model=used_model,
        tokens_in=tin,
        tokens_out=tout,
        cost_rub=cost,
        cache_hit=cache_hit,
        latency_ms=latency,
    )


def _save_chat_log(
    session_id: str,
    channel: str,
    question: str,
    normalized_q: str,
    cluster_id: int | None,
    is_repeat: bool,
    intent: str,
    answer: str,
    escalated: bool,
    confidence: float,
    sources: list[dict[str, Any]],
    trace: list[dict[str, Any]],
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_rub: float,
    cache_hit: bool,
    latency_ms: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_logs(
                session_id, channel, question, normalized_q, cluster_id, is_repeat,
                intent, answer, escalated, confidence, sources_json, trace_json,
                provider, model, tokens_in, tokens_out, cost_rub, cache_hit, latency_ms, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id, channel, question, normalized_q, cluster_id, int(is_repeat),
                intent, answer, int(escalated), confidence,
                json.dumps(sources, ensure_ascii=False),
                json.dumps(trace, ensure_ascii=False),
                provider, model, tokens_in, tokens_out, cost_rub, int(cache_hit), latency_ms, now_ts(),
            ),
        )
