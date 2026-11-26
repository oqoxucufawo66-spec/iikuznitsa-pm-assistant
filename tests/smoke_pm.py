"""Смоук-проверка ИИ-помощника ПМ.

Ожидаемые значения эталонного кейса организаторов в репозитории НЕ хранятся (правила датасета): они читаются из
PM_DATA_DIR/expected.local.json (по умолчанию ./private-data/expected.local.json, вне git). Нет файла — тест пропускается (SKIP, exit 0).

  python tests/smoke_pm.py --local                  # движок на локальных данных, без LLM
  python tests/smoke_pm.py --url URL [--user U --password P] [--llm]   # стенд по HTTP
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

FAILS: list[str] = []
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PM_DATA_DIR", ROOT / "private-data"))


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def expected() -> dict | None:
    p = DATA_DIR / "expected.local.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def rng(v, lo_hi) -> bool:
    return lo_hi[0] <= v <= lo_hi[1]


def local(E: dict) -> int:
    sys.path.insert(0, str(ROOT))
    if not any(DATA_DIR.glob("*атасет*.xlsx")) or not (DATA_DIR / "pm_signals.local.json").exists():
        print(f"SKIP: нет датасета или pm_signals.local.json в {DATA_DIR}")
        return 0
    os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "smoke.db"))
    os.environ["LLM_PROVIDER"] = "mock"
    from app.db import init_db
    init_db()
    from app.pm import actions, analysis, effect, store
    st = store.load_all(force=True)["status"]
    print("load:", {k: v for k, v in st.items() if k != "sources"})
    fam = E["family"]
    rep = analysis.analyze_family(fam)
    sig = rep["signal"]
    check(sig is not None, "1. найден устойчивый сигнал")
    check(rng(sig["change_pct"], E["change_pct"]), f"1. падение в ожидаемом диапазоне (факт {sig['change_pct']}%)")
    check(abs(sig["baseline_mean"] - E["baseline"][0]) <= E["baseline"][1] and abs(sig["after_mean"] - E["after"][0]) <= E["after"][1],
          f"1. база/после (факт {sig['baseline_mean']} → {sig['after_mean']})")
    check(any(g["week_start"] == E["guard_week"] for g in sig["excluded_guard_weeks"]), "ловушка: разовый всплеск исключён")
    check(sig["last_week_incomplete"], "ловушка: последняя неделя помечена неполной")
    check(rep["control"]["stable"] and abs(rep["control"]["change_pct"]) <= E["control_max_abs_pct"], f"2. контроль стабилен ({rep['control']['change_pct']}%)")
    v = {r["release_id"]: r["verdict"] for r in rep["releases"]}
    for rid, verdict in E["verdicts"].items():
        check(v.get(rid) == verdict, f"3. {rid}: {verdict}")
    u = E["use_share"]
    use = next((f for f in rep["funnel"] if f["metric_id"] == u["metric_id"]), None)
    check(use is not None and rng(use["total_after"], u["total_after"]) and use["min"] >= u["min_ge"] and use["max"] <= u["max_le"], "4. использование воронки сохранилось")
    probe = next(f for f in rep["funnel"] if f["metric_id"] == E["not_measured_metric"])
    check(probe["status"] == "не измеряется", "5. неизмеряемая метрика не превращается в «ноль»")
    check(any(t["task_id"] == E["linked_task"] for t in rep["backlog"]), "5. связанная задача бэклога найдена")
    check(len(rep["hypotheses"]) >= E["min_hypotheses"] and all(h["check"] for h in rep["hypotheses"]), "5. гипотезы с проверками")
    check(any(s["source"].startswith("Исследования") for s in rep["sources"]) and any(s["source"].startswith("Конкурент") for s in rep["sources"]), "6. исследования и конкуренты проверены")
    for kws in E["gap_keywords"]:
        check(any(any(k in g for k in kws) for g in rep["data_gaps"]), f"ограничение данных указано: {kws}")
    applied = actions.apply_actions(rep)
    t = next(a for a in applied if a["kind"] == "new_task")
    check(t["created"] and t["tags"] == ["сгенерирована ИИ-помощником"], f"7. задача {t['task_id']} с тегом")
    check(any(a["kind"] == "raise_priority" and a["task_id"] == E["ai_task_priority_task_id"] for a in applied), "7. предложено повысить приоритет")
    check(not next(a for a in actions.apply_actions(rep) if a["kind"] == "new_task")["created"], "идемпотентность")
    msg = actions.compose_message(rep, applied)
    check("Критерий успеха" in msg["body"], "7. письмо ПМ с критерием успеха")
    ef = E["effect"]
    e1 = effect.measure_effect(fam, ef["fix_date"])
    check(e1["verdict"] == ef["verdict"], f"8. эффект: {e1['verdict']} ({e1['before_mean']} → {e1['after_mean']})")
    sim = [{"week_start": w, "orders_index": ef["sim_index"]} for w in ef["sim_weeks"]]
    e2 = effect.measure_effect(fam, ef["sim_fix_date"], extra_weeks=sim)
    check(e2["verdict"] == "закрыть задачу" and e2.get("warning"), "8. правило закрытия на симуляции (помечено)")
    check(effect.measure_effect(fam, ef["early_fix_date"])["verdict"] == "ждём данных", "8. мало недель → «ждём данных»")
    check(analysis.analyze_family(E["no_signal_family"])["signal"] is None, "ложного сигнала на контроле нет")
    return 1 if FAILS else 0


def http(E: dict | None, url: str, user: str | None, pwd: str | None, need_llm: bool) -> int:
    hdr = {"Content-Type": "application/json"}
    if user:
        hdr["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    def call(path: str, body: dict | None = None, auth: bool = True):
        req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=hdr if auth else {}, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "null")

    st, h = call("/health", auth=False)
    check(st == 200 and h["status"] == "ok", f"/health ok (provider={h.get('provider')}, model={h.get('model')})")
    check(h.get("pm_data_loaded") is True, "данные задачи загружены на стенде")
    if h.get("auth_enabled"):
        try:
            call("/api/pm/status", auth=False)
            check(False, "без пароля API закрыт")
        except urllib.error.HTTPError as e:
            check(e.code == 401, "без пароля API закрыт (401)")
    _, s = call("/api/pm/status")
    check(s["metrics_rows"] > 0 and s["research_chunks"] > 0, f"статус: {s['metrics_rows']} строк Метрики, {s['research_chunks']} фрагментов исследований")
    _, sc = call("/api/pm/scan")
    check(len(sc["signals"]) >= 1, "скан нашёл сигнал")
    if not E:
        print("SKIP: нет expected.local.json — проверки значений эталонного кейса пропущены")
        return 1 if FAILS else 0
    _, rep = call("/api/pm/signal/" + urllib.request.quote(E["family"]))
    check(rng(rep["signal"]["change_pct"], E["http"]["change_pct"]), f"разбор: {rep['signal']['change_pct']}%, {rep['sources_confirm']}/{rep['sources_total']} источников")
    _, a = call("/api/pm/ask", {"question": E["http"]["question"], "session_id": "smoke"})
    llm_ok = a["provider"] != "mock" and not any(t.get("step") == "llm" for t in a["trace"])
    print("  answer:", a["answer"][:500].replace("\n", " "), "…")
    print(f"  meta: {a['provider']} {a['model']} tokens {a['tokens_in']}+{a['tokens_out']} cost {a['cost_rub']} ₽ {a['latency_ms']} ms cache={a['cache_hit']}")
    check(any(k.lower() in a["answer"].lower() for k in E["http"]["answer_keywords_any"]), "ответ опирается на ожидаемые факты")
    if need_llm:
        check(llm_ok, "ответ сгенерирован LLM (не fallback)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--user", default=os.environ.get("STAND_USER"))
    ap.add_argument("--password", default=os.environ.get("STAND_PASSWORD"))
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    E = expected()
    if a.url and not a.local:
        rc = http(E, a.url, a.user, a.password, a.llm)
    elif E is None:
        print(f"SKIP: нет {DATA_DIR / 'expected.local.json'} (данные и ожидаемые значения организаторов не публикуются в git)")
        rc = 0
    else:
        rc = local(E)
    print("RESULT:", "FAIL" if rc else "PASS", FAILS)
    sys.exit(rc)
