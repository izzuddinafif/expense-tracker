# Production Verification

Run this read-only evidence check on SG after a Coolify deployment or host
restart. It reads neither `.env` files nor backup contents and does not print
secrets:

```bash
cd /home/afif/projects/expense-tracker
scripts/verify_ledgerly_production.sh
```

The operator needs permission to use Docker and query systemd. The script
checks that public `/livez` returns HTTP 200, exactly one labeled Coolify
container is running and healthy, `/app/data` is mounted writable, the ledger
database and backup directory are accessible, and the enforced resource limits
are 512 MiB memory/swap, 1 CPU, and 256 PIDs. It also records timer and last
service-result evidence for `ledgerly-backup` and `ledgerly-limits`.

The defaults match the current Ledgerly deployment. If SG uses a different
public endpoint or Coolify label, provide only these non-secret overrides:

```bash
LEDGERLY_LIVEZ_URL=https://public-host.example/livez \
LEDGERLY_COOLIFY_LABEL=coolify.name=application-id \
scripts/verify_ledgerly_production.sh
```

Do not put credentials, tokens, or query strings in `LEDGERLY_LIVEZ_URL`.
Any failed check is a failed handoff: preserve the output, inspect the relevant
Coolify deployment, timer, or container, then rerun the script after recovery.
Passing `/livez` proves process and database liveness only; it is not a
substitute for authenticated worker/outbox review or a restore drill.
