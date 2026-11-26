"""Детерминированный аналитический движок: сигнал → контроль → релизы → воронка → бэклог → исследования/конкуренты.

Все числа считаются кодом по данным (LLM их не придумывает). LLM только пишет человеческий текст поверх отчёта.
"""
from __future__ import annotations
import datetime as dt
import json
import re
from statistics import mean
from typing import Any

from app.db import get_conn
from app.pm.settings import load_config


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _fmt(d: dt.date | str) -> str:
    d = _d(d) if isinstance(d, str) else d
    return d.strftime("%d.%m.%Y")


def _pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100.0, 1) if b else None


# ---------- продажи ----------
def sales_series(family: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pm_sales WHERE product_family=? ORDER BY week_start", (family,)).fetchall()
    out = [dict(r) for r in rows]
    w = load_config()["sales"]["rolling_weeks"]
    for i, r in enumerate(out):
        win = [x["orders_index"] for x in out[max(0, i - w + 1): i + 1] if x["orders_index"] is not None]
        r["rolling"] = round(mean(win), 1) if len(win) == w else None
        r["incomplete"] = "неполн" in (r.get("comment") or "").lower()
    return out


def families() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT product_family FROM pm_sales ORDER BY product_family").fetchall()]


def _window_means(series: list[dict], split_idx: int, cfg: dict) -> tuple[list[dict], list[dict]]:
    g = cfg["guard_weeks"]
    before = series[max(0, split_idx - g - cfg["baseline_weeks"]): max(0, split_idx - g)]
    after = series[split_idx:]
    return before, after


def detect_sales_signal(family: str) -> dict[str, Any] | None:
    """Ищем устойчивое отклонение: точка разлома, после которой среднее держится ниже базы (по скользящему, а не по одной неделе)."""
    cfg = load_config()["sales"]
    s = sales_series(family)
    if len(s) < cfg["baseline_weeks"] + cfg["min_weeks_after"] + cfg["guard_weeks"]:
        return None
    best = None
    for i in range(cfg["baseline_weeks"] + cfg["guard_weeks"], len(s) - cfg["min_weeks_after"] + 1):
        before, after = _window_means(s, i, cfg)
        if len(before) < 6:
            continue
        mb = mean(x["orders_index"] for x in before)
        ma = mean(x["orders_index"] for x in after)
        ch = _pct(ma, mb)
        if ch is None:
            continue
        # устойчивость: скользящее после разлома ниже базы N недель подряд
        roll_after = [x["rolling"] for x in after if x["rolling"] is not None]
        sustained = sum(1 for v in roll_after if v < mb * (1 - cfg["drop_threshold_pct"] / 100.0))
        score = -ch * (len(after) ** 0.5) if ch < 0 else 0
        if ch <= -cfg["drop_threshold_pct"] and sustained >= 3 and (best is None or score > best["score"]):
            best = {"split": i, "score": score, "before": before, "after": after, "mb": mb, "ma": ma, "ch": ch, "sustained": sustained}
    abs_rule = []
    run = 0
    for x in s:
        run = run + 1 if (x["rolling"] is not None and x["rolling"] < cfg["abs_index_threshold"]) else 0
        if run >= cfg["abs_consecutive_weeks"]:
            abs_rule.append(x["week_start"])
    if not best:
        return None
    sp = best["split"]
    guard = s[sp - cfg["guard_weeks"]: sp]
    roll = [x for x in s if x["rolling"] is not None]
    peak = max(roll, key=lambda x: x["rolling"]) if roll else None
    falling_weeks = 0
    for a, b in zip(roll[::-1][1:], roll[::-1]):
        pass
    # недели подряд снижения скользящего после пика
    if peak:
        pi = roll.index(peak)
        for j in range(pi + 1, len(roll)):
            if roll[j]["rolling"] < roll[j - 1]["rolling"]:
                falling_weeks += 1
            else:
                break
    return {
        "family": family,
        "onset_week": s[sp]["week_start"],
        "baseline_from": best["before"][0]["week_start"],
        "baseline_to": best["before"][-1]["week_start"],
        "after_from": best["after"][0]["week_start"],
        "after_to": best["after"][-1]["week_start"],
        "baseline_mean": round(best["mb"], 1),
        "after_mean": round(best["ma"], 1),
        "change_pct": best["ch"],
        "excluded_guard_weeks": [{"week_start": g["week_start"], "orders_index": g["orders_index"], "comment": g.get("comment") or ""} for g in guard],
        "rolling_peak": {"week_start": peak["week_start"], "value": peak["rolling"]} if peak else None,
        "rolling_last": {"week_start": roll[-1]["week_start"], "value": roll[-1]["rolling"]} if roll else None,
        "rolling_falling_weeks_after_peak": falling_weeks,
        "abs_rule_weeks_below_threshold": abs_rule,
        "last_week_incomplete": bool(s[-1]["incomplete"]),
        "weeks_after": len(best["after"]),
    }


def control_change(control: str, sig: dict) -> dict[str, Any]:
    s = {x["week_start"]: x for x in sales_series(control)}
    def m(a, b):
        vals = [x["orders_index"] for k, x in s.items() if a <= k <= b and x["orders_index"] is not None]
        return mean(vals) if vals else None
    mb, ma = m(sig["baseline_from"], sig["baseline_to"]), m(sig["after_from"], sig["after_to"])
    ch = _pct(ma, mb) if (ma is not None and mb) else None
    lim = load_config()["sales"]["control_max_abs_change_pct"]
    return {"family": control, "baseline_mean": round(mb, 1) if mb else None, "after_mean": round(ma, 1) if ma else None,
            "change_pct": ch, "stable": ch is not None and abs(ch) <= lim, "threshold_pct": lim,
            "did_pct": round(sig["change_pct"] - ch, 1) if ch is not None else None}


# ---------- релизы ----------
def releases() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pm_releases ORDER BY release_date").fetchall()]


def classify_releases(sig: dict, family_cfg: dict, control_stable: bool) -> list[dict[str, Any]]:
    cfg = load_config()
    sc = cfg["sales"]
    onset = _d(sig["onset_week"])
    lo = onset - dt.timedelta(days=sc["release_window_days_before"])
    hi = onset + dt.timedelta(days=sc["release_window_days_after"])
    syn = [x.lower() for x in family_cfg.get("synonyms", [])]
    out = []
    for r in releases():
        rd = _d(r["release_date"])
        if not (lo <= rd <= hi):
            continue
        desc = (r["description"] or "").lower()
        mentions = any(x in desc for x in syn)
        affects_orders = cfg["orders_metric_id"] in (r["affected_metrics"] or "")
        rtype = r["release_type"]
        rt = cfg["release_types"]
        offscope_kw = [k.lower() for k in cfg.get("offscope_keywords", [])]
        if rtype in rt["offscope"] or (any(k in desc for k in offscope_kw) and not mentions and rtype not in rt["global"]):
            verdict, why = "отсеян", "касается другого контура (ЛК/рассылка), а не витрины семейства"
        elif rtype in rt["global"] or (not mentions and rtype not in rt["product"]):
            verdict = "фактор"
            why = ("затронул весь магазин; контрольное семейство не просело → не основное объяснение, но в списке факторов"
                   if control_stable else "затронул весь магазин; контроль тоже изменился → может объяснять падение")
        elif mentions or affects_orders:
            verdict, why = "главный кандидат", "релиз именно этого семейства, в окне падения, затрагивает заказы семейства"
        else:
            verdict, why = "фактор", "в окне падения"
        out.append({**r, "days_from_onset": (rd - onset).days, "verdict": verdict, "why": why})
    order = {"главный кандидат": 0, "фактор": 1, "отсеян": 2}
    out.sort(key=lambda x: (order[x["verdict"]], x["release_date"]))
    return out


# ---------- метрики ----------
def metric_weekly(metric_id: str, product: str) -> list[dict[str, Any]]:
    """Недельные агрегаты: счётчики суммируем, доли — взвешенно по sample_size."""
    with get_conn() as conn:
        rows = conn.execute("SELECT date, segment, value, unit, sample_size, detail_json FROM pm_metrics WHERE metric_id=? AND product=? ORDER BY date",
                            (metric_id, product)).fetchall()
    weeks: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _d(r["date"])
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"week_start": wk, "sum": 0.0, "wsum": 0.0, "n": 0.0, "unit": r["unit"], "days": set()})
        v = r["value"] or 0.0
        n = r["sample_size"] or 0.0
        w["days"].add(r["date"])
        if r["unit"] in ("ratio",):
            w["wsum"] += v * n
            w["n"] += n
        else:
            w["sum"] += v
            w["n"] += n
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        val = (w["wsum"] / w["n"]) if w["unit"] == "ratio" and w["n"] else w["sum"]
        out.append({"week_start": wk, "value": round(val, 4 if w["unit"] == "ratio" else 1), "unit": w["unit"], "sample": w["n"], "days": len(w["days"])})
    return out


