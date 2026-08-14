#!/usr/bin/env bash
# Read-only production evidence check. Run on SG as a user permitted to query
# Docker and systemd; it does not read .env files or print secret values.

set -Eeuo pipefail

readonly livez_url="${LEDGERLY_LIVEZ_URL:-https://ledgerly.izzudd.in/livez}"
readonly coolify_label="${LEDGERLY_COOLIFY_LABEL:-coolify.name=vamkwvui8e3cq8kkjxdo3zka}"
readonly max_backup_age_seconds="${LEDGERLY_MAX_BACKUP_AGE_SECONDS:-93600}"
failures=0

pass() {
    printf 'PASS  %s\n' "$*"
}

fail() {
    printf 'FAIL  %s\n' "$*" >&2
    failures=$((failures + 1))
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        printf 'Cannot run verification: required command is unavailable: %s\n' "$1" >&2
        exit 2
    }
}

service_evidence() {
    local timer="$1"
    local service="$2"
    local last_trigger result trigger_epoch now_epoch trigger_age

    if systemctl is-enabled --quiet "${timer}" && systemctl is-active --quiet "${timer}"; then
        pass "${timer} is enabled and active"
    else
        fail "${timer} is not both enabled and active"
    fi

    last_trigger="$(systemctl show "${timer}" --property=LastTriggerUSec --value 2>/dev/null || true)"
    if [[ -n "${last_trigger}" && "${last_trigger}" != "n/a" ]]; then
        now_epoch="$(date +%s)"
        trigger_epoch="$(date --date="${last_trigger}" +%s 2>/dev/null || true)"
        if [[ "${trigger_epoch}" =~ ^[0-9]+$ ]]; then
            trigger_age=$((now_epoch - trigger_epoch))
            if ((trigger_age >= 0 && trigger_age <= max_backup_age_seconds)); then
                pass "${timer} triggered ${trigger_age}s ago"
            else
                fail "${timer} last triggered ${trigger_age}s ago; maximum is ${max_backup_age_seconds}s"
            fi
        else
            fail "${timer} trigger timestamp could not be parsed"
        fi
    else
        fail "${timer} has no recorded trigger"
    fi

    result="$(systemctl show "${service}" --property=Result --value 2>/dev/null || true)"
    if [[ "${result}" == "success" ]]; then
        pass "${service} last result is success"
    else
        fail "${service} last result is ${result:-unavailable}"
    fi
}

require_command curl
require_command docker
require_command systemctl

case "${livez_url}" in
    http://*|https://*) ;;
    *) printf 'Cannot run verification: LEDGERLY_LIVEZ_URL must be an HTTP(S) URL\n' >&2; exit 2 ;;
esac
case "${livez_url}" in
    *'?'*|*'@'*) printf 'Cannot run verification: LEDGERLY_LIVEZ_URL must not contain credentials or a query string\n' >&2; exit 2 ;;
esac

http_code="$(curl --fail --silent --show-error --max-time 10 --output /dev/null --write-out '%{http_code}' "${livez_url}" || true)"
if [[ "${http_code}" == "200" ]]; then
    pass '/livez returned HTTP 200'
else
    fail "/livez returned ${http_code:-no response}"
fi

mapfile -t containers < <(docker ps --filter "label=${coolify_label}" --format '{{.ID}}')
if ((${#containers[@]} != 1)); then
    fail "expected exactly one running Coolify container for the configured label; found ${#containers[@]}"
    printf 'Production verification failed with %d failed check(s).\n' "${failures}" >&2
    exit 1
fi

container="${containers[0]}"
state="$(docker inspect --format '{{.State.Status}}' "${container}")"
health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}")"
if [[ "${state}" == "running" && "${health}" == "healthy" ]]; then
    pass 'Coolify container is running and Docker health is healthy'
else
    fail "Coolify container state=${state}, health=${health}"
fi

mapfile -t data_mounts < <(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}|{{.RW}}{{"\n"}}{{end}}{{end}}' "${container}")
if ((${#data_mounts[@]} == 1)) && [[ "${data_mounts[0]}" == *'|true' ]]; then
    pass 'container has one writable /app/data mount'
else
    fail 'container does not have exactly one writable /app/data mount'
fi

if ((${#data_mounts[@]} == 1)) && [[ "${data_mounts[0]}" == *'|true' ]]; then
    data_source="${data_mounts[0]%|*}"
    latest_backup="$(find "${data_source}/backups" -maxdepth 1 -type f -name 'expense_tracker-*.db' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
    if [[ -n "${latest_backup}" && -s "${latest_backup}" ]]; then
        now_epoch="$(date +%s)"
        backup_epoch="$(stat -c '%Y' "${latest_backup}" 2>/dev/null || true)"
        if [[ "${backup_epoch}" =~ ^[0-9]+$ ]]; then
            backup_age=$((now_epoch - backup_epoch))
            if ((backup_age >= 0 && backup_age <= max_backup_age_seconds)); then
                pass "latest local backup is ${backup_age}s old and non-empty"
            else
                fail "latest local backup is ${backup_age}s old; maximum is ${max_backup_age_seconds}s"
            fi
        else
            fail 'latest local backup timestamp could not be read'
        fi
    else
        fail 'no non-empty local backup artifact was found'
    fi
else
    fail 'cannot inspect backup recency without a writable data mount'
fi

if docker exec --user appuser "${container}" test -r /app/data/expense_tracker.db \
    && docker exec --user appuser "${container}" test -d /app/data/backups; then
    pass 'ledger database and backup directory are accessible in the data mount'
else
    fail 'ledger database or backup directory is inaccessible in the data mount'
fi

memory="$(docker inspect --format '{{.HostConfig.Memory}}' "${container}")"
swap="$(docker inspect --format '{{.HostConfig.MemorySwap}}' "${container}")"
cpus="$(docker inspect --format '{{.HostConfig.NanoCpus}}' "${container}")"
pids="$(docker inspect --format '{{.HostConfig.PidsLimit}}' "${container}")"
if [[ "${memory}" == "536870912" && "${swap}" == "536870912" && "${cpus}" == "1000000000" && "${pids}" == "256" ]]; then
    pass 'container limits match 512MiB memory/swap, 1 CPU, and 256 PIDs'
else
    fail 'container limits do not match the required production envelope'
fi

service_evidence ledgerly-backup.timer ledgerly-backup.service
service_evidence ledgerly-limits.timer ledgerly-limits.service

if ((failures > 0)); then
    printf 'Production verification failed with %d failed check(s).\n' "${failures}" >&2
    exit 1
fi

printf 'Production verification passed.\n'
