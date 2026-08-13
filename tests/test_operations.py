from datetime import datetime, timedelta, timezone

from operations import classify_operational_health


def test_worker_failure_and_staleness_affect_overall_health():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    health = classify_operational_health(
        {
            "pending_count": 0,
            "failed_count": 0,
            "oldest_pending_at": None,
            "max_attempt_count": 0,
        },
        {
            "gmail": {
                "last_heartbeat_at": (now - timedelta(minutes=25)).isoformat(),
                "consecutive_failures": 0,
                "last_error": None,
                "metadata": {"poll_interval_seconds": 300},
            }
        },
        now=now,
    )
    assert health["status"] == "critical"
    assert health["workers"]["gmail"]["status"] == "critical"


def test_outbox_retry_threshold_is_critical():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    health = classify_operational_health(
        {
            "pending_count": 1,
            "failed_count": 1,
            "oldest_pending_at": now.isoformat(),
            "max_attempt_count": 8,
        },
        {},
        now=now,
    )
    assert health["status"] == "critical"
    assert health["outbox"]["status"] == "critical"


def test_email_poison_state_affects_health_without_fake_imap_failures():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    health = classify_operational_health(
        {
            "pending_count": 0,
            "failed_count": 0,
            "oldest_pending_at": None,
            "max_attempt_count": 0,
        },
        {
            "gmail": {
                "last_heartbeat_at": now.isoformat(),
                "last_success_at": now.isoformat(),
                "consecutive_failures": 0,
                "last_error": None,
                "metadata": {
                    "poll_interval_seconds": 300,
                    "processing_failures": {
                        "retrying": 0,
                        "degraded": 1,
                        "terminal": 0,
                    },
                },
            }
        },
        now=now,
    )
    assert health["status"] == "degraded"
    assert health["workers"]["gmail"]["reason"] == (
        "repeated email processing failure present"
    )


def test_missing_required_backup_heartbeat_is_visible():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    health = classify_operational_health(
        {
            "pending_count": 0,
            "failed_count": 0,
            "oldest_pending_at": None,
            "max_attempt_count": 0,
        },
        {},
        now=now,
    )
    assert health["status"] == "degraded"
    assert health["workers"]["backup"]["reason"] == "no heartbeat recorded"