def metrics_ref() -> dict[str, dict]:
    with get_conn() as conn:
        return {r["metric_id"]: dict(r) for r in conn.execute("SELECT * FROM pm_metrics_ref").fetchall()}


def funnel_check(family_cfg: dict, sig: dict) -> list[dict[str, Any]]:
    cfg = load_config()
    prod = family_cfg.get("metrics_product", "")
    ref = metrics_ref()
    out = []
    onset = sig["onset_week"]
    for mid in cfg["funnel_metrics"]:
        wk = metric_weekly(mid, prod)
        r = ref.get(mid, {})
        item = {"metric_id": mid, "name": r.get("metric_name", mid), "priority_impact": r.get("priority_impact", ""),
                "bad_signal": r.get("bad_signal", ""), "status": "", "note": ""}
        if not wk:
            item["status"] = "не измеряется"
            note = (r.get("comment") or "")
            syn = [x.lower() for x in family_cfg.get("synonyms", [])]
            m = next((re.search(re.escape(sent) + r"\.?", note) for sent in re.split(r"(?<=\.)\s+", note) if any(x in sent.lower() for x in syn)), None)
            item["note"] = (m.group(0) if m else note[:220]) + " Отсутствие данных ≠ ноль: вывод о конверсии не делаем."
            out.append(item)
            continue
        full = [x for x in wk if x["days"] >= 5]
        before = [x for x in full if sig["baseline_from"] <= x["week_start"] < onset]
        after = [x for x in full if x["week_start"] >= onset]
        avg = lambda xs: round(mean(x["value"] for x in xs), 4 if wk[0]["unit"] == "ratio" else 1) if xs else None
        item.update({"unit": wk[0]["unit"], "data_from": wk[0]["week_start"], "before_avg": avg(before), "after_avg": avg(after),
                     "weeks_before": len(before), "weeks_after": len(after)})
        if before and after:
            item["change_pct"] = _pct(item["after_avg"], item["before_avg"])
            item["status"] = cfg.get("funnel_notes", {}).get(mid, "сопоставимо")
        elif after and not before:
            item["status"] = f"данные только после релиза (с {_fmt(wk[0]['week_start'])}) — до/после не сравнить"
        out.append(item)
    ds = cfg.get("derived_share")
    if ds:
        nums = {x["week_start"]: x["value"] for x in metric_weekly(ds["numerator"], prod)}
        dens = {x["week_start"]: x["value"] for x in metric_weekly(ds["denominator"], prod)}
        shares = [nums[w] / dens[w] for w in nums if w in dens and dens[w] and w >= onset]
        if shares:
            out.append({"metric_id": ds["id"], "name": ds["name"],
                        "status": "расчёт", "after_avg": round(mean(shares), 3), "min": round(min(shares), 3), "max": round(max(shares), 3),
                        "total_after": round(sum(nums[w] for w in nums if w >= onset)), "unit": "ratio", "note": ds.get("note", "")})
    return out


