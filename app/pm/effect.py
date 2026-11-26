"""После релиза/исправления: замер эффекта по скользящему индексу 4 недели до/после, с контрольной группой, и правило закрытия."""
from __future__ import annotations
import datetime as dt
from statistics import mean
from typing import Any

from app.pm.analysis import _fmt, _pct, detect_sales_signal, sales_series
from app.pm.settings import load_config


def measure_effect(family: str, fix_date: str, control: str | None = None, target: float | None = None,
                   extra_weeks: list[dict[str, Any]] | None = None, weeks: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    weeks = weeks or cfg["sales"]["rolling_weeks"]
    fcfg = cfg["families"].get(family, {})
    control = control or fcfg.get("control")
    simulated = bool(extra_weeks)

    def series(fam: str, extra: list[dict] | None) -> list[dict]:
        s = [{"week_start": x["week_start"], "orders_index": x["orders_index"], "simulated": False} for x in sales_series(fam)]
        if extra:
            known = {x["week_start"] for x in s}
            s += [{"week_start": e["week_start"], "orders_index": float(e["orders_index"]), "simulated": True}
                  for e in extra if e["week_start"] not in known]
        return sorted(s, key=lambda x: x["week_start"])

    fd = dt.date.fromisoformat(fix_date[:10])
    fix_week = (fd - dt.timedelta(days=fd.weekday())).isoformat()

    def window(s: list[dict]) -> tuple[list[dict], list[dict]]:
        before = [x for x in s if x["week_start"] < fix_week][-weeks:]
        after = [x for x in s if x["week_start"] > fix_week][:weeks]
        return before, after

    main = series(family, [e for e in (extra_weeks or []) if e.get("family", family) == family])
    b, a = window(main)
    res: dict[str, Any] = {"family": family, "fix_date": fix_date, "fix_week": fix_week, "weeks": weeks, "simulated": simulated,
                           "before": b, "after": a}
    res["before_mean"] = round(mean(x["orders_index"] for x in b), 1) if b else None
    res["after_mean"] = round(mean(x["orders_index"] for x in a), 1) if a else None
    if target is None:
        sig = detect_sales_signal(family)
        if sig:
            target = round(sig["baseline_mean"] * (1 - cfg["sales"]["recovery_tolerance_pct"] / 100.0), 1)
            res["target_basis"] = f"уровень {_fmt(sig['baseline_from'])}–{_fmt(sig['baseline_to'])} = {sig['baseline_mean']} минус допуск {cfg['sales']['recovery_tolerance_pct']}%"
    res["target"] = target
    if control:
        cs = series(control, [e for e in (extra_weeks or []) if e.get("family") == control])
        cb, ca = window(cs)
        res["control"] = {"family": control,
                          "before_mean": round(mean(x["orders_index"] for x in cb), 1) if cb else None,
                          "after_mean": round(mean(x["orders_index"] for x in ca), 1) if len(ca) == weeks else None}
        if res["control"]["before_mean"] and res["control"]["after_mean"]:
            res["control"]["change_pct"] = _pct(res["control"]["after_mean"], res["control"]["before_mean"])
    if res["before_mean"] and res["after_mean"]:
        res["change_pct"] = _pct(res["after_mean"], res["before_mean"])
        if res.get("control", {}).get("change_pct") is not None:
            res["did_pct"] = round(res["change_pct"] - res["control"]["change_pct"], 1)
    if len(a) < weeks:
        res["verdict"] = "ждём данных"
        res["decision"] = f"после релиза/исправления {len(a)} из {weeks} полных недель — решение принимать рано (только скользящее, не отдельные недели)"
    elif target is not None and res["after_mean"] >= target:
        res["verdict"] = "закрыть задачу"
        res["decision"] = f"скользящий индекс {res['after_mean']} ≥ цели {target}: эффект достигнут, задача закрывается"
    else:
        res["verdict"] = "пересмотреть гипотезу"
        res["decision"] = (f"скользящий индекс {res['after_mean']} < цели {target}: после релиза/исправления заказы не вернулись к цели — гипотеза пересматривается "
                           "(следующая по списку) или доработка/откат")
    if simulated:
        res["warning"] = "СИМУЛЯЦИЯ: недели после исправления введены вручную, в датасете их нет"
    return res
