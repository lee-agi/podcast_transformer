#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/.env" ]; then
  echo "Loading environment variables from ${SCRIPT_DIR}/.env"
  set -a
  # shellcheck disable=SC1090
  source "${SCRIPT_DIR}/.env"
  set +a
fi

URL="${1:-https://www.youtube.com/watch?v=exampleid}"

"${SCRIPT_DIR}/setup_and_run.sh" \
  --url "${URL}" \
  --language en \
  --force-azure-diarization \
  --azure-summary \