# ---------- бэклог ----------
def backlog(include_ai: bool = True) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pm_backlog " + ("" if include_ai else "WHERE ai_generated=0") + " ORDER BY ai_generated, task_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["details"] = json.loads(d.pop("details_json") or "{}")
        out.append(d)
    return out


def related_backlog(candidates: list[dict], family_cfg: dict) -> list[dict[str, Any]]:
    syn = [x.lower() for x in family_cfg.get("synonyms", [])]
    cand_ids = {c["release_id"] for c in candidates if c["verdict"] == "главный кандидат"}
    cfg = load_config()
    funnel = set(cfg["funnel_metrics"]) | {cfg["orders_metric_id"]}
    done = cfg["backlog"]["done_statuses"]
    out = []
    for t in backlog(include_ai=False):
        text = f"{t['title']} {t['description']}".lower()
        reasons = []
        if t["release_id"] and any(x.strip() in cand_ids for x in t["release_id"].split(",")):
            reasons.append(f"выпущена в релизе-кандидате {t['release_id']}")
        mentions = any(x in text for x in syn)
        if t["status"] not in done and (mentions or set(m.strip() for m in (t["linked_metric_ids"] or "").split(",")) & funnel):
            reasons.append("открыта, связана с метриками воронки/семейством")
        if reasons:
            proposal = ""
            if t["status"] not in done and t["priority"] in cfg["backlog"]["raise_from_priorities"]:
                proposal = "повысить приоритет до High"
            elif t["status"] in done:
                proposal = "держать в фокусе: регресс может быть следствием этой задачи"
            out.append({**t, "reasons": reasons, "proposal": proposal})
    return out


