from datetime import datetime

from reporting_views import format_budget_report, format_monthly_stats, format_search_results


def test_search_escapes_markdown_and_limits_rows():
    text = format_search_results("a_b*", [{"amount_idr": 1200, "description": "A_[x]", "occurred_on": "2026-07-01"}], 1)
    assert 'a\\_b\\*' in text
    assert "A\\_\\[x]" in text


def test_monthly_stats_empty_and_december_projection():
    empty = format_monthly_stats({"expense": {"count": 0}}, None, datetime(2026, 7, 29))
    assert "Belum ada" in empty
    summary = {"expense": {"count": 1, "total_idr": 100000, "by_category": {"Makan_*": 100000}, "biggest": {"amount_idr": 100000, "description": "Lunch_[x]", "occurred_on": "2026-12-02"}}, "income": {"count": 0}}
    text = format_monthly_stats(summary, {"expense": {"total_idr": 50000}}, datetime(2026, 12, 2))
    assert "Desember 2026" in text and "Proyeksi" in text
    assert "Makan\\_\\*" in text and "Lunch\\_\\[x]" in text


def test_budget_report_renders_statuses_totals_and_escapes_categories():
    text = format_budget_report(
        [
            {"month": "2026-07", "category": "Makan_*", "budget_idr": 1_000_000, "spent_idr": 500_000, "remaining_idr": 500_000, "percentage": 50, "status": "ok"},
            {"month": "2026-07", "name": "Belanja", "budget": 200_000, "spent": 180_000, "status": "warning"},
            {"month": "2026-07", "category": "Tagihan", "budget_idr": 100_000, "spent_idr": 120_000, "remaining_idr": -20_000, "percentage": 120, "status": "over"},
        ]
    )
    assert "Budget Juli 2026" in text
    assert "Makan\\_\\*" in text
    assert "✅" in text and "⚠️" in text and "🔴" in text
    assert "Total: Rp 800,000 / Rp 1,300,000" in text


def test_budget_report_empty_state_is_safe_and_uses_requested_period():
    text = format_budget_report([], "2026-08")
    assert text == "💰 Belum ada budget untuk 2026-08."
