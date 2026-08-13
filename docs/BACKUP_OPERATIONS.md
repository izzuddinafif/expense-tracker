# Backup Operations

SQLite is authoritative, so keep verified copies outside the application host.
Run the retention command once daily only after its dry-run output is correct:

```ini
# /etc/systemd/system/ledgerly-backup.service
[Unit]
Description=Ledgerly SQLite backup and retention

[Service]
Type=oneshot
WorkingDirectory=/opt/expense-tracker
ExecStart=/opt/expense-tracker/.venv/bin/python -m scripts.sqlite_backup maintain --source data/expense_tracker.db --destination data/backups --daily 7 --weekly 4
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

Install these examples only after adjusting the absolute paths:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ledgerly-backup.timer
systemctl list-timers ledgerly-backup.timer
```

After each run, copy both the timestamped `.db` file and its `.json` metadata
to separate storage. Verify the copied SHA-256 against the metadata. At least
monthly, restore the newest off-host copy to a new path and follow
[`RESTORE_DRILL.md`](RESTORE_DRILL.md). Alert if no successful backup arrives
for 26 hours. Do not synchronize a live SQLite database file directly; copy
only completed backup artifacts.