# ---------- исследования и конкуренты ----------
def evidence_search(family_cfg: dict, doc_type: str, limit: int = 6) -> dict[str, Any]:
    cfg = load_config()
    syn = [x.lower() for x in family_cfg.get("synonyms", [])]
    topics = [t.lower() for t in cfg["research_topics"]]
    with get_conn() as conn:
        rows = conn.execute("SELECT source_id, source_file, section, content FROM chunks WHERE doc_type=?", (doc_type,)).fetchall()
    hits_product, hits_problem = [], []
    for r in rows:
        low = r["content"].lower()
        if not any(x in low for x in syn):
            continue
        # предложения/ячейки, где семейство упомянуто рядом со сложностью выбора/покупки
        for line in r["content"].split("\n"):
            ll = line.lower()
            if not any(x in ll for x in syn):
                continue
            ans = ll.split(":", 1)[-1]
            prob = [t for t in topics if t in ans]
            item = {"source": r["source_id"], "text": line[:400], "topics": prob}
            hits_product.append(item)
            if prob and re.search(r"сложн|неудоб|непонят|не наш|не смог|проблем|долго|запута|ошиб|геморр", ans):
                hits_problem.append(item)
    return {"doc_type": doc_type, "mentions": len(hits_product), "problem_mentions": hits_problem[:limit],
            "examples": hits_product[:limit]}


