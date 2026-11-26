from __future__ import annotations
import copy
import json
import os
from pathlib import Path
from app.config import BASE_DIR, DB_PATH

PM_DATA_DIR = Path(os.environ.get("PM_DATA_DIR", BASE_DIR / "private-data"))
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "pm_signals.json"
# Всё, что выведено из материалов организаторов (семейства, метрики, разрывы данных, формулировки), — в локальном файле вне git
LOCAL_CONFIG_PATH = Path(os.environ.get("PM_LOCAL_CONFIG", PM_DATA_DIR / "pm_signals.local.json"))
# Изменения через API сохраняются рядом с БД (том /app/var на стенде), исходный файл в git не трогаем
OVERRIDE_CONFIG_PATH = Path(os.environ.get("PM_CONFIG_OVERRIDE", DB_PATH.parent / "pm_signals.override.json"))


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for extra in (LOCAL_CONFIG_PATH, BASE_DIR / "config" / "pm_signals.local.json"):
        if extra.exists():
            try:
                cfg = _deep_merge(cfg, json.loads(extra.read_text(encoding="utf-8")))
            except Exception:
                pass
            break
    if OVERRIDE_CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(OVERRIDE_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_override(patch: dict) -> dict:
    cur = {}
    if OVERRIDE_CONFIG_PATH.exists():
        try:
            cur = json.loads(OVERRIDE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur = _deep_merge(cur, patch)
    OVERRIDE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_CONFIG_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config()


def reset_override() -> dict:
    if OVERRIDE_CONFIG_PATH.exists():
        OVERRIDE_CONFIG_PATH.unlink()
    return load_config()


def has_local_config() -> bool:
    return LOCAL_CONFIG_PATH.exists() or (BASE_DIR / "config" / "pm_signals.local.json").exists()
