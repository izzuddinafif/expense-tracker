import pytest

from config import _parse_telegram_ids


def test_telegram_allowlist_parser_normalizes_ids():
    assert _parse_telegram_ids(" 7,42,7 ") == frozenset({7, 42})


def test_telegram_allowlist_parser_rejects_non_numeric_ids():
    with pytest.raises(ValueError, match="numeric IDs"):
        _parse_telegram_ids("7,not-an-id")