def competitive_facts(family_cfg: dict) -> dict[str, Any]:
    syn = [x.lower() for x in family_cfg.get("synonyms", [])]
    with get_conn() as conn:
        prices = [dict(r) for r in conn.execute("SELECT * FROM pm_prices").fetchall()]
        comp = [dict(r) for r in conn.execute("SELECT * FROM pm_competitors ORDER BY weighted_total DESC").fetchall()]
    fam = [p for p in prices if any(x in (p["product"] or "").lower() for x in syn)]
    cheaper_than = [p for p in fam if p["ratio"] and p["ratio"] > 1.0 + 1e-6]
    pricier_than = [p for p in fam if p["ratio"] and p["ratio"] < 1.0 - 1e-6]
    store = next((c for c in comp if c["site"] == "store.softline.ru"), None)
    rank = [c["site"] for c in comp].index("store.softline.ru") + 1 if store else None
    return {
        "price_points": len(fam),
        "store_cheaper_than": sorted({p["site"] for p in cheaper_than}),
        "store_more_expensive_than": sorted({p["site"] for p in pricier_than}),
        "max_gap_pct": round((max(p["ratio"] for p in cheaper_than) - 1) * 100, 1) if cheaper_than else None,
        "store_scores": json.loads(store["scores_json"]) if store else None,
        "store_weighted_rank": rank,
        "store_weighted_total": round(store["weighted_total"], 2) if store and store["weighted_total"] else None,
        "leaders": [{"site": c["site"], "weighted_total": round(c["weighted_total"] or 0, 2)} for c in comp[:4]],
    }


# ---------- разрывы данных ----------
def data_gaps(scopes: list[str], date_from: str, date_to: str) -> list[str]:
    out = []
    for g in load_config()["known_gaps"]:
        if g["scope"] in scopes and not (g["to"] < date_from or g["from"] > date_to):
            out.append(g["text"])
    return out


