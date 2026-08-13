#!/bin/sh

# Coolify's supported custom Docker options do not include resource limits for
# this application type. Re-apply the production envelope after deploys and
# host restarts, and fail loudly if Coolify ever creates an overlap.

set -eu

label="coolify.name=vamkwvui8e3cq8kkjxdo3zka"
containers=$(docker ps --filter "label=$label" --format '{{.Names}}')
count=$(printf '%s\n' "$containers" | sed '/^$/d' | wc -l | tr -d ' ')

if [ "$count" -eq 0 ]; then
    echo "Ledgerly container is not running; limits will be applied on the next timer tick"
    exit 0
fi
if [ "$count" -ne 1 ]; then
    echo "Expected exactly one Ledgerly container; found $count" >&2
    exit 1
fi

container="$containers"
docker update --memory=512m --memory-swap=512m --cpus=1 --pids-limit=256 "$container" >/dev/null

memory=$(docker inspect -f '{{.HostConfig.Memory}}' "$container")
swap=$(docker inspect -f '{{.HostConfig.MemorySwap}}' "$container")
cpus=$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$container")
pids=$(docker inspect -f '{{.HostConfig.PidsLimit}}' "$container")
[ "$memory" = "536870912" ] && [ "$swap" = "536870912" ] \
    && [ "$cpus" = "1000000000" ] && [ "$pids" = "256" ] || {
        echo "Ledgerly resource limit assertion failed" >&2
        exit 1
    }
echo "Ledgerly limits verified: memory=512MiB swap=512MiB cpus=1 pids=256"
