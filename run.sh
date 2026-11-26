#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
elif [ -z "${LLM_API_KEY:-}" ] && [ -z "${CLOUDRU_FM_API_KEY:-}" ] && [ -f /workspace/secrets/cloudru-fm.env ]; then
  set -a
  source /workspace/secrets/cloudru-fm.env
  set +a
fi

export LLM_PROVIDER="${LLM_PROVIDER:-cloudru}"
export LLM_MODEL="${LLM_MODEL:-GigaChat/GigaChat-3-Pro}"
export LLM_FALLBACK_MODEL="${LLM_FALLBACK_MODEL:-ai-sage/GigaChat3-10B-A1.8B}"
export EMBED_MODEL="${EMBED_MODEL:-ai-sage/Giga-Embeddings-instruct-480M}"
export HEYGEN_DRY_RUN="${HEYGEN_DRY_RUN:-true}"
export APP_PORT="${APP_PORT:-8091}"

PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then
    PY=".venv/bin/python"
  elif [ -x /workspace/.venv-img/bin/python ]; then
    PY="/workspace/.venv-img/bin/python"
  else
    PY="python3"
  fi
fi

exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"