# ---------- полный отчёт по сигналу ----------
def analyze_family(family: str) -> dict[str, Any]:
    cfg = load_config()
    fcfg = cfg["families"].get(family, {"synonyms": [family.lower()], "metrics_product": "", "control": ""})
    sig = detect_sales_signal(family)
    if not sig:
        s = sales_series(family)
        return {"family": family, "signal": None, "message": "Устойчивого отклонения индекса заказов не найдено", "weeks": len(s)}
    control_name = fcfg.get("control") or next((f for f in families() if f != family), "")
    ctrl = control_change(control_name, sig) if control_name else None
    rels = classify_releases(sig, fcfg, bool(ctrl and ctrl["stable"]))
    funnel = funnel_check(fcfg, sig) if fcfg.get("metrics_product") else []
    bl = related_backlog(rels, fcfg)
    research = evidence_search(fcfg, "research")
    compet = evidence_search(fcfg, "competitive")
    cfacts = competitive_facts(fcfg)
    gaps = data_gaps(["sales", "visits", fcfg.get("metrics_product", "")], sig["baseline_from"], sig["after_to"])
    if sig["last_week_incomplete"]:
        gaps.append("Последняя неделя продаж неполная — в выводах только скользящее среднее")
    main = [r for r in rels if r["verdict"] == "главный кандидат"]
    roles = cfg["roles"]
    views = next((f for f in funnel if f["metric_id"] == roles["traffic_metric"]), None)
    cart = next((f for f in funnel if f["metric_id"] == roles["probe_metric"]), None)
    tx = cfg["texts"]
    traffic_ok = views is not None and views.get("change_pct") is not None and views["change_pct"] > -20

    # перекрёстная проверка источников
    sources = [
        {"source": "Продажи (BI)", "status": "подтверждает",
         "evidence": f"индекс {sig['baseline_mean']} → {sig['after_mean']} ({sig['change_pct']}%), скользящее 4 нед снижается {sig['rolling_falling_weeks_after_peak']} нед подряд"},
        {"source": "Контрольная группа", "status": "подтверждает" if ctrl and ctrl["stable"] else "не подтверждает",
         "evidence": (f"{ctrl['family']}: {ctrl['change_pct']}% за те же недели" if ctrl else "нет контроля")},
        {"source": "Релизы", "status": "подтверждает" if main else "не найдено",
         "evidence": ", ".join(f"{r['release_id']} {_fmt(r['release_date'])}" for r in main) or "нет релиза семейства в окне"},
        {"source": "Яндекс Метрика", "status": "частично" if cart and cart["status"] == "не измеряется" else "подтверждает",
         "evidence": (tx["metrika_traffic_ok"] if traffic_ok else tx["metrika_traffic_bad"])},
        {"source": "Бэклог", "status": "подтверждает" if any(t["status"] not in cfg["backlog"]["done_statuses"] for t in bl) else "нет связанных открытых задач",
         "evidence": ", ".join(f"{t['task_id']} ({t['status']})" for t in bl)},
        {"source": "Исследования/интервью", "status": "подтверждает" if research["problem_mentions"] else "не подтверждено",
         "evidence": ("; ".join(f"«{x['text'].split(':', 1)[-1].strip()[:160]}» [{x['source']}]" for x in research["problem_mentions"][:2]) if research["problem_mentions"]
                      else f"{research['mentions']} упоминаний семейства, жалоб на подбор/покупку нет — гипотеза внешними источниками не проверена")},
        {"source": "Конкурентный анализ", "status": "опровергает ценовую причину" if cfacts["store_cheaper_than"] else ("подтверждает" if compet["problem_mentions"] else "не подтверждено"),
         "evidence": (f"Store дешевле {len(cfacts['store_cheaper_than'])} площадок до {cfacts['max_gap_pct']}% — цена не объясняет падение; "
                      f"жалоб на подбор у конкурентов в исследовании нет" if cfacts["store_cheaper_than"] else "нет данных")},
    ]
    confirm = sum(1 for s in sources if s["status"] == "подтверждает")

    rel_main = main[0]["release_id"] if main else "—"
    hypotheses = [{**h, "text": h["text"].format(rel_main=rel_main, short=fcfg.get("short", family)),
                   "check": h["check"].format(rel_main=rel_main, short=fcfg.get("short", family)),
                   "criteria": h["criteria"]} for h in cfg["hypotheses"]]
    success = {
        "metric": f"{cfg['orders_metric_id']} (скользящее 4 нед)",
        "target": f"≥ {round(sig['baseline_mean'] * (1 - cfg['sales']['recovery_tolerance_pct'] / 100.0))} (уровень {_fmt(sig['baseline_from'])}–{_fmt(sig['baseline_to'])} = {sig['baseline_mean']}, допуск {cfg['sales']['recovery_tolerance_pct']}%)",
        "control": control_name,
        "rule": "индекс вернулся → задача закрыта; не вернулся за 4 недели после исправления → гипотеза пересматривается",
    }
    report = {
        "family": family, "short": fcfg.get("short", family), "signal": sig, "control": ctrl, "releases": rels,
        "funnel": funnel, "backlog": bl, "research": research, "competitive": compet, "competitive_facts": cfacts,
        "data_gaps": gaps, "sources": sources, "sources_confirm": confirm, "sources_total": len(sources),
        "hypotheses": hypotheses, "success_criteria": success,
        "conclusion": (f"Устойчивое падение самостоятельных заказов {fcfg.get('short', family)} на {abs(sig['change_pct'])}% с недели {_fmt(sig['onset_week'])}; "
                       f"контроль {control_name}: {ctrl['change_pct'] if ctrl else '—'}%. Главный кандидат — {rel_main}. "
                       + (tx["conclusion_traffic"] if traffic_ok else "")
                       + f"Подтверждают {confirm} из {len(sources)} источников."),
    }
    report["proposed_actions"] = proposed_actions(report)
    return report


