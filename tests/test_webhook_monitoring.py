import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from operations import (
    TELEGRAM_WEBHOOK_PENDING_DEGRADED,
    classify_operational_health,
    evaluate_telegram_webhook,
    inspect_telegram_webhook,
    record_telegram_webhook_check,
)


EXPECTED_URL = "https://ledger.example/secret-webhook-path"


def webhook_info(**overrides):
    values = {
        "url": EXPECTED_URL,
        "pending_update_count": 0,
        "last_error_date": None,
        "last_error_message": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_webhook_evaluation_exposes_health_without_url_or_error_text():
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    result = evaluate_telegram_webhook(
        webhook_info(
            pending_update_count=TELEGRAM_WEBHOOK_PENDING_DEGRADED,
            last_error_date=now - timedelta(seconds=30),
            last_error_message="proxy rejected token=do-not-leak",
        ),
        expected_url=EXPECTED_URL,
        now=now,
    )

    assert result["success"] is False
    assert result["metadata"]["url_matches"] is True
    assert result["metadata"]["pending_update_count"] == 10
    assert result["metadata"]["last_error_age_seconds"] == 30
    serialized = repr(result)
    assert EXPECTED_URL not in serialized
    assert "do-not-leak" not in serialized


def test_webhook_evaluation_detects_mismatch_without_exposing_actual_url():
    actual_url = "https://attacker.example/stolen-secret-path"
    result = evaluate_telegram_webhook(
        webhook_info(url=actual_url),
        expected_url=EXPECTED_URL,
    )

    assert result["success"] is False
    assert result["metadata"]["url_matches"] is False
    assert actual_url not in repr(result)
    assert EXPECTED_URL not in repr(result)


def test_stale_telegram_error_remains_visible_but_does_not_degrade_health():
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    result = evaluate_telegram_webhook(
        webhook_info(
            last_error_date=now - timedelta(hours=1),
            last_error_message="old delivery failure",
        ),
        expected_url=EXPECTED_URL,
        now=now,
    )

    assert result["success"] is True
    assert result["metadata"]["last_error_reported"] is True
    assert result["metadata"]["last_error_age_seconds"] == 3600


def test_stale_webhook_monitor_is_visible_in_operational_health():
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    health = classify_operational_health(
        {
            "pending_count": 0,
            "failed_count": 0,
            "oldest_pending_at": None,
            "max_attempt_count": 0,
        },
        {
            "backup": {
                "last_heartbeat_at": now.isoformat(),
                "consecutive_failures": 0,
                "metadata": {},
            },
            "telegram_webhook": {
                "last_heartbeat_at": (now - timedelta(seconds=151)).isoformat(),
                "consecutive_failures": 0,
                "metadata": {"url_matches": True},
            },
        },
        now=now,
    )

    webhook = health["workers"]["telegram_webhook"]
    assert health["status"] == "degraded"
    assert webhook["status"] == "degraded"
    assert webhook["reason"] == "heartbeat stale for 151s"


@pytest.mark.asyncio
async def test_webhook_inspection_sanitizes_api_exception():
    class FailingBot:
        async def get_webhook_info(self):
            raise RuntimeError("token=telegram-secret")

    result = await inspect_telegram_webhook(
        FailingBot(), expected_url=EXPECTED_URL
    )

    assert result["success"] is False
    assert result["metadata"]["check_completed"] is False
    assert "telegram-secret" not in repr(result)
    assert "RuntimeError" in result["error"]


@pytest.mark.asyncio
async def test_webhook_inspection_is_time_bounded():
    class HangingBot:
        async def get_webhook_info(self):
            await asyncio.Event().wait()

    result = await inspect_telegram_webhook(
        HangingBot(), expected_url=EXPECTED_URL, timeout_seconds=0.01
    )

    assert result["success"] is False
    assert result["metadata"]["check_completed"] is False
    assert "TimeoutError" in result["error"]


@pytest.mark.asyncio
async def test_webhook_check_records_only_sanitized_operational_state():
    class HealthyBot:
        async def get_webhook_info(self):
            return webhook_info(pending_update_count=2)

    calls = []

    async def record_state(name, **values):
        calls.append((name, values))

    result = await record_telegram_webhook_check(
        HealthyBot(), record_state, expected_url=EXPECTED_URL
    )

    assert result["success"] is True
    assert calls == [
        (
            "telegram_webhook",
            {
                "success": True,
                "error": None,
                "metadata": result["metadata"],
            },
        )
    ]
    assert EXPECTED_URL not in repr(calls)
