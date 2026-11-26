from __future__ import annotations
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.db import get_conn
from app.pm import actions, agent, analysis, effect, metrika, store
from app.pm.settings import PM_DATA_DIR, has_local_config, load_config, reset_override, save_override

router = APIRouter(prefix="/api/pm", tags=["pm"])
_LOAD = {"state": "idle", "error": "", "result": None}
_LOCK = threading.Lock()


def background_load(force: bool = False) -> None:
    def run():
        with _LOCK:
            _LOAD["state"] = "loading"
            try:
                _LOAD["result"] = store.load_all(force=force)
                _LOAD["state"] = "ready"
            except Exception as exc:  # noqa: BLE001
                _LOAD["state"], _LOAD["error"] = "error", f"{type(exc).__name__}: {exc}"[:300]
    threading.Thread(target=run, daemon=True).start()


def _require_data() -> None:
    if not store.status()["loaded"]:
        raise HTTPException(status_code=503, detail=f"Данные задачи ещё не загружены (state={_LOAD['state']}). Положите файлы в PM_DATA_DIR и вызовите /api/pm/reload")


def _family(name: str) -> str:
    fams = analysis.families()
    for f in fams:
        c = load_config()["families"].get(f, {})
        if name.lower() in (f.lower(), c.get("short", "").lower()) or any(s == name.lower() for s in c.get("synonyms", [])):
            return f
    raise HTTPException(status_code=404, detail=f"Семейство не найдено. Есть: {fams}")


class AskReq(BaseModel):
    question: str = Field(..., min_length=2, max_length=2000)
    session_id: str = "pm-web"


class TaskReq(BaseModel):
    title: str = Field(..., min_length=3)
    description: str = ""
    type: str = "Улучшение"
    priority: str = "Medium"
    linked_metric_ids: str = ""
    release_id: str = ""


class EffectReq(BaseModel):
    family: str
    fix_date: str
    control: str | None = None
    target: float | None = None
    extra_weeks: list[dict[str, Any]] | None = None


@router.get("/status")
def pm_status() -> dict[str, Any]:
    return {"load": {"state": _LOAD["state"], "error": _LOAD["error"]}, "data_dir_exists": PM_DATA_DIR.exists(),
            "dataset_label": "Обезличенный тестовый датасет организаторов ИИкузницы (v17)", **store.status(), "metrika": metrika.status()}


@router.get("/ui-config")
def pm_ui_config() -> dict[str, Any]:
    """Примеры вопросов, список семейств и значения по умолчанию для интерфейса — из локального конфига (вне git)."""
    cfg = load_config()
    return {"ui": cfg.get("ui", {}), "families": analysis.families() if store.status()["loaded"] else [],
            "local_config": has_local_config()}


@router.post("/reload")
def pm_reload(force: bool = True) -> dict[str, Any]:
    background_load(force=force)
    return {"ok": True, "state": "loading"}


@router.get("/scan")
def pm_scan() -> dict[str, Any]:
    _require_data()
    return agent.scan()


@router.get("/signal/{family}")
def pm_signal(family: str) -> dict[str, Any]:
    _require_data()
    return analysis.analyze_family(_family(family))


@router.get("/sales/{family}")
def pm_sales(family: str) -> dict[str, Any]:
    _require_data()
    f = _family(family)
    return {"family": f, "series": analysis.sales_series(f)}


@router.get("/metric/{metric_id}")
def pm_metric(metric_id: str, product: str) -> dict[str, Any]:
    _require_data()
    return {"metric_id": metric_id, "product": product, "weekly": analysis.metric_weekly(metric_id, product),
            "ref": analysis.metrics_ref().get(metric_id)}


@router.post("/ask")
def pm_ask(req: AskReq) -> dict[str, Any]:
    _require_data()
    return agent.ask(req.question, req.session_id)


@router.post("/apply/{family}")
def pm_apply(family: str, notify: bool = True) -> dict[str, Any]:
    """Отправить в бэклог задачу с тегом + предложить смену приоритетов + сформировать письмо ПМ (dry-run по умолчанию)."""
    _require_data()
    rep = analysis.analyze_family(_family(family))
    if not rep.get("signal"):
        return {"ok": False, "detail": rep.get("message")}
    applied = actions.apply_actions(rep)
    msg = actions.compose_message(rep, applied)
    sent = actions.notify(msg["subject"], msg["body"]) if notify else None
    return {"ok": True, "applied": applied, "message": msg, "notification": sent}


@router.get("/backlog")
def pm_backlog() -> dict[str, Any]:
    _require_data()
    return {"items": analysis.backlog(), "changes": actions.list_changes(), "ai_tag": load_config()["ai_tag"]}


@router.get("/backlog/ranked")
def pm_backlog_ranked() -> dict[str, Any]:
    _require_data()
    return {"items": agent.rank_backlog()}


@router.get("/backlog.csv", response_class=PlainTextResponse)
def pm_backlog_csv() -> str:
    _require_data()
    return actions.export_csv()


@router.post("/backlog")
def pm_backlog_create(req: TaskReq) -> dict[str, Any]:
    return actions.create_task(req.title, req.description, req.type, req.priority, req.linked_metric_ids, req.release_id)


@router.post("/backlog/{task_id}/close")
def pm_backlog_close(task_id: str, resolution: str = "Эффект достигнут") -> dict[str, Any]:
    return actions.close_task(task_id, resolution)


@router.post("/changes/{change_id}")
def pm_change(change_id: int, approve: bool = True) -> dict[str, Any]:
    return actions.decide_change(change_id, approve)


@router.post("/effect")
def pm_effect(req: EffectReq) -> dict[str, Any]:
    _require_data()
    return effect.measure_effect(_family(req.family), req.fix_date, req.control, req.target, req.extra_weeks)


@router.get("/release-effects")
def pm_release_effects() -> dict[str, Any]:
    _require_data()
    return {"items": analysis.release_effects(), "releases": analysis.releases()}


@router.get("/reference-case")
def pm_reference_case() -> dict[str, Any]:
    with get_conn() as conn:
        return {"steps": [dict(r) for r in conn.execute("SELECT * FROM pm_reference_case").fetchall()]}


@router.get("/config")
def pm_config() -> dict[str, Any]:
    return load_config()


@router.put("/config")
def pm_config_put(patch: dict[str, Any]) -> dict[str, Any]:
    return save_override(patch)


@router.delete("/config")
def pm_config_reset() -> dict[str, Any]:
    return reset_override()


@router.get("/metrika/status")
def pm_metrika_status() -> dict[str, Any]:
    return metrika.status()


@router.post("/metrika/sync")
def pm_metrika_sync(date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    return metrika.sync(date_from, date_to)


@router.post("/research/upload")
async def pm_research_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Закрытый контур: новые исследования/интервью (xlsx, md, txt, csv) сразу попадают в индекс агента."""
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in (".xlsx", ".md", ".txt", ".csv"):
        raise HTTPException(status_code=400, detail="Поддерживаются xlsx, md, txt, csv")
    dest_dir = PM_DATA_DIR / "uploaded-research"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(await file.read())
    n = store.load_research_file(dest)
    return {"ok": True, "file": name, "chunks": n}


@router.get("/notifications")
def pm_notifications() -> dict[str, Any]:
    return {"items": actions.notifications()}