def proposed_actions(rep: dict) -> list[dict[str, Any]]:
    cfg = load_config()
    tag = cfg["ai_tag"]
    acts = []
    main = [r for r in rep["releases"] if r["verdict"] == "главный кандидат"]
    rid = main[0]["release_id"] if main else ""
    acts.append({
        "kind": "new_task", "title": cfg["texts"]["task_title"].format(short=rep["short"], rel_main=rid).strip(),
        "type": "Исследование/Баг", "priority": "High",
        "description": ((rep["hypotheses"][0]["check"] + ". Сигнал: " if rep["hypotheses"] else "Сигнал: ") + rep["conclusion"]),
        "linked_metric_ids": cfg["texts"]["task_linked_metrics"], "release_id": rid,
        "success_criteria": rep["success_criteria"], "hypotheses": rep["hypotheses"], "tags": [tag], "source": "ИИ-помощник: Метрика+Продажи+Релизы+Бэклог+Исследования+Конкуренты"})
    for t in rep["backlog"]:
        if t.get("proposal", "").startswith("повысить"):
            acts.append({"kind": "raise_priority", "task_id": t["task_id"], "title": t["title"], "old": t["priority"] or "не указан", "new": "High",
                         "reason": cfg["texts"]["priority_reason"].format(short=rep["short"], rel_main=rid), "tags": [tag]})
    return acts


def release_effects() -> list[dict[str, Any]]:
    """Пост-релизный мониторинг: для каждого релиза и его метрик — 4 недели до/после (или почему сравнить нельзя)."""
    out = []
    cfg = load_config()
    with get_conn() as conn:
        prods = {r[0]: [p[0] for p in conn.execute("SELECT DISTINCT product FROM pm_metrics WHERE metric_id=?", (r[0],)).fetchall()]
                 for r in conn.execute("SELECT DISTINCT metric_id FROM pm_metrics").fetchall()}
    for rel in releases():
        rd = rel["release_date"]
        rdd = _d(rd)
        rw = (rdd - dt.timedelta(days=rdd.weekday())).isoformat()  # неделя релиза исключается (переходная)
        lo = (rdd - dt.timedelta(days=35)).isoformat()
        hi = (rdd + dt.timedelta(days=35)).isoformat()
        for mid in [m.strip() for m in (rel["affected_metrics"] or "").split(",") if m.strip()]:
            if mid == cfg["orders_metric_id"]:
                for fam in families():
                    s = sales_series(fam)
                    b = [x["orders_index"] for x in s if x["week_start"] < rw][-4:]
                    a = [x["orders_index"] for x in s if x["week_start"] > rw][:4]
                    out.append({"release_id": rel["release_id"], "metric_id": mid, "product": fam,
                                "before": round(mean(b), 1) if len(b) == 4 else None, "after": round(mean(a), 1) if len(a) == 4 else None,
                                "change_pct": _pct(mean(a), mean(b)) if len(a) == 4 and len(b) == 4 else None,
                                "gaps": data_gaps(["sales"], lo, hi)})
                continue
            for prod in prods.get(mid, []):
                raw = metric_weekly(mid, prod)
                wk = [x for x in raw if x["days"] >= 5]
                item = {"release_id": rel["release_id"], "metric_id": mid, "product": prod, "before": None, "after": None, "change_pct": None}
                if (raw and raw[0]["unit"] == "step") or mid in cfg["roles"]["structural_metrics"]:
                    item["note"] = "структурная метрика (шаг/распределение) — сравнение по detail_json, не средним"
                    out.append(item)
                    continue
                b = [x["value"] for x in wk if x["week_start"] < rw][-4:]
                a = [x["value"] for x in wk if x["week_start"] > rw][:4]
                item.update({"before": round(mean(b), 4) if b else None, "after": round(mean(a), 4) if a else None})
                item["change_pct"] = _pct(item["after"], item["before"]) if (len(b) >= 2 and a) else None
                scopes = [mid, prod] + (["visits"] if (wk and wk[0]["unit"] in ("visits", "views")) else [])
                item["gaps"] = data_gaps(scopes, lo, hi)
                if not b:
                    item["note"] = "нет данных до релиза — эффект не оценить (разметка появилась вместе с релизом или позже)"
                elif len(b) < 2:
                    item["note"] = "до релиза меньше 2 полных недель — эффект не оценить"
                elif item["gaps"]:
                    item["note"] = "в окне есть разрыв данных — вывод с осторожностью"
                out.append(item)
    return out
