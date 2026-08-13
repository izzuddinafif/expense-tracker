# Backup Operations

SQLite is authoritative, so keep verified copies outside the application host.
Run the retention command once daily only after its dry-run output is correct.
For the current Coolify deployment, use the repository wrapper below so the
host targets the active generated container name through its stable Coolify
label:

```ini
# /etc/systemd/system/ledgerly-backup.service
[Unit]
Description=Ledgerly SQLite backup and retention
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=afif
WorkingDirectory=/home/afif/projects/expense-tracker
EnvironmentFile=-/etc/ledgerly-backup.env
ExecStart=/home/afif/projects/expense-tracker/ops/ledgerly_offsite_backup.sh
```

```ini
# /etc/systemd/system/ledgerly-backup.timer
[Unit]
Description=Run Ledgerly backup daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
```

For a non-container checkout, the equivalent service is:

```ini
# /etc/systemd/system/ledgerly-backup.service (non-container checkout)
[Service]
Type=oneshot
WorkingDirectory=/opt/expense-tracker
ExecStart=/opt/expense-tracker/.venv/bin/python -m scripts.sqlite_backup maintain --source data/expense_tracker.db --destination data/backups --daily 7 --weekly 4
```

Install these examples only after adjusting the absolute paths:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ledgerly-backup.timer
systemctl list-timers ledgerly-backup.timer
```

The production service runs the local verified backup first, encrypts both the
timestamped `.db` file and its `.json` metadata with a recovery public key, and
copies them to the separate `hik-vps` host over SSH. It verifies the SHA-256 of
both encrypted files on the remote host before marking the `backup` health
heartbeat successful. Configure `/etc/ledgerly-backup.env` with these values:

```ini
LEDGERLY_BACKUP_GPG_HOME=/etc/ledgerly-backup/gnupg
LEDGERLY_BACKUP_RECIPIENT_FILE=/etc/ledgerly-backup/recipient.asc
LEDGERLY_BACKUP_RECIPIENT_FINGERPRINT=<recovery-key-fingerprint>
LEDGERLY_OFFSITE_HOST=vps.shelterinteliguardsystem.site
LEDGERLY_OFFSITE_USER=hik
LEDGERLY_OFFSITE_PATH=/home/hik/ledgerly-backups
LEDGERLY_OFFSITE_KEEP_DAYS=45
```

The private recovery key is kept outside SG; store it securely at
`~/.config/ledgerly-backup/` on the operator workstation. Without that key,
off-site artifacts are intentionally undecryptable. At least monthly, restore
the newest off-site copy to a new path and follow
[`RESTORE_DRILL.md`](RESTORE_DRILL.md). Alert if no successful backup arrives
for 26 hours. Do not synchronize a live SQLite database file directly; copy
only completed backup artifacts.

Because Coolify's supported custom Docker option list excludes resource-limit
flags for this application type, SG also runs a host timer that reapplies and
asserts the production envelope after deploys and restarts:

```ini
# /etc/systemd/system/ledgerly-limits.service
[Unit]
Description=Apply Ledgerly container resource limits
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/home/afif/projects/expense-tracker/ops/ledgerly_apply_limits.sh
```

```ini
# /etc/systemd/system/ledgerly-limits.timer
[Unit]
Description=Keep Ledgerly container resource limits enforced

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```
