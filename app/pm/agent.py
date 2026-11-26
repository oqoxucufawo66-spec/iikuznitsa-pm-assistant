"""Агент ПМ: маршрутизация вопроса → детерминированные инструменты → LLM пишет ответ только по фактам контекста."""
from __future__ import annotations
import json
import re
import time
from typing import Any

from app.db import get_conn, now_ts
from app.llm_client import LLMError, chat_completion
from app.pm import actions, analysis, effect
from app.pm.settings import load_config
from app.rag import search_knowledge_base

SYSTEM = (
    "Ты — ИИ-помощник продукт-менеджера интернет-магазина store.softline.ru. Отвечай по-русски, кратко и по делу. "
    "Используй ТОЛЬКО факты и числа из блока КОНТЕКСТ (их посчитал код по данным Метрики, продаж, релизов, бэклога, исследований и конкурентного анализа). "
    "Не придумывай числа и причины. Продажи даны относительным ИНДЕКСОМ заказов, а не штуками и не рублями — так и пиши. Всегда указывай ограничения данных, если они есть в контексте. Если метрика помечена «не измеряется» — не утверждай, что она упала или выросла: говори, что этот отрезок не измеряется и это гипотеза. "
    "Различай: сигнал, гипотеза, проверка, критерий успеха. Ссылайся на источники в квадратных скобках, например [Продажи], [ID релиза], [ID задачи], [Интервью: ...]. "
    "Если данных нет — прямо скажи, что внешними источниками не подтверждено."
)

IMPACT = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}


def _family_from_text(q: str) -> str | None:
    ql = q.lower()
    for fam, c in load_config()["families"].items():
        if any(s in ql for s in c.get("synonyms", [])) or fam.lower() in ql:
            return fam
    return None


def scan() -> dict[str, Any]:
    """Регулярный проактивный проход: все семейства + пост-релизный мониторинг."""
    out = {"generated_at": now_ts(), "signals": [], "no_signal": []}
    for fam in analysis.families():
        rep = analysis.analyze_family(fam)
        if rep.get("signal"):
            out["signals"].append({"family": fam, "short": rep["short"], "conclusion": rep["conclusion"],
                                   "change_pct": rep["signal"]["change_pct"], "sources_confirm": rep["sources_confirm"],
                                   "sources_total": rep["sources_total"]})
        else:
            out["no_signal"].append(fam)
    out["release_effects"] = analysis.release_effects()
    return out


def rank_backlog() -> list[dict[str, Any]]:
    """Приоритизация: влияние связанных метрик × подтверждение активным сигналом (не по одному сигналу)."""
    ref = analysis.metrics_ref()
    signals = {}
    for fam in analysis.families():
        rep = analysis.analyze_family(fam)
        if rep.get("signal"):
            for t in rep["backlog"]:
                signals[t["task_id"]] = rep
            signals.update({f"AI:{a['title']}": rep for a in rep["proposed_actions"] if a["kind"] == "new_task"})
    out = []
    for t in analysis.backlog():
        if t["status"] in ("Done", "Closed"):
            continue
        mids = [m.strip() for m in (t["linked_metric_ids"] or "").split(",") if m.strip()]
        impact = max([IMPACT.get(ref.get(m, {}).get("priority_impact", ""), 4 if m == load_config()["orders_metric_id"] else 1) for m in mids] or [1])
        rep = signals.get(t["task_id"]) or signals.get(f"AI:{t['title']}")
        conf = rep["sources_confirm"] if rep else 0
        score = impact * (1 + conf)
        out.append({"task_id": t["task_id"], "title": t["title"], "status": t["status"], "priority": t["priority"],
                    "tags": t["tags"], "ai_generated": bool(t["ai_generated"]), "impact": impact, "confirming_sources": conf,
                    "score": score, "why": (f"связана с активным сигналом ({rep['short']}, {conf}/{rep['sources_total']} источников)" if rep else "активного сигнала нет")})
    out.sort(key=lambda x: -x["score"])
    return out


def _intent(q: str) -> str:
    ql = q.lower()
    if re.search(r"эффект|после релиз|после исправ|закры|вернул", ql):
        return "effect"
    if re.search(r"бэклог|приоритет|задач", ql) and not re.search(r"почему|упал|падени", ql):
        return "backlog"
    if re.search(r"конкурент|цен[аыу]|дешевл|дорож|положени|позици", ql):
        return "competitive"
    if re.search(r"упал|падени|сигнал|почему|просел|заказ|аномал|что случилось|провал", ql):
        return "signal"
    return "research"


