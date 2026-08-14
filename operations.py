"""Operational-health classification shared by API and user interfaces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


_STALE_THRESHOLDS: dict[str, tuple[int, int]] = {
    "app_loop": (30, 45),
    "notion_sync": (45, 90),
    "gmail": (660, 1260),
    "reconciliation": (900, 1800),
    "backup": (26 * 3600, 72 * 3600),
    "telegram_webhook": (150, 300),
}
_LEVELS = {"unknown": 0, "ok": 1, "degraded": 2, "critical": 3}
# The application workers are created dynamically based on configured
# integrations. The host-level backup timer is the one required control that
# is otherwise completely invisible when it has never run.
_REQUIRED_WORKERS = ("backup",)

TELEGRAM_WEBHOOK_CHECK_INTERVAL_SECONDS = 60
TELEGRAM_WEBHOOK_CHECK_TIMEOUT_SECONDS = 10
TELEGRAM_WEBHOOK_PENDING_DEGRADED = 10
TELEGRAM_WEBHOOK_RECENT_ERROR_SECONDS = 15 * 60


def _telegram_error_age_seconds(value: Any, now: datetime) -> int | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now - parsed.astimezone(timezone.utc)).total_seconds()))


def evaluate_telegram_webhook(
    info: Any,
    *,
    expected_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reduce Telegram webhook data to a secret-free operational signal."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    actual_url = str(getattr(info, "url", "") or "")
    try:
        pending = max(0, int(getattr(info, "pending_update_count", 0) or 0))
    except (TypeError, ValueError):
        pending = 0
    last_error_date = getattr(info, "last_error_date", None)
    last_error_reported = bool(
        last_error_date or getattr(info, "last_error_message", None)
    )
    last_error_age = _telegram_error_age_seconds(last_error_date, now)
    recent_error = last_error_reported and (
        last_error_age is None
        or last_error_age <= TELEGRAM_WEBHOOK_RECENT_ERROR_SECONDS
    )
    url_matches = actual_url == expected_url

    error: str | None = None
    if not url_matches:
        error = "Telegram webhook URL does not match configured endpoint"
    elif pending >= TELEGRAM_WEBHOOK_PENDING_DEGRADED:
        error = "Telegram webhook pending-update threshold exceeded"
    elif recent_error:
        error = "Telegram reports a recent webhook delivery error"

    return {
        "success": error is None,
        "error": error,
        "metadata": {
            "check_completed": True,
            "url_matches": url_matches,
            "pending_update_count": pending,
            "pending_degraded_threshold": TELEGRAM_WEBHOOK_PENDING_DEGRADED,
            "last_error_reported": last_error_reported,
            "last_error_age_seconds": last_error_age,
            "recent_error_window_seconds": TELEGRAM_WEBHOOK_RECENT_ERROR_SECONDS,
        },
    }


async def inspect_telegram_webhook(
    bot: Any,
    *,
    expected_url: str,
    timeout_seconds: float = TELEGRAM_WEBHOOK_CHECK_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch Telegram webhook state with a strict timeout and sanitized errors."""
    try:
        info = await asyncio.wait_for(
            bot.get_webhook_info(), timeout=timeout_seconds
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "success": False,
            "error": f"Telegram webhook inspection failed ({type(exc).__name__})",
            "metadata": {
                "check_completed": False,
                "url_matches": None,
                "pending_update_count": None,
                "last_error_reported": None,
                "last_error_age_seconds": None,
            },
        }
    return evaluate_telegram_webhook(info, expected_url=expected_url, now=now)


async def record_telegram_webhook_check(
    bot: Any,
    record_state: Any,
    *,
    expected_url: str,
    timeout_seconds: float = TELEGRAM_WEBHOOK_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Persist one sanitized webhook inspection in operational health."""
    result = await inspect_telegram_webhook(
        bot,
        expected_url=expected_url,
        timeout_seconds=timeout_seconds,
    )
    await record_state(
        "telegram_webhook",
        success=result["success"],
        error=result["error"],
        metadata=result["metadata"],
    )
    return result


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

    # Missing rows are meaningful: a worker or the external backup timer that
    # has never emitted a heartbeat must not disappear from an otherwise green
    # health response.
    for name in _REQUIRED_WORKERS:
        if name not in workers:
            classified_workers[name] = {
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": None,
                "started_at": None,
                "last_heartbeat_at": None,
                "consecutive_failures": 0,
                "metadata": {},
                "status": "degraded",
                "reason": "no heartbeat recorded",
                "age_seconds": None,
            }
            overall = "degraded"

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
