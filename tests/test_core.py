"""
Tests for db.py and models.py — core data layer.

Run with: .venv/bin/pytest tests/ -v
"""
import asyncio
import math
import os
import tempfile
from typing import cast

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from models import ExpenseEntry, IncomeEntry, QueryIntent, EmailTransaction, NotionCache, _fuzzy_match, format_self_transfer_label
from email_watcher import EmailWatcher, _is_jago_pocket_transfer
from agent import _looks_like_self_transfer
from db import Database
from notion import _coerce_date


# ── models.py tests ────────────────────────────────────────────────────────────

class TestExpenseEntry:
    def test_valid_entry(self):
        e = ExpenseEntry(
            description="Nasi goreng",
            amount=25000,
            date="2026-06-17",
            subcategory="Warung/Makan Siap Saji",
            account="Cash",
            confidence=0.95,
            merchant="Warung Pak Kumis",
        )
        assert e.amount == 25000
        assert e.description == "Nasi goreng"
        assert e.merchant == "Warung Pak Kumis"

    def test_amount_must_be_positive(self):
        with pytest.raises(Exception):
            ExpenseEntry(
                description="Test",
                amount=-100,
                date="2026-06-17",
                subcategory="Groceries",
                account="Cash",
                confidence=0.5,
            )

    def test_amount_must_be_finite(self):
        with pytest.raises(Exception):
            ExpenseEntry(
                description="Test",
                amount=math.inf,
                date="2026-06-17",
                subcategory="Groceries",
                account="Cash",
                confidence=0.5,
            )

    def test_amount_rejects_nan(self):
        with pytest.raises(Exception):
            ExpenseEntry(
                description="Test",
                amount=math.nan,
                date="2026-06-17",
                subcategory="Groceries",
                account="Cash",
                confidence=0.5,
            )

    def test_amount_rejects_zero(self):
        with pytest.raises(Exception):
            ExpenseEntry(
                description="Test",
                amount=0,
                date="2026-06-17",
                subcategory="Groceries",
                account="Cash",
                confidence=0.5,
            )

    def test_model_dump_json_roundtrip(self):
        e = ExpenseEntry(
            description="Indomie",
            amount=5000,
            date="2026-06-17",
            subcategory="Groceries",
            account="Jago",
            confidence=0.9,
        )
        raw = e.model_dump_json()
        e2 = ExpenseEntry.model_validate_json(raw)
        assert e2.amount == 5000
        assert e2.description == "Indomie"
        assert e2.date == "2026-06-17"


class TestIncomeEntry:
    def test_valid_income(self):
        e = IncomeEntry(
            description="Gaji Juni",
            amount=3000000,
            date="2026-06-01",
            subcategory="Salary",
            account="Mandiri",
            confidence=1.0,
        )
        assert e.amount == 3000000

    def test_amount_validation(self):
        with pytest.raises(Exception):
            IncomeEntry(
                description="Bad",
                amount=-1,
                date="2026-06-01",
                subcategory="Other",
                account="Cash",
                confidence=0.5,
            )


class TestQueryIntent:
    def test_from_json(self):
        obj = QueryIntent(type="query", text="Berapa pengeluaran bulan ini?")
        assert obj.type == "query"
        assert "pengeluaran" in obj.text


