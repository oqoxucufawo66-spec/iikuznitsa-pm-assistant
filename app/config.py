from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data" / "demo_catalog"))
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", BASE_DIR / "artifacts"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "pm_assistant.db"))

PROVIDER_DEFAULTS = {
    "cloudru": {
        "base_url": "https://foundation-models.api.cloud.ru/v1",
        "model": "GigaChat/GigaChat-3-Pro",
        "fallback_model": "ai-sage/GigaChat3-10B-A1.8B",
        "price_in_rub_1m": 96.0,
        "price_out_rub_1m": 289.0,
        "ru_hosted": True,
    },
    "gigachat": {
        "base_url": "https://gigachat.devices.sberbank.ru/api/v1",
        "model": "GigaChat-2-Pro",
        "fallback_model": "GigaChat-2",
        "price_in_rub_1m": 500.0,
        "price_out_rub_1m": 500.0,
        "ru_hosted": True,
    },
    "yandex": {
        "base_url": "https://llm.api.cloud.yandex.net/v1",
        "model": "yandexgpt/latest",
        "fallback_model": "yandexgpt-lite/latest",
        "price_in_rub_1m": 400.0,
        "price_out_rub_1m": 400.0,
        "ru_hosted": True,
    },
    "minimax": {
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3",
        "fallback_model": "MiniMax-M2.7-highspeed",
        "price_in_rub_1m": 25.0,
        "price_out_rub_1m": 100.0,
        "ru_hosted": False,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "fallback_model": "qwen/qwen-2.5-72b-instruct",
        "price_in_rub_1m": 15.0,
        "price_out_rub_1m": 60.0,
        "ru_hosted": False,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "fallback_model": "",
        "price_in_rub_1m": 15.0,
        "price_out_rub_1m": 60.0,
        "ru_hosted": False,
    },
    "mock": {
        "base_url": "",
        "model": "deterministic-local-mock",
        "fallback_model": "",
        "price_in_rub_1m": 0.0,
        "price_out_rub_1m": 0.0,
        "ru_hosted": True,
    },
}


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str
    base_url: str
    model: str
    fallback_model: str
    folder_id: str
    gigachat_scope: str
    ca_bundle: str
    embed_model: str
    price_in_rub_1m: float
    price_out_rub_1m: float
    ru_hosted: bool
    heygen_dry_run: bool
    heygen_api_key: str
    heygen_avatar_id: str
    heygen_voice_id: str
    telegram_bot_token: str
    dataset_label: str = "ДЕМО-ДАННЫЕ"
    reasoning_effort: str = ""
    max_tokens_extra: int = 0


def get_settings() -> Settings:
    raw_prov = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("CLOUDRU_FM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("MINIMAX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()

    if not raw_prov:
        if os.environ.get("CLOUDRU_FM_API_KEY"):
            raw_prov = "cloudru"
        elif os.environ.get("OPENROUTER_API_KEY"):
            raw_prov = "openrouter"
        elif os.environ.get("MINIMAX_API_KEY"):
            raw_prov = "minimax"
        elif os.environ.get("OPENAI_API_KEY"):
            raw_prov = "openai"
        else:
            raw_prov = "mock"

    if raw_prov not in PROVIDER_DEFAULTS:
        raw_prov = "openai"

    if raw_prov != "mock" and not api_key:
        raw_prov = "mock"

    d = PROVIDER_DEFAULTS[raw_prov]
    return Settings(
        provider=raw_prov,
        api_key=api_key,
        base_url=(os.environ.get("LLM_BASE_URL") or d["base_url"]).rstrip("/"),
        model=os.environ.get("LLM_MODEL") or d["model"],
        fallback_model=os.environ.get("LLM_FALLBACK_MODEL") or d["fallback_model"],
        folder_id=os.environ.get("LLM_FOLDER_ID") or "",
        gigachat_scope=os.environ.get("GIGACHAT_SCOPE") or "GIGACHAT_API_B2B",
        ca_bundle=os.environ.get("LLM_CA_BUNDLE") or "",
        embed_model=os.environ.get("EMBED_MODEL") or (
            "ai-sage/Giga-Embeddings-instruct-480M" if raw_prov == "cloudru" else ""
        ),
        price_in_rub_1m=float(os.environ.get("LLM_PRICE_IN_RUB_1M") or d["price_in_rub_1m"]),
        price_out_rub_1m=float(os.environ.get("LLM_PRICE_OUT_RUB_1M") or d["price_out_rub_1m"]),
        ru_hosted=bool(d["ru_hosted"]),
        heygen_dry_run=(os.environ.get("HEYGEN_DRY_RUN", "true").lower() != "false"),
        heygen_api_key=os.environ.get("HEYGEN_API_KEY") or "",
        heygen_avatar_id=os.environ.get("HEYGEN_AVATAR_ID") or "Anna_public_3_20240108",
        heygen_voice_id=os.environ.get("HEYGEN_VOICE_ID") or "ru-RU-SvetlanaNeural",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or "",
        reasoning_effort=(os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower(),
        max_tokens_extra=int(os.environ.get("LLM_MAX_TOKENS_EXTRA") or 0),
    )
