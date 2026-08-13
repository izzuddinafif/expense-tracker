"""Operational-health classification shared by API and user interfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_STALE_THRESHOLDS: dict[str, tuple[int, int]] = {
    "app_loop": (30, 45),
    "notion_sync": (45, 90),
    "gmail": (660, 1260),
    "reconciliation": (900, 1800),
    "backup": (26 * 3600, 72 * 3600),
}
_LEVELS = {"unknown": 0, "ok": 1, "degraded": 2, "critical": 3}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def classify_operational_health(
    sync: dict[str, Any],
    workers: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify durable health signals without contacting dependencies."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    overall = "ok"
    classified_workers: dict[str, dict[str, Any]] = {}

    for name, worker in workers.items():
        value = dict(worker)
        heartbeat_at = value.get("last_heartbeat_at") or value.get("last_attempt_at")
        age = _age_seconds(heartbeat_at, now)
        failures = int(value.get("consecutive_failures") or 0)
        status = "ok"
        reason: str | None = None
        degraded_after, critical_after = _STALE_THRESHOLDS.get(
            name, (15 * 60, 30 * 60)
        )
        if name == "gmail":
            interval = int(value.get("metadata", {}).get("poll_interval_seconds") or 300)
            degraded_after, critical_after = 2 * interval + 60, 4 * interval + 60

        error = str(value.get("last_error") or "")
        processing_failures = value.get("metadata", {}).get(
            "processing_failures", {}
        )
        if name == "gmail" and int(processing_failures.get("terminal") or 0) > 0:
            status, reason = "critical", "terminal email processing failure present"
        elif name == "gmail" and int(processing_failures.get("degraded") or 0) > 0:
            status, reason = "degraded", "repeated email processing failure present"
        elif "auth" in error.lower() or "credential" in error.lower():
            status, reason = "critical", "authentication failure"
        elif failures >= 6:
            status, reason = "critical", f"{failures} consecutive failures"
        elif age is not None and age > critical_after:
            status, reason = "critical", f"heartbeat stale for {age}s"
        elif failures >= 1:
            status, reason = "degraded", f"{failures} consecutive failure(s)"
        elif age is not None and age > degraded_after:
            status, reason = "degraded", f"heartbeat stale for {age}s"
        elif age is None:
            status, reason = "unknown", "no heartbeat yet"

        value.update({"status": status, "reason": reason, "age_seconds": age})
        classified_workers[name] = value
        if _LEVELS[status] > _LEVELS[overall]:
            overall = status

    oldest_age = _age_seconds(sync.get("oldest_pending_at"), now)
    attempts = int(sync.get("max_attempt_count") or 0)
    failed = int(sync.get("failed_count") or 0)
    outbox_status = "ok"
    outbox_reason: str | None = None
    if (oldest_age is not None and oldest_age > 6 * 3600) or attempts >= 8:
        outbox_status = "critical"
        outbox_reason = "oldest pending item or retry count exceeded critical threshold"
    elif failed or (oldest_age is not None and oldest_age > 15 * 60) or attempts >= 3:
        outbox_status = "degraded"
        outbox_reason = "failed, stale, or repeatedly retried item present"
    if _LEVELS[outbox_status] > _LEVELS[overall]:
        overall = outbox_status

    return {
        "status": overall,
        "outbox": {
            "depth": int(sync.get("pending_count") or 0),
            "failed": failed,
            "oldest_pending_at": sync.get("oldest_pending_at"),
            "oldest_age_seconds": oldest_age,
            "max_attempt_count": attempts,
            "status": outbox_status,
            "reason": outbox_reason,
        },
        "workers": classified_workers,
        "evaluated_at": now.isoformat(),
    }
