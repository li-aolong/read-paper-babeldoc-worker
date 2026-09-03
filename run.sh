#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -z "${BABELDOC_API_KEY:-}" ] && [ -n "${GLM_API_KEY:-}" ]; then
  export BABELDOC_API_KEY="$GLM_API_KEY"
fi
export BABELDOC_BASE_URL="${BABELDOC_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
export BABELDOC_MODEL="${BABELDOC_MODEL:-glm-4-flash-250414}"
export BABELDOC_SAMPLE_PDF="${BABELDOC_SAMPLE_PDF:-/home/yhli/yhli-library/research/4-投稿/别人/WSDM 2027/WSDM2027_Web_Data_Collection.pdf}"

if [ ! -x .venv/bin/uvicorn ]; then
  uv sync --frozen
fi

exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port "${BABELDOC_LAB_PORT:-8787}"
