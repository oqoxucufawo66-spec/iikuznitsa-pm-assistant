"""Действия агента: задачи в бэклог с тегом, предложения по приоритету, письмо/сообщение ПМ, экспорт для Jira."""
from __future__ import annotations
import csv
import io
import json
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from app.db import get_conn, now_ts
from app.pm.analysis import _fmt, backlog
from app.pm.settings import load_config


def _next_ai_id(conn) -> str:
    n = conn.execute("SELECT COUNT(*) FROM pm_backlog WHERE ai_generated=1").fetchone()[0]
    while True:
        n += 1
        tid = f"AI-{n:02d}"
        if not conn.execute("SELECT 1 FROM pm_backlog WHERE task_id=?", (tid,)).fetchone():
            return tid


def create_task(title: str, description: str, type_: str = "Исследование", priority: str = "High",
                linked_metric_ids: str = "", release_id: str = "", details: dict | None = None, source: str = "ИИ-помощник") -> dict[str, Any]:
    tag = load_config()["ai_tag"]
    with get_conn() as conn:
        # идемпотентность: та же задача не создаётся дважды
        ex = conn.execute("SELECT task_id FROM pm_backlog WHERE ai_generated=1 AND title=? AND status!='Closed'", (title,)).fetchone()
        if ex:
            return {"created": False, "task_id": ex[0], "detail": "уже есть в бэклоге"}
        tid = _next_ai_id(conn)
        conn.execute(
            "INSERT INTO pm_backlog(task_id,title,type,status,priority,created_date,closed_date,release_id,description,linked_metric_ids,source,tags,ai_generated,details_json,updated_at)"
            " VALUES (?,?,?,?,?,date('now'),'',?,?,?,?,?,1,?,?)",
            (tid, title, type_, "New", priority, release_id, description, linked_metric_ids, source,
             json.dumps([tag], ensure_ascii=False), json.dumps(details or {}, ensure_ascii=False), now_ts()))
    return {"created": True, "task_id": tid, "tags": [tag]}


def propose_change(task_id: str, field: str, new_value: str, reason: str) -> dict[str, Any]:
    tag = load_config()["ai_tag"]
    with get_conn() as conn:
        row = conn.execute(f"SELECT {field} FROM pm_backlog WHERE task_id=?", (task_id,)).fetchone() if field in ("priority", "status") else None
        if row is None:
            return {"ok": False, "detail": "нет такой задачи/поля"}
        ex = conn.execute("SELECT id FROM pm_backlog_changes WHERE task_id=? AND field=? AND new_value=? AND status='proposed'",
                          (task_id, field, new_value)).fetchone()
        if ex:
            return {"ok": True, "change_id": ex[0], "detail": "предложение уже есть"}
        cur = conn.execute("INSERT INTO pm_backlog_changes(task_id,field,old_value,new_value,reason,tag,status,created_at) VALUES (?,?,?,?,?,?,'proposed',?)",
                           (task_id, field, row[0] or "", new_value, reason, tag, now_ts()))
        return {"ok": True, "change_id": cur.lastrowid}


def decide_change(change_id: int, approve: bool) -> dict[str, Any]:
    with get_conn() as conn:
        ch = conn.execute("SELECT * FROM pm_backlog_changes WHERE id=?", (change_id,)).fetchone()
        if not ch or ch["status"] != "proposed":
            return {"ok": False, "detail": "нет активного предложения"}
        if approve:
            conn.execute(f"UPDATE pm_backlog SET {ch['field']}=?, updated_at=? WHERE task_id=?", (ch["new_value"], now_ts(), ch["task_id"]))
        conn.execute("UPDATE pm_backlog_changes SET status=? WHERE id=?", ("approved" if approve else "rejected", change_id))
    return {"ok": True, "status": "approved" if approve else "rejected"}


def list_changes() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pm_backlog_changes ORDER BY id DESC").fetchall()]


def close_task(task_id: str, resolution: str) -> dict[str, Any]:
    with get_conn() as conn:
        r = conn.execute("UPDATE pm_backlog SET status='Closed', closed_date=date('now'), details_json=json_set(COALESCE(details_json,'{}'),'$.resolution',?), updated_at=? WHERE task_id=?",
                         (resolution, now_ts(), task_id))
    return {"ok": r.rowcount > 0}


def apply_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in report.get("proposed_actions", []):
        if a["kind"] == "new_task":
            res = create_task(a["title"], a["description"], a["type"], a["priority"], a["linked_metric_ids"], a["release_id"],
                              {"hypotheses": a["hypotheses"], "success_criteria": a["success_criteria"], "signal": report.get("conclusion")}, a["source"])
            out.append({**res, "kind": "new_task", "title": a["title"]})
        elif a["kind"] == "raise_priority":
            res = propose_change(a["task_id"], "priority", a["new"], a["reason"])
            out.append({**res, "kind": "raise_priority", "task_id": a["task_id"]})
    return out