def _compact_report(rep: dict) -> dict:
    keep = ["family", "conclusion", "signal", "control", "sources", "hypotheses", "success_criteria", "data_gaps"]
    c = {k: rep.get(k) for k in keep}
    c["releases"] = [{k: r[k] for k in ("release_id", "release_date", "release_type", "description", "verdict", "why")} for r in rep.get("releases", [])]
    c["funnel"] = [{k: f.get(k) for k in ("metric_id", "name", "status", "before_avg", "after_avg", "change_pct", "note")} for f in rep.get("funnel", [])]
    c["backlog"] = [{k: t.get(k) for k in ("task_id", "title", "status", "priority", "reasons", "proposal")} for t in rep.get("backlog", [])]
    c["research_quotes"] = [x["source"] + ": " + x["text"][:300] for x in rep["research"]["problem_mentions"][:3]]
    c["competitive_facts"] = rep.get("competitive_facts")
    return c


def ask(question: str, session_id: str = "pm-web") -> dict[str, Any]:
    t0 = time.time()
    intent = _intent(question)
    fam = _family_from_text(question)
    trace = [{"step": "intent", "value": intent}, {"step": "family", "value": fam}]
    ctx: dict[str, Any] = {}
    report = None
    if intent == "signal":
        fams = [fam] if fam else analysis.families()
        reps = [analysis.analyze_family(f) for f in fams]
        report = next((r for r in reps if r.get("signal")), None)
        ctx["reports"] = [_compact_report(r) if r.get("signal") else {"family": r["family"], "message": r.get("message")} for r in reps]
        trace.append({"step": "tools", "value": "detect_sales_signal → control → releases → funnel → backlog → research → competitive"})
    elif intent == "effect":
        fam = fam or next((f for f in analysis.families() if analysis.detect_sales_signal(f)), None) or (analysis.families() or [""])[0]
        m = re.search(r"(R-\d+)", question.upper())
        rel = next((r for r in analysis.releases() if m and r["release_id"] == m.group(1)), None)
        if rel:
            ctx["effect"] = effect.measure_effect(fam, rel["release_date"])
        ctx["release_effects"] = [e for e in analysis.release_effects() if not m or e["release_id"] == m.group(1)][:30]
        trace.append({"step": "tools", "value": "measure_effect + release_effects"})
    elif intent == "backlog":
        ctx["ranked_backlog"] = rank_backlog()[:10]
        ctx["proposed_changes"] = actions.list_changes()[:10]
        trace.append({"step": "tools", "value": "rank_backlog"})
    elif intent == "competitive":
        fcfg = load_config()["families"].get(fam or "", {"synonyms": [w for w in re.findall(r"[a-zа-яё]{4,}", question.lower())][:3]})
        ctx["competitive_facts"] = analysis.competitive_facts(fcfg) if fam else analysis.competitive_facts({"synonyms": ["@@none@@"]})
    chunks = search_knowledge_base(question, top_k=6, use_vector=False)
    chunks = [c for c in chunks if c.doc_type in ("research", "competitive")] or chunks
    ctx["documents"] = [{"source": c.source_id, "text": c.content[:900]} for c in chunks[:5] if c.score > 0.05]
    trace.append({"step": "retrieval", "value": [c.source_id for c in chunks[:5]]})

    answer, provider, model, tin, tout, cost, cache = "", "mock", "", 0, 0, 0.0, False
    try:
        r = chat_completion([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"ВОПРОС ПМ: {question}\n\nКОНТЕКСТ:\n{json.dumps(ctx, ensure_ascii=False, default=str)[:14000]}"},
        ], max_tokens=900, temperature=0.1)
        answer, provider, model, tin, tout, cost, cache = r.text, r.provider, r.model, r.tokens_in, r.tokens_out, r.cost_rub, r.cache_hit
    except LLMError as exc:
        trace.append({"step": "llm", "value": f"недоступна ({exc}) — детерминированный ответ"})
        if report:
            answer = actions.compose_message(report)["body"]
        elif "effect" in ctx:
            e = ctx["effect"]; answer = f"{e['verdict']}: {e['decision']}"
        elif "ranked_backlog" in ctx:
            answer = "\n".join(f"{x['task_id']} {x['title']} — score {x['score']} ({x['why']})" for x in ctx["ranked_backlog"])
        else:
            answer = "\n\n".join(f"[{d['source']}] {d['text'][:400]}" for d in ctx["documents"]) or "Данных по вопросу не найдено."
    latency = int((time.time() - t0) * 1000)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_logs(session_id, channel, question, normalized_q, intent, answer, sources_json, trace_json, provider, model, tokens_in, tokens_out, cost_rub, cache_hit, latency_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, "pm", question, question.lower().strip(), intent, answer, json.dumps([d["source"] for d in ctx["documents"]], ensure_ascii=False),
             json.dumps(trace, ensure_ascii=False, default=str), provider, model, tin, tout, cost, int(cache), latency, now_ts()))
    return {"answer": answer, "intent": intent, "family": fam, "trace": trace, "sources": [d["source"] for d in ctx["documents"]],
            "report": _compact_report(report) if report else None, "provider": provider, "model": model,
            "tokens_in": tin, "tokens_out": tout, "cost_rub": cost, "cache_hit": cache, "latency_ms": latency}
