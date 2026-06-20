#!/usr/bin/env bash
set -euo pipefail

cd /home/afif/projects/expense-tracker

echo "=== Deploy started $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

echo "→ docker compose down"
docker compose down

echo "→ docker compose build --no-cache"
docker compose build --no-cache

echo "→ docker compose up -d"
docker compose up -d

echo "→ docker compose ps"
docker compose ps

echo "=== Deploy complete ==="
