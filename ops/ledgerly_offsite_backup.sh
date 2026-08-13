#!/bin/sh

# Create the verified local backup, encrypt its database and metadata with the
# recovery public key, and copy both artifacts to a separate SSH host. The
# application health heartbeat is marked successful only after both remote
# checksums match the local encrypted files.

set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
backup_dir="${repo_root}/data/backups"
gpg_home="${LEDGERLY_BACKUP_GPG_HOME:-/etc/ledgerly-backup/gnupg}"
recipient_file="${LEDGERLY_BACKUP_RECIPIENT_FILE:-/etc/ledgerly-backup/recipient.asc}"
recipient_fingerprint="${LEDGERLY_BACKUP_RECIPIENT_FINGERPRINT:?LEDGERLY_BACKUP_RECIPIENT_FINGERPRINT is required}"
offsite_host="${LEDGERLY_OFFSITE_HOST:-vps.shelterinteliguardsystem.site}"
offsite_user="${LEDGERLY_OFFSITE_USER:-hik}"
offsite_path="${LEDGERLY_OFFSITE_PATH:-/home/hik/ledgerly-backups}"
keep_days="${LEDGERLY_OFFSITE_KEEP_DAYS:-45}"

case "$offsite_host" in
    ""|*[!A-Za-z0-9._-]*) echo "Invalid off-site SSH host" >&2; exit 2 ;;
esac
case "$offsite_user" in
    ""|*[!A-Za-z0-9_-]*) echo "Invalid off-site SSH user" >&2; exit 2 ;;
esac
case "$offsite_path" in
    /home/hik/ledgerly-backups) ;;
    *) echo "Refusing unexpected off-site path: $offsite_path" >&2; exit 2 ;;
esac
case "$keep_days" in
    ''|*[!0-9]*) echo "Invalid off-site retention" >&2; exit 2 ;;
esac

ssh_target="${offsite_user}@${offsite_host}"
staging_dir=$(mktemp -d "${TMPDIR:-/tmp}/ledgerly-offsite.XXXXXX")
status_recorded=0
container=""
latest_db=""
db_name=""

cleanup() { rm -rf "$staging_dir"; }

record_status() {
    success="$1"
    error_message="${2:-}"
    [ -n "$container" ] || return 0
    args="offsite-status --source /app/data/expense_tracker.db --destination /app/data/backups/${db_name:-unknown}"
    if [ "$success" = "true" ]; then
        docker exec --user appuser "$container" python -m scripts.sqlite_backup $args \
            --success --offsite-host "$offsite_host" --offsite-path "$offsite_path" >/dev/null
    else
        docker exec --user appuser "$container" python -m scripts.sqlite_backup $args \
            --failure --error "$error_message" --offsite-host "$offsite_host" \
            --offsite-path "$offsite_path" >/dev/null || true
    fi
}

on_exit() {
    rc=$?
    if [ "$status_recorded" -eq 0 ] && [ -n "$latest_db" ]; then
        record_status false "off-site backup failed with exit code $rc"
    fi
    cleanup
    exit "$rc"
}
trap on_exit EXIT INT TERM

"$repo_root/ops/ledgerly_backup.sh"

container_list=$(docker ps --filter "label=coolify.name=vamkwvui8e3cq8kkjxdo3zka" --format '{{.Names}}')
container_count=$(printf '%s\n' "$container_list" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$container_count" -ne 1 ]; then
    echo "Expected exactly one running Ledgerly Coolify container; found $container_count" >&2
    exit 1
fi
container="$container_list"

latest_db=$(find "$backup_dir" -maxdepth 1 -type f -name 'expense_tracker-*.db' -printf '%T@ %p\n' \
    | sort -nr | sed -n '1s/^[^ ]* //p')
if [ -z "$latest_db" ]; then
    echo "No verified local backup was created" >&2
    exit 1
fi
db_name=$(basename "$latest_db")
metadata_path="${latest_db}.json"
case "$db_name" in
    expense_tracker-*.db) ;;
    *) echo "Unexpected backup name: $db_name" >&2; exit 2 ;;
esac
[ -f "$metadata_path" ] || { echo "Missing backup metadata: $metadata_path" >&2; exit 1; }

mkdir -p "$gpg_home"
chmod 700 "$gpg_home"
gpg --homedir "$gpg_home" --batch --no-tty --import "$recipient_file" >/dev/null 2>&1
gpg --homedir "$gpg_home" --batch --no-tty --list-keys "$recipient_fingerprint" >/dev/null

encrypted_db="${db_name}.gpg"
encrypted_metadata="$(basename "$metadata_path").gpg"
gpg --homedir "$gpg_home" --batch --yes --no-tty --trust-model always \
    --recipient "$recipient_fingerprint" --output "$staging_dir/$encrypted_db" \
    --encrypt "$latest_db"
gpg --homedir "$gpg_home" --batch --yes --no-tty --trust-model always \
    --recipient "$recipient_fingerprint" --output "$staging_dir/$encrypted_metadata" \
    --encrypt "$metadata_path"

ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$ssh_target" \
    "umask 077; mkdir -p '$offsite_path/.incoming'; chmod 700 '$offsite_path' '$offsite_path/.incoming'"
scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$staging_dir/$encrypted_db" "$staging_dir/$encrypted_metadata" \
    "$ssh_target:$offsite_path/.incoming/"
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$ssh_target" \
    "mv '$offsite_path/.incoming/$encrypted_db' '$offsite_path/$encrypted_db'; \
     mv '$offsite_path/.incoming/$encrypted_metadata' '$offsite_path/$encrypted_metadata'"

for file in "$encrypted_db" "$encrypted_metadata"; do
    local_sha=$(sha256sum "$staging_dir/$file" | awk '{print $1}')
    remote_sha=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$ssh_target" \
        "sha256sum '$offsite_path/$file' | awk '{print \$1}'")
    [ "$local_sha" = "$remote_sha" ] || {
        echo "Remote checksum mismatch for $file" >&2
        exit 1
    }
done

ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$ssh_target" \
    "find '$offsite_path' -maxdepth 1 -type f -name 'expense_tracker-*.gpg' -mtime +$keep_days -delete"
record_status true
status_recorded=1
echo "Off-site encrypted backup verified: $db_name -> $ssh_target:$offsite_path"