class TestEmailTransaction:
    def test_expense_type(self):
        tx = EmailTransaction(
            type="expense",
            description="Warung Padang",
            amount=35000,
            admin_fee=0,
            date="2026-06-17",
            subcategory="Groceries",
            account="Mandiri",
        )
        assert tx.type == "expense"
        assert tx.admin_fee == 0

    def test_self_transfer(self):
        tx = EmailTransaction(
            type="self_transfer",
            description="Transfer ke BSI",
            amount=500000,
            admin_fee=2500,
            date="2026-06-17",
            subcategory="Transfer of Wealth",
            account="Mandiri",
            recipient_bank="BSI",
            source_account="Mandiri",
            destination_account="BSI",
            income_subcategory="Transfer",
        )
        assert tx.admin_fee == 2500
        assert tx.recipient_bank == "BSI"
        assert tx.source_account == "Mandiri"
        assert tx.destination_account == "BSI"
        assert tx.income_subcategory == "Transfer"

    def test_self_transfer_label(self):
        assert format_self_transfer_label("Mandiri", "Jago", "out") == "Transfer antar rekening — Mandiri → Jago (keluar)"
        assert format_self_transfer_label("Mandiri", "Jago", "in") == "Transfer antar rekening — Mandiri → Jago (masuk)"
        assert format_self_transfer_label("Mandiri", "Jago", "fee") == "Biaya admin transfer — Mandiri → Jago"

    def test_jago_pocket_transfer_skip(self):
        subject = "Transfer sendiri Rp 500,000 → rekening sendiri"
        body = "Rp500.000 telah dipindahkan dari Kantong Utama ke Kantong Spending."
        assert _is_jago_pocket_transfer("noreply@jago.com", subject, body) is True
        assert _is_jago_pocket_transfer("noreply@jago.com", "Bukti transfer", "Pindah saldo ke bank lain") is False

    def test_explicit_own_account_transfer_language_is_detectable(self):
        assert _looks_like_self_transfer(
            "Transfer berhasil",
            "Dana dipindahkan ke rekening sendiri dari Mandiri ke Jago",
        )
        assert not _looks_like_self_transfer(
            "Pembayaran",
            "Transfer ke toko langganan",
        )

    def test_skip(self):
        tx = EmailTransaction(
            type="skip",
            description="",
            amount=0,
            admin_fee=0,
            date="2026-06-17",
            subcategory="",
            account="",
            skip_reason="failed transaction",
        )
        assert tx.type == "skip"


class TestFuzzyMatch:
    def test_exact_match(self):
        options = {"Groceries": "url1", "Transport": "url2"}
        result = _fuzzy_match("groceries", options)
        assert result is not None
        assert result[0] == "Groceries"

    def test_case_insensitive(self):
        options = {"Groceries": "url1"}
        result = _fuzzy_match("GROCERIES", options)
        assert result is not None
        assert result[0] == "Groceries"

    def test_prefix_match(self):
        options = {"Groceries & More": "url1", "Transport": "url2"}
        result = _fuzzy_match("groceries", options)
        assert result is not None
        assert "Groceries" in result[0]

    def test_no_match(self):
        options = {"Groceries": "url1"}
        assert _fuzzy_match("xyz", options) is None

    def test_empty_name(self):
        options = {"Groceries": "url1"}
        assert _fuzzy_match("", options) is None

    def test_empty_options(self):
        assert _fuzzy_match("groceries", {}) is None

    def test_single_char_no_partial(self):
        """Single char should not match partial (min 2 chars for prefix, 3 for partial)."""
        options = {"Groceries": "url1"}
        assert _fuzzy_match("g", options) is None

    def test_two_char_prefix_match(self):
        """2-char prefix should work (>= 2 char minimum for prefix match)."""
        options = {"Groceries": "url1", "Transport": "url2"}
        result = _fuzzy_match("gr", options)
        assert result is not None
        assert "Groceries" in result[0]

    def test_partial_match_min_3_chars(self):
        """Partial match requires >= 3 chars."""
        options = {"Groceries & More": "url1"}
        result = _fuzzy_match("mor", options)
        assert result is not None

    def test_partial_match_2_chars_rejected(self):
        """2-char partial match should NOT match (needs 3+)."""
        options = {"Groceries": "url1"}
        assert _fuzzy_match("es", options) is None


class TestNotionCache:
    def test_closest_subcategory(self):
        cache = NotionCache(subcategories={"Groceries": "url1", "Transport": "url2"})
        result = cache.closest_subcategory("groceries")
        assert result is not None
        assert result[0] == "Groceries"

    def test_closest_account(self):
        cache = NotionCache(accounts={"Mandiri": "url1", "Jago": "url2"})
        result = cache.closest_account("jago")
        assert result is not None
        assert result[0] == "Jago"

    def test_month_url(self):
        cache = NotionCache(months={"January": "url1"})
        result = cache.month_url("January")
        assert result is not None

    def test_year_url(self):
        cache = NotionCache(years={"2026": "url1"})
        result = cache.year_url("2026")
        assert result is not None

    def test_empty_cache_returns_none(self):
        cache = NotionCache()
        assert cache.closest_subcategory("food") is None
        assert cache.closest_account("mandiri") is None
        assert cache.month_url("January") is None
        assert cache.year_url("2026") is None


