#!/bin/sh

# Run the verified SQLite backup utility inside the current Coolify Ledgerly
# container. Coolify replaces the container name on each deployment, so select
# it by its stable application label instead of hard-coding a generated name.

set -eu

application_label="coolify.name=vamkwvui8e3cq8kkjxdo3zka"
containers="$(docker ps --filter "label=${application_label}" --format '{{.Names}}')"
container_count="$(printf '%s\n' "$containers" | sed '/^$/d' | wc -l | tr -d ' ')"

if [ "$container_count" -ne 1 ]; then
    echo "Expected exactly one running Ledgerly Coolify container; found $container_count" >&2
    exit 1
fi
container="$containers"

exec docker exec --user appuser "$container" python -m scripts.sqlite_backup maintain \
    --source /app/data/expense_tracker.db \
    --destination /app/data/backups \
    --daily "${LEDGERLY_BACKUP_DAILY:-7}" \
    --weekly "${LEDGERLY_BACKUP_WEEKLY:-4}"
