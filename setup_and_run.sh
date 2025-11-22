#!/usr/bin/env bash

set -euo pipefail

# 默认启用本地 7890 端口代理，可按需修改。
export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 all_proxy=socks5://127.0.0.1:7890

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
VENV_DIR="${PROJECT_ROOT}/.venv"


if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -n "${MINIFORGE_HOME:-}" ] && [ -x "${MINIFORGE_HOME}/bin/python3" ]; then
    PYTHON_BIN="${MINIFORGE_HOME}/bin/python3"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "[error] 未找到可用的 python3，可通过设置 MINIFORGE_HOME 或 PYTHON_BIN 指定解释器。" >&2
    exit 1
  fi
else
  if [ ! -x "${PYTHON_BIN}" ]; then
    echo "[error] 指定的 PYTHON_BIN='${PYTHON_BIN}' 不可执行。" >&2
    exit 1
  fi
fi

if [ ! -d "${VENV_DIR}" ]; then
  echo "[setup] Creating virtual environment at ${VENV_DIR}" >&2
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"

echo "[setup] Upgrading pip inside ${VENV_DIR}" >&2
python -m pip install --upgrade pip

if [ -f "${PROJECT_ROOT}/requirements.txt" ]; then
  echo "[setup] Installing dependencies from requirements.txt" >&2
  python -m pip install -r "${PROJECT_ROOT}/requirements.txt"
fi

if printf '%s\n' "$@" | grep -E -q -- "--force-azure-diarization|--azure-summary"; then
  if [ -z "${AZURE_OPENAI_API_KEY:-}" ] || [ -z "${AZURE_OPENAI_ENDPOINT:-}" ]; then
    echo "[error] AZURE_OPENAI_API_KEY 或 AZURE_OPENAI_ENDPOINT 未设置。" >&2
    exit 1
  fi
fi

exec "${VENV_DIR}/bin/python" -m any2summary.cli "$@"
