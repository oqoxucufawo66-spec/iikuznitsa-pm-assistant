from __future__ import annotations
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
import httpx
from app.config import Settings, get_settings
from app.db import get_conn, now_ts

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_GIGA_LOCK = threading.Lock()
_GIGA_STATE: dict[str, Any] = {"token": None, "exp": 0.0}
_GIGA_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_rub: float
    cache_hit: bool = False


def clean_reasoning(text: str) -> str:
    t = _THINK_RE.sub("", text or "")
    if "<think>" in t:
        t = t.split("<think>")[0]
    return t.strip()


def _gigachat_token(client: httpx.Client, settings: Settings) -> str:
    with _GIGA_LOCK:
        if _GIGA_STATE["token"] and _GIGA_STATE["exp"] - 60 > time.time():
            return str(_GIGA_STATE["token"])
        r = client.post(
            _GIGA_OAUTH_URL,
            data={"scope": settings.gigachat_scope},
            headers={
                "Authorization": f"Basic {settings.api_key}",
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        js = r.json()
        _GIGA_STATE["token"] = js["access_token"]
        exp = js.get("expires_at", 0)
        _GIGA_STATE["exp"] = (exp / 1000.0) if exp else (time.time() + 1500.0)
        return str(_GIGA_STATE["token"])


def _calc_cost_rub(tokens_in: int, tokens_out: int, settings: Settings) -> float:
    cost = (tokens_in * settings.price_in_rub_1m + tokens_out * settings.price_out_rub_1m) / 1_000_000.0
    return round(cost, 6)


def _cache_key(provider: str, model: str, messages: list[dict[str, str]], temperature: float) -> str:
    raw = json.dumps([provider, model, messages, round(temperature, 2)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def chat_completion(
    messages: list[dict[str, str]],
    max_tokens: int = 700,
    temperature: float = 0.15,
    use_cache: bool = True,
    settings: Settings | None = None,
) -> LLMResponse:
    st = settings or get_settings()

    if st.provider == "mock":
        raise LLMError("LLM provider is in mock mode (no API key configured)")

    ck = _cache_key(st.provider, st.model, messages, temperature)
    if use_cache:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT response_text, tokens_saved FROM llm_cache WHERE cache_key=?",
                (ck,),
            ).fetchone()
            if row:
                return LLMResponse(
                    text=row["response_text"],
                    provider=st.provider,
                    model=st.model,
                    tokens_in=0,
                    tokens_out=0,
                    cost_rub=0.0,
                    cache_hit=True,
                )

    models_to_try = [st.model] + ([st.fallback_model] if st.fallback_model else [])
    last_err = ""
    verify: Any = st.ca_bundle if (st.provider == "gigachat" and st.ca_bundle) else True

    for mdl in models_to_try:
        try:
            with httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0), verify=verify) as client:
                if st.provider == "gigachat":
                    token = _gigachat_token(client, st)
                    headers = {"Authorization": f"Bearer {token}"}
                elif st.provider == "yandex":
                    headers = {"Authorization": f"Api-Key {st.api_key}", "x-folder-id": st.folder_id}
                else:
                    headers = {"Authorization": f"Bearer {st.api_key}"}

                model_id = mdl
                if st.provider == "yandex" and st.folder_id and not mdl.startswith("gpt://"):
                    model_id = f"gpt://{st.folder_id}/{mdl}"

                payload = {
                    "model": model_id,
                    "messages": messages,
                    # для рассуждающих моделей (например gpt-oss) токены рассуждения входят в max_tokens
                    "max_tokens": max_tokens + st.max_tokens_extra,
                    "temperature": temperature,
                }
                if st.reasoning_effort in ("low", "medium", "high"):
                    payload["reasoning_effort"] = st.reasoning_effort
                resp = client.post(f"{st.base_url}/chat/completions", json=payload, headers=headers)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    continue
                data = resp.json()
                base_resp = data.get("base_resp") or {}
                if base_resp.get("status_code"):
                    last_err = f"provider status {base_resp.get('status_code')}"
                    continue
                choice = (data.get("choices") or [{}])[0]
                text = clean_reasoning((choice.get("message") or {}).get("content") or "")
                if not text:
                    last_err = "empty completion"
                    continue
                usage = data.get("usage") or {}
                tin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                tout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                cost = _calc_cost_rub(tin, tout, st)

                if use_cache:
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO llm_cache(cache_key, response_text, tokens_saved, created_at) VALUES(?,?,?,?)",
                            (ck, text, tin + tout, now_ts()),
                        )

                return LLMResponse(
                    text=text,
                    provider=st.provider,
                    model=mdl,
                    tokens_in=tin,
                    tokens_out=tout,
                    cost_rub=cost,
                    cache_hit=False,
                )
        except Exception as exc:
            last_err = type(exc).__name__
            continue

    raise LLMError(f"All models failed ({last_err})")


def embed_texts(texts: list[str], settings: Settings | None = None) -> list[list[float]] | None:
    st = settings or get_settings()
    if not texts or st.provider == "mock" or not st.embed_model or not st.api_key:
        return None
    if st.provider not in ("cloudru", "openai", "openrouter"):
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(25.0, connect=8.0)) as client:
            resp = client.post(
                f"{st.base_url}/embeddings",
                json={"model": st.embed_model, "input": texts},
                headers={"Authorization": f"Bearer {st.api_key}"},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json().get("data") or []
            if len(data) != len(texts):
                return None
            data_sorted = sorted(data, key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in data_sorted]
    except Exception:
        return None
