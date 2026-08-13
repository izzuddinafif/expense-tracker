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
ExecStart=/home/afif/projects/expense-tracker/ops/ledgerly_backup.sh
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

After each run, copy both the timestamped `.db` file and its `.json` metadata
to separate storage. Verify the copied SHA-256 against the metadata. The
current host timer creates and verifies local recovery artifacts; an off-host
destination still needs to be selected and configured before this operational
control is complete. At least monthly, restore the newest off-host copy to a
new path and follow
[`RESTORE_DRILL.md`](RESTORE_DRILL.md). Alert if no successful backup arrives
for 26 hours. Do not synchronize a live SQLite database file directly; copy
only completed backup artifacts.
