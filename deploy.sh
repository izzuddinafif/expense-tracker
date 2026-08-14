#!/usr/bin/env bash
set -euo pipefail

cd /home/afif/projects/expense-tracker

if [[ "${LEDGERLY_LOCAL_DEPLOY:-0}" != "1" ]]; then
  echo "Production is deployed by Coolify; this script only manages an explicit local profile." >&2
  echo "Set LEDGERLY_LOCAL_DEPLOY=1 to run the local compose instance." >&2
  exit 2
fi

echo "=== Deploy started $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

echo "→ docker compose --profile local up --build -d"
docker compose --profile local up --build -d

echo "→ docker compose ps"
docker compose ps

echo "=== Deploy complete ==="
