# SQLite Restore Drill

## 2026-07-29

A disposable database was created under `/tmp` with one transaction row,
backed up using `python -m scripts.sqlite_backup backup`, and restored into a
new path using `python -m scripts.sqlite_backup restore`.

Verification succeeded:

- restored `PRAGMA integrity_check` returned `ok`;
- transaction `drill-tx-1` retained its `125000` IDR amount;
- the source, backup, and restored database used separate paths;
- no live file under `data/` was read, replaced, or stopped.

For a production recovery, stop the service before replacing an existing
database, keep the previous database and WAL sidecars until validation is
complete, restore into a new path first, and start the application against the
restored copy only after `integrity_check` succeeds.