# ── db.py tests (async, require SQLite) ───────────────────────────────────────

@pytest_asyncio.fixture
async def db(monkeypatch):
    """Create a temporary database for each test."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = await Database.connect(path)
    yield database
    await database.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_processed_emails(db):
    assert not await db.is_processed("uid1")
    await db.mark_processed("uid1", "sender@test.com")
    assert await db.is_processed("uid1")


@pytest.mark.asyncio
async def test_processed_emails_idempotent(db):
    await db.mark_processed("uid1", "sender@test.com")
    await db.mark_processed("uid1", "sender@test.com")  # should not raise
    assert await db.is_processed("uid1")


@pytest.mark.asyncio
async def test_pending_expense_crud(db):
    entry = ExpenseEntry(
        description="Nasi goreng",
        amount=25000,
        date="2026-06-17",
        subcategory="Warung/Makan Siap Saji",
        account="Cash",
        confidence=0.95,
    )
    await db.set_pending_expense(12345, entry)
    loaded = await db.get_pending_expense(12345)
    assert loaded is not None
    assert loaded.description == "Nasi goreng"
    assert loaded.amount == 25000
    assert loaded.merchant == ""

    await db.clear_pending_expense(12345)
    assert await db.get_pending_expense(12345) is None


@pytest.mark.asyncio
async def test_pending_expense_overwrite(db):
    """INSERT OR REPLACE — second write for same user_id overwrites."""
    e1 = ExpenseEntry(description="First", amount=1000, date="2026-06-17", subcategory="Groceries", account="Cash", confidence=0.5)
    e2 = ExpenseEntry(description="Second", amount=2000, date="2026-06-17", subcategory="Groceries", account="Cash", confidence=0.5)
    await db.set_pending_expense(111, e1)
    await db.set_pending_expense(111, e2)
    loaded = await db.get_pending_expense(111)
    assert loaded.description == "Second"
    assert loaded.amount == 2000


@pytest.mark.asyncio
async def test_pending_income_crud(db):
    entry = IncomeEntry(
        description="Gaji",
        amount=3000000,
        date="2026-06-01",
        subcategory="Salary",
        account="Mandiri",
        confidence=1.0,
    )
    await db.set_pending_income(12345, entry)
    loaded = await db.get_pending_income(12345)
    assert loaded is not None
    assert loaded.amount == 3000000

    await db.clear_pending_income(12345)
    assert await db.get_pending_income(12345) is None


@pytest.mark.asyncio
async def test_conversation_history(db):
    await db.append_history(12345, "user", "Hello")
    await db.append_history(12345, "assistant", "Hi!")
    history = await db.get_history(12345)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_conversation_history_limit(db):
    for i in range(25):
        await db.append_history(12345, "user", f"msg {i}")
    history = await db.get_history(12345, limit=20)
    assert len(history) == 20
    assert history[0]["content"] == "msg 5"
    assert history[-1]["content"] == "msg 24"


@pytest.mark.asyncio
async def test_debit_queue_fifo(db):
    tx1 = EmailTransaction(type="expense", description="First", amount=10000, admin_fee=0, date="2026-06-17", subcategory="Groceries", account="Jago", merchant="")
    tx2 = EmailTransaction(type="expense", description="Second", amount=20000, admin_fee=0, date="2026-06-17", subcategory="Groceries", account="Jago", merchant="")
    await db.push_debit(12345, tx1)
    await db.push_debit(12345, tx2)

    assert await db.debit_queue_depth(12345) == 2

    popped = await db.pop_debit(12345)
    assert popped.description == "First"

    popped2 = await db.pop_debit(12345)
    assert popped2.description == "Second"

    assert await db.debit_queue_depth(12345) == 0
    assert await db.pop_debit(12345) is None


@pytest.mark.asyncio
async def test_debit_merchant_cache(db):
    assert await db.get_debit_merchant(12345, 50000) is None
    await db.set_debit_merchant(12345, 50000, "Starbucks")
    assert await db.get_debit_merchant(12345, 50000) == "Starbucks"


@pytest.mark.asyncio
async def test_user_undo(db):
    await db.set_user_undo(12345, "page-id-123", "Nasi goreng", 25000, "2026-06-17", "Warung/Makan Siap Saji")
    record = await db.get_user_undo(12345)
    assert record is not None
    assert record["page_id"] == "page-id-123"
    assert record["description"] == "Nasi goreng"

    await db.clear_user_undo(12345)
    assert await db.get_user_undo(12345) is None


@pytest.mark.asyncio
async def test_upsert_user(db):
    await db.upsert_user(99999, owner_name="TestUser", notion_token="ntn_test123")
    user = await db.get_user(99999)
    assert user is not None
    assert user.owner_name == "TestUser"
    assert user.notion_token == "ntn_test123"
    assert user.setup_step == "start"


@pytest.mark.asyncio
async def test_upsert_user_rejects_bad_columns(db):
    with pytest.raises(ValueError, match="Unexpected user columns"):
        await db.upsert_user(99999, bad_field="oops")


@pytest.mark.asyncio
async def test_set_user_setup_step(db):
    # upsert_user requires owner_name and notion_token on first insert
    await db.upsert_user(88888, owner_name="Test", notion_token="ntn_x", setup_step="await_name")
    await db.set_user_setup_step(88888, "done")
    user = await db.get_user(88888)
    assert user.setup_step == "done"


@pytest.mark.asyncio
async def test_get_all_users(db):
    await db.upsert_user(111, owner_name="Alice", notion_token="ntn_a")
    await db.upsert_user(222, owner_name="Bob", notion_token="ntn_b")
    users = await db.get_all_users()
    assert len(users) == 2
    assert users[111].owner_name == "Alice"
    assert users[222].owner_name == "Bob"


@pytest.mark.asyncio
async def test_pending_since(db):
    await db.set_pending_since(12345, 1700000000.0)
    ts = await db.get_pending_since(12345)
    assert ts == 1700000000.0

    all_since = await db.get_all_pending_since()
    assert 12345 in all_since

    await db.clear_pending_since(12345)
    assert await db.get_pending_since(12345) is None


@pytest.mark.asyncio
async def test_email_account_owners(db):
    await db.set_email_account_owner("Mandiri", 12345)
    await db.set_email_account_owner("Jago", 12345)
    accounts = await db.get_email_accounts_for_user(12345)
    assert "Mandiri" in accounts
    assert "Jago" in accounts

    owner = await db.get_email_owner_for_account("Mandiri")
    assert owner == 12345

    # substring match
    owner2 = await db.get_email_owner_for_account("Mandiri 1854")
    assert owner2 == 12345

    await db.remove_email_account_owner("Mandiri")
    assert await db.get_email_owner_for_account("Mandiri") is None


@pytest.mark.asyncio
async def test_prune_processed(db):
    """Old entries should be prunable."""
    # Insert an old entry directly
    old_ts = "2020-01-01T00:00:00+00:00"
    await db._conn.execute(
        "INSERT OR REPLACE INTO processed_emails (uid, sender, processed_at) VALUES (?, ?, ?)",
        ("old-uid", "test@test.com", old_ts),
    )
    await db._conn.commit()
    assert await db.is_processed("old-uid")

    pruned = await db.prune_processed(days=90)
    assert pruned >= 1
    assert not await db.is_processed("old-uid")


# ── notion.py helper tests ────────────────────────────────────────────────────

class TestExtractMerchantFromDescription:
    def test_with_owner_prefix_and_dash(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("[Afif] SAKINAH SUPERMARKET")
        assert result == "SAKINAH SUPERMARKET"

    def test_with_owner_prefix_and_em_dash(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("[Afif] Warung Emak Keputih — nasi padang")
        assert result == "Warung Emak Keputih"

    def test_with_owner_prefix_and_hyphen(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("[Afif] Starbucks - kopi susu")
        assert result == "Starbucks"

    def test_no_owner_prefix(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("SAKINAH SUPERMARKET")
        assert result == "SAKINAH SUPERMARKET"

    def test_empty_string(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("")
        assert result == ""

    def test_only_owner_prefix(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("[Afif] ")
        assert result == ""

    def test_no_dash_returns_full(self):
        from notion import _extract_merchant_from_description
        result = _extract_merchant_from_description("[Afif] Indomaret")
        assert result == "Indomaret"


class TestUrlToId:
    def test_bare_id(self):
        from notion import _url_to_id
        result = _url_to_id("385c2adf84548161a518e2a4536f22b8")
        assert result == "385c2adf-8454-8161-a518-e2a4536f22b8"

    def test_url_with_dashes(self):
        from notion import _url_to_id
        url = "https://www.notion.so/385c2adf-8454-8161-a518-e2a4536f22b8"
        result = _url_to_id(url)
        assert result == "385c2adf-8454-8161-a518-e2a4536f22b8"

    def test_url_with_slug(self):
        from notion import _url_to_id
        url = "https://www.notion.so/Some-Title-385c2adf84548161a518e2a4536f22b8"
        result = _url_to_id(url)
        assert result == "385c2adf-8454-8161-a518-e2a4536f22b8"

    def test_url_with_trailing_slash(self):
        from notion import _url_to_id
        url = "https://www.notion.so/385c2adf-8454-8161-a518-e2a4536f22b8/"
        result = _url_to_id(url)
        assert result == "385c2adf-8454-8161-a518-e2a4536f22b8"


class TestParseDate:
    def test_valid_date(self):
        from notion import _parse_date
        year, month = _parse_date("2026-06-17")
        assert year == "2026"
        assert month == "June"

    def test_january(self):
        from notion import _parse_date
        year, month = _parse_date("2026-01-15")
        assert month == "January"

    def test_december(self):
        from notion import _parse_date
        year, month = _parse_date("2026-12-31")
        assert month == "December"

    def test_invalid_format(self):
        from notion import _parse_date
        import pytest
        with pytest.raises(ValueError):
            _parse_date("not-a-date")

    def test_month_out_of_range(self):
        from notion import _parse_date
        import pytest
        with pytest.raises(ValueError):
            _parse_date("2026-13-01")


# ── email watcher tests ─────────────────────────────────────────────────────

class TestEmailWatcher:
    def test_imap_fetch_records_final_error_after_retries(self, monkeypatch):
        watcher = EmailWatcher(
            config=object(),
            db=cast(Database, object()),
            notion=None,
            agent=None,
            cache_getter=lambda: None,
        )

        def fail(_processed_uids):
            raise RuntimeError("imap down")

        monkeypatch.setattr(watcher, "_imap_fetch_once", fail)
        monkeypatch.setattr("time.sleep", lambda _seconds: None)

        assert watcher._imap_fetch(set()) == []
        assert watcher._last_imap_error == "imap down"

    @pytest.mark.asyncio
    async def test_semantic_match_still_persists_before_notify(self):
        events = []

        class FakeDb:
            async def get_email_owner_for_account(self, _account_name):
                return 981749333

            async def get_pending_expense(self, _user_id):
                return None

            async def mark_processed(self, uid, sender):
                events.append(("mark_processed", uid, sender))

            async def create_confirmed_external_transaction(self, user_id, **kwargs):
                events.append(("ledger", user_id, kwargs["source_ref"]))
                return {"id": "local-tx-duplicate-looking"}, True

        class FakeNotion:
            async def fetch_duplicates(self, owner, amount, date):
                return ["M FAIRUZ HAFIDZUDDIN"]

        class FakeAgent:
            async def parse_bank_email(self, **_kwargs):
                return EmailTransaction(
                    type="expense",
                    description="M FAIRUZ HAFIDZUDDIN",
                    amount=200000,
                    admin_fee=0,
                    date="2026-07-17",
                    subcategory="Transfer Antar Rekening",
                    account="BSI 9400",
                    merchant="M FAIRUZ HAFIDZUDDIN",
                )

            async def check_duplicate(self, *_args, **_kwargs):
                return True

        class FakeCache:
            subcategories = {}
            accounts = {"BSI 9400": "account-url"}
            recurring_payments = {}

            def closest_subcategory(self, name):
                return (name, "subcat-url")

            def closest_account(self, name):
                return (name, "account-url")

        class FakeBot:
            async def send_message(self, *_args, **_kwargs):
                events.append(("notify",))

        async def user_data(_user_id):
            return FakeNotion(), FakeCache(), "Afif"

        watcher = EmailWatcher(
            config=object(),
            db=cast(Database, FakeDb()),
            notion=FakeNotion(),
            agent=FakeAgent(),
            cache_getter=lambda: FakeCache(),
            bot=FakeBot(),
            email_owner_id=981749333,
            email_owner_name="Afif",
            user_data_fn=user_data,
        )

        await watcher._process("66113", "nonereply.byondbybsi@bankbsi.co.id", "Transfer Berhasil", "body")

        assert events[:3] == [
            ("ledger", 981749333, "gmail:66113:expense"),
            ("mark_processed", "66113", "nonereply.byondbybsi@bankbsi.co.id"),
            ("notify",),
        ]

    @pytest.mark.asyncio
    async def test_auto_logged_email_marks_processed_before_side_effects(self):
        events = []

        class FakeDb:
            async def get_email_owner_for_account(self, _account_name):
                return 981749333

            async def get_pending_expense(self, _user_id):
                return None

            async def mark_processed(self, uid, sender):
                events.append(("mark_processed", uid, sender))

            async def create_confirmed_external_transaction(self, user_id, **kwargs):
                events.append(("ledger", user_id, kwargs["source_ref"]))
                return {"id": "local-tx-1"}, True

        class FakeNotion:
            async def fetch_duplicates(self, owner, amount, date):
                return []

            async def find_similar_by_merchant(self, *_args, **_kwargs):
                return []

        class FakeAgent:
            async def parse_bank_email(self, **_kwargs):
                return EmailTransaction(
                    type="expense",
                    description="SAKINAH SUPERMARKET",
                    amount=59900,
                    admin_fee=0,
                    date="2026-07-18",
                    subcategory="Groceries",
                    account="BSI 9400",
                    merchant="SAKINAH SUPERMARKET",
                )

        class FakeCache:
            subcategories = {}
            accounts = {"BSI 9400": "account-url"}
            recurring_payments = {}

            def closest_subcategory(self, name):
                return (name, "subcat-url")

            def closest_account(self, name):
                return (name, "account-url")

        class FakeBot:
            async def send_message(self, *_args, **_kwargs):
                events.append(("notify",))

        async def user_data(_user_id):
            return FakeNotion(), FakeCache(), "Afif"

        async def on_save(*_args, **_kwargs):
            events.append(("on_save",))

        watcher = EmailWatcher(
            config=object(),
            db=cast(Database, FakeDb()),
            notion=FakeNotion(),
            agent=FakeAgent(),
            cache_getter=lambda: FakeCache(),
            bot=FakeBot(),
            email_owner_id=981749333,
            email_owner_name="Afif",
            on_save_fn=on_save,
            user_data_fn=user_data,
        )

        await watcher._process("66114", "nonereply.byondbybsi@bankbsi.co.id", "Transaksi Berhasil", "body")

        assert events[:3] == [
            ("ledger", 981749333, "gmail:66114:expense"),
            ("mark_processed", "66114", "nonereply.byondbybsi@bankbsi.co.id"),
            ("notify",),
        ]

    @pytest.mark.asyncio
    async def test_self_transfer_queues_deterministic_ledger_components(self):
        refs = []
        events = []

        class FakeDb:
            async def get_email_owner_for_account(self, _account_name):
                return 981749333

            async def create_confirmed_external_transaction(self, _user_id, **kwargs):
                refs.append((kwargs["kind"], kwargs["source_ref"], kwargs["amount_idr"]))
                return {"id": kwargs["source_ref"]}, True

            async def mark_processed(self, uid, sender):
                events.append(("processed", uid, sender))

        class FakeNotion:
            async def fetch_duplicates(self, *_args, **_kwargs):
                return []

        class FakeAgent:
            async def parse_bank_email(self, **_kwargs):
                return EmailTransaction(
                    type="self_transfer",
                    description="Transfer antar rekening",
                    amount=500000,
                    admin_fee=2500,
                    date="2026-07-29",
                    subcategory="Transfer",
                    account="Mandiri",
                    source_account="Mandiri",
                    destination_account="BSI",
                    income_subcategory="Transfer",
                )

        class FakeBot:
            async def send_message(self, *_args, **_kwargs):
                events.append(("notify",))

        class FakeCache:
            accounts = {"Mandiri": "account-url"}
            recurring_payments = {}

        async def user_data(_user_id):
            return FakeNotion(), FakeCache(), "Afif"

        watcher = EmailWatcher(
            config=object(),
            db=cast(Database, FakeDb()),
            notion=FakeNotion(),
            agent=FakeAgent(),
            cache_getter=lambda: FakeCache(),
            bot=FakeBot(),
            email_owner_id=981749333,
            email_owner_name="Afif",
            user_data_fn=user_data,
        )
        await watcher._process(
            "66115", "no-reply@bankmandiri.co.id", "Transfer Berhasil", "body"
        )

        assert refs == [
            ("expense", "gmail:66115:transfer-out", 500000),
            ("income", "gmail:66115:transfer-in", 500000),
            ("expense", "gmail:66115:fee", 2500),
        ]
        assert events[0] == (
            "processed",
            "66115",
            "no-reply@bankmandiri.co.id",
        )

    @pytest.mark.asyncio
    async def test_budget_alert_resolves_fuzzy_subcategory(self):
        """Budget alert should fire when the model subcategory is fuzzy-matched to a budget subcategory."""
        events = []

        class FakeNotion:
            async def fetch_budgets(self, cache):
                return [
                    {
                        "name": "Makan",
                        "budget": 1000000,
                        "period": "monthly",
                        "spent": 950000,
                        "percentage": 95,
                        "subcategories": ["Warung/Makan Siap Saji"],
                    }
                ]

        class FakeCache:
            subcategories = {"Warung/Makan Siap Saji": "url1"}

            def closest_subcategory(self, name):
                return _fuzzy_match(name, self.subcategories)

        async def fake_alert(text):
            events.append(text)

        watcher = EmailWatcher(
            config=object(),
            db=cast(Database, object()),
            notion=FakeNotion(),
            agent=None,
            cache_getter=lambda: FakeCache(),
            alert_fn=fake_alert,
        )

        entry = ExpenseEntry(
            description="Nasi goreng",
            amount=25000,
            date="2026-07-20",
            subcategory="warung",  # fuzzy match to "Warung/Makan Siap Saji"
            account="Cash",
            confidence=0.9,
        )
        await watcher._check_budget_alert(entry)

        assert len(events) == 1
        assert "Makan" in events[0]


# ── merchant_patterns tests (async, require SQLite) ──────────────────────────

class TestMerchantPatterns:
    @pytest.mark.asyncio
    async def test_record_pattern_creates(self, db):
        await db.record_pattern(12345, "Starbucks", "Cafe/Coffee Shop", "Jago", 50000, "2026-06-24")
        result = await db.find_pattern(12345, 55000)
        assert result is not None
        assert result["merchant"] == "Starbucks"

    @pytest.mark.asyncio
    async def test_record_pattern_increments(self, db):
        await db.record_pattern(12345, "Starbucks", "Cafe/Coffee Shop", "Jago", 50000, "2026-06-20")
        await db.record_pattern(12345, "Starbucks", "Cafe/Coffee Shop", "Jago", 52000, "2026-06-24")
        result = await db.find_pattern(12345, 51000)
        assert result["merchant"] == "Starbucks"

    @pytest.mark.asyncio
    async def test_find_pattern_no_match(self, db):
        await db.record_pattern(12345, "Starbucks", "Cafe/Coffee Shop", "Jago", 50000, "2026-06-24")
        result = await db.find_pattern(12345, 999999)
        assert result is None

    @pytest.mark.asyncio
    async def test_find_pattern_no_patterns(self, db):
        result = await db.find_pattern(12345, 50000)
        assert result is None

    @pytest.mark.asyncio
    async def test_record_pattern_ignores_blank_merchant(self, db):
        await db.record_pattern(12345, "   ", "Cafe/Coffee Shop", "Jago", 50000, "2026-06-24")
        result = await db.find_pattern(12345, 50000)
        assert result is None


# ── notion date coercion tests ─────────────────────────────────────────────

class TestCoerceDate:
    def test_returns_string_unchanged(self):
        assert _coerce_date("2026-07-21") == "2026-07-21"

    def test_converts_date_object(self):
        from datetime import date
        assert _coerce_date(date(2026, 7, 21)) == "2026-07-21"

    def test_converts_datetime_object(self):
        from datetime import datetime
        assert _coerce_date(datetime(2026, 7, 21, 10, 30)) == "2026-07-21"

    def test_rejects_invalid_type(self):
        with pytest.raises(TypeError):
            _coerce_date(12345)