def export_csv() -> str:
    """CSV в формате импорта Jira (Summary, Issue Type, Priority, Description, Labels)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Issue key", "Summary", "Issue Type", "Status", "Priority", "Description", "Labels", "Linked metrics", "Release"])
    for t in backlog():
        labels = " ".join(x.replace(" ", "_") for x in t["tags"])
        w.writerow([t["task_id"], t["title"], t["type"], t["status"], t["priority"], t["description"], labels, t["linked_metric_ids"], t["release_id"]])
    return buf.getvalue()


def compose_message(report: dict[str, Any], applied: list[dict[str, Any]] | None = None) -> dict[str, str]:
    sig = report["signal"]
    ctrl = report.get("control") or {}
    main = [r for r in report["releases"] if r["verdict"] == "главный кандидат"]
    lines = [
        f"Сигнал: самостоятельные заказы {report['short']} упали на {abs(sig['change_pct'])}% "
        f"(среднее {sig['baseline_mean']} → {sig['after_mean']}, база {_fmt(sig['baseline_from'])}–{_fmt(sig['baseline_to'])}, с недели {_fmt(sig['onset_week'])}).",
        f"Контроль: {ctrl.get('family', '—')} за те же недели {ctrl.get('change_pct', '—')}% → падение специфично для {report['short']}.",
        "",
        f"Перекрёстная проверка — подтверждают {report['sources_confirm']} из {report['sources_total']} источников:",
    ]
    lines += [f"• {s['source']}: {s['status']} — {s['evidence']}" for s in report["sources"]]
    lines += ["", "Релизы в окне:"]
    lines += [f"• {r['release_id']} ({_fmt(r['release_date'])}, {r['release_type']}): {r['verdict']} — {r['why']}" for r in report["releases"]]
    lines += ["", "Гипотезы и проверки (до разработки):"]
    lines += [f"• {h['id']}: {h['text']}. Проверка: {h['check']}. Критерий: {h['criteria']}" for h in report["hypotheses"]]
    sc = report["success_criteria"]
    lines += ["", f"Критерий успеха: {sc['metric']} {sc['target']}, контроль {sc['control']}. Правило: {sc['rule']}.", ""]
    if report["data_gaps"]:
        lines += ["Ограничения данных (учтены в выводе):"] + [f"• {g}" for g in report["data_gaps"]] + [""]
    lines += ["Прошу:"]
    for a in report["proposed_actions"]:
        if a["kind"] == "new_task":
            tid = next((x.get("task_id") for x in (applied or []) if x.get("kind") == "new_task"), None)
            lines.append(f"• задача {tid + ' ' if tid else ''}«{a['title']}» — в бэклог с тегом «{a['tags'][0]}»")
        elif a["kind"] == "raise_priority":
            lines.append(f"• поднять приоритет {a['task_id']} «{a['title']}»: {a['old']} → {a['new']} ({a['reason']})")
    subj = f"[ИИ-помощник ПМ] {report['short']}: заказы {sig['change_pct']}% после {main[0]['release_id'] if main else 'релиза'} — нужна проверка"
    return {"subject": subj, "body": "\n".join(lines)}


def notify(subject: str, body: str) -> dict[str, Any]:
    """По умолчанию dry-run: сообщение сохраняется в журнал. Реальная отправка — при SMTP_* или TELEGRAM_* в .env и notify.dry_run=false."""
    cfg = load_config().get("notify", {})
    to = os.environ.get("PM_NOTIFY_TO") or cfg.get("to", "")
    status = "dry-run"
    channel = cfg.get("channel", "email")
    if not cfg.get("dry_run", True):
        try:
            if channel == "email" and os.environ.get("SMTP_HOST"):
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM", "pm-assistant@localhost"), to
                msg.set_content(body)
                with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465")), timeout=20) as s:
                    if os.environ.get("SMTP_USER"):
                        s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    s.send_message(msg)
                status = "sent"
            elif channel == "telegram" and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("PM_TELEGRAM_CHAT_ID"):
                import httpx
                httpx.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                           json={"chat_id": os.environ["PM_TELEGRAM_CHAT_ID"], "text": f"{subject}\n\n{body}"[:4000]}, timeout=20)
                status = "sent"
        except Exception as exc:
            status = f"error: {type(exc).__name__}"
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO pm_notifications(channel,recipient,subject,body,status,created_at) VALUES (?,?,?,?,?,?)",
                           (channel, to, subject, body, status, now_ts()))
    return {"id": cur.lastrowid, "channel": channel, "to": to, "status": status}


def notifications(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pm_notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
