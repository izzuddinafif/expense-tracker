"""
Email watcher — polls Gmail via IMAP for bank transaction notifications
and auto-logs them to Notion.

Banks monitored:
  - Mandiri (Livin')     noreply.livin@bankmandiri.co.id
  - Jago Syariah         noreply@jago.com
  - BYOND by BSI         nonereply.byondbybsi@bankbsi.co.id

Setup: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env
  (Gmail App Password — enable 2FA first, then create at
   https://myaccount.google.com/apppasswords)

Recurring expenses are loaded from Notion's "Recurring Payment" database
(Active entries only). When a Jago debit card transaction matches a known
amount exactly, it is auto-logged with the correct name and linked back to
the Recurring Payment entry. Unknown amounts trigger a Telegram follow-up.
"""

import asyncio
import email
import email.header
import imaplib
import logging
import re
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Callable

from db import Database
from keyboards import make_category_keyboard, make_confirm_keyboard, make_email_edit_keyboard
from models import ExpenseEntry, IncomeEntry, EmailTransaction, NotionCache, format_self_transfer_label

from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

log = logging.getLogger(__name__)

# ── Bank sender addresses ──────────────────────────────────────────────────────

BANK_SENDERS = [
    "noreply.livin@bankmandiri.co.id",
    "noreply@jago.com",
    "nonereply.byondbybsi@bankbsi.co.id",
]

# Subjects containing these strings → always skip (failed transactions)
SKIP_SUBJECT_KEYWORDS = [
    "tidak berhasil",
    "gagal",
    "failed",
    "declined",
]

IMAP_HOST = "imap.gmail.com"
LOOKBACK_DAYS = 3    # scan last 3 days — catches emails after brief downtime

# Description the AI returns for Jago debit card emails (no merchant info)
JAGO_DEBIT_DESCRIPTION = "jago debit card transaction"


def _is_jago_pocket_transfer(sender: str, subject: str, body: str) -> bool:
    """Detect Jago internal pocket-to-pocket transfers that should be skipped.

    These are not bank transfers and should not create expense/income records.
    """
    sender_l = sender.lower()
    if "jago" not in sender_l:
        return False
    text = f"{subject}\n{body}".lower()
    if "kantong" not in text:
        return False
    pocket_phrases = (
        "pindah antar kantong",
        "antar kantong",
        "dipindahkan dari kantong",
        "dipindahkan",
        "pindah dana antar kantong",
    )
    return any(phrase in text for phrase in pocket_phrases)


# ── HTML → plain text ──────────────────────────────────────────────────────────

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True
        elif tag in ("br", "p", "tr", "div", "td", "th", "h1", "h2", "h3", "h4", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        raw = re.sub(r" {2,}", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    s = _Stripper()
    s.feed(html)
    return s.get_text()


# ── EmailWatcher ───────────────────────────────────────────────────────────────

class EmailWatcher:
    """
    Background task that polls Gmail via IMAP every `interval` seconds,
    parses bank notification emails, and logs transactions to Notion.

    Supports multi-user routing: after parsing, the account name is looked up
    in the email_account_owners DB table to find the target Telegram user.
    If no match is found, falls back to the default email owner.

    Parameters
    ----------
    config            : app Config
    db                : Database instance for persistence
    notion            : NotionClient instance (default email owner)
    agent             : Agent instance
    cache_getter      : zero-arg callable returning current NotionCache (default)
    bot               : aiogram Bot instance (for Telegram notifications)
    email_owner_id    : default Telegram user_id (fallback when no account match)
    email_owner_name  : default owner name string (fallback)
        user_data_fn      : optional async fn(telegram_id) -> (NotionClient, NotionCache, owner_name) | None
        on_save_fn        : optional async fn(user_id, page_url, description, amount, date, subcategory, merchant="")
                            called after every auto-logged expense/admin-fee with page details
        pending_since     : optional dict[int, float] for tracking when pending expenses are created
        alert_fn          : async fn(text) that broadcasts to all users
    """

    def __init__(
        self,
        config,
        db: Database,
        notion,
        agent,
        cache_getter: Callable,
        bot=None,
        email_owner_id: int | None = None,
        email_owner_name: str = "Afif",
        user_data_fn=None,
        on_save_fn=None,
        pending_since=None,
        alert_fn=None,
    ):
        self._config = config
        self._db = db
        self._notion = notion
        self._agent = agent
        self._cache_getter = cache_getter
        self._bot = bot
        self._owner_id = email_owner_id
        self._owner_name = email_owner_name
        self._user_data_fn = user_data_fn
        self._on_save_fn = on_save_fn
        self._pending_since = pending_since
        self._alert_fn = alert_fn
        self._imap: imaplib.IMAP4_SSL | None = None
        self._last_imap_error: str | None = None
        self._imap_fail_streak: int = 0
        self._notion_fail_streak: int = 0
        self._last_poll_time: float | None = None
        self._total_processed: int = 0
        self._start_time: float = time.time()

    # ── IMAP (synchronous — called via asyncio.to_thread) ──────────────────────

    @staticmethod
    def _is_imap_auth_error(error: object) -> bool:
        text = str(error)
        return "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text

    def _ensure_imap(self) -> imaplib.IMAP4_SSL:
        """Ensure IMAP connection is alive. Reconnect with backoff on failure."""
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except Exception:
                self._close_imap()

        last_err = None
        for attempt in range(3):
            try:
                imap = imaplib.IMAP4_SSL(IMAP_HOST)
                imap.login(self._config.gmail_address, self._config.gmail_app_password)
                imap.select("INBOX")
                self._imap = imap
                if attempt > 0:
                    log.info(f"IMAP reconnected after {attempt + 1} attempts")
                return imap
            except Exception as e:
                last_err = e
                log.warning(f"IMAP connect attempt {attempt + 1} failed: {e}")
                self._close_imap()
                if self._is_imap_auth_error(e):
                    # App-password failures are deterministic. Retrying/backing off inside
                    # the same poll only spams logs and delays the async watcher thread.
                    break
                import time as _t
                _t.sleep(2 ** attempt)  # 1s, 2s, 4s

        raise last_err

    def _close_imap(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def _imap_fetch(self, processed_uids: set[str]) -> list[tuple[str, str, str, str]]:
        """
        Connect to Gmail IMAP and fetch unprocessed emails from bank senders.
        Returns list of (uid, sender_email, subject, body_text).
        Retries once on connection failure and records the final error so the
        async polling loop can raise the configured IMAP failure alert.
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._imap_fetch_once(processed_uids)
            except Exception as e:
                last_error = e
                log.warning(f"IMAP fetch attempt {attempt + 1} failed: {e}")
                self._close_imap()
                if self._is_imap_auth_error(e):
                    # Invalid Gmail App Password will not recover on immediate retry.
                    break
                if attempt == 0:
                    import time as _t
                    _t.sleep(2)
        if last_error is not None:
            self._last_imap_error = str(last_error)
        return []

    def _imap_fetch_once(self, processed_uids: set[str]) -> list[tuple[str, str, str, str]]:
        results = []
        imap = self._ensure_imap()

        # Only fetch emails from the last LOOKBACK_DAYS days
        since = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")

        for sender in BANK_SENDERS:
            log.info(f"IMAP: searching FROM {sender} SINCE {since}")
            typ, data = imap.uid("search", None, f'FROM "{sender}" SINCE {since}')
            if typ != "OK" or not data[0]:
                continue

            raw = data[0]
            if isinstance(raw, (bytes, bytearray)):
                uids = raw.split()
            elif isinstance(raw, list):
                uids = raw
            elif isinstance(raw, int):
                uids = [str(raw).encode()]
            elif isinstance(raw, str):
                uids = raw.split()
            else:
                uids = []
            log.info(f"IMAP: found {len(uids)} UIDs for {sender}")
            for uid_bytes in uids[-100:]:
                if isinstance(uid_bytes, int):
                    uid = str(uid_bytes)
                    uid_bytes = uid.encode()
                elif isinstance(uid_bytes, bytes):
                    uid = uid_bytes.decode()
                else:
                    uid = str(uid_bytes)
                    uid_bytes = uid.encode()
                if uid in processed_uids:
                    continue

                typ2, msg_data = imap.uid("fetch", uid_bytes, "(RFC822)")
                if typ2 != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = self._decode_header(msg.get("Subject", ""))
                body = self._extract_body(msg)
                results.append((uid, sender, subject, body))

        return results

    def _decode_header(self, raw: str) -> str:
        parts = email.header.decode_header(raw)
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(part))
        return "".join(out)

    def _extract_body(self, msg: email.message.Message) -> str:
        html_body = ""
        text_body = ""

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                decoded = payload.decode(charset, errors="replace")
                if ct == "text/html" and not html_body:
                    html_body = decoded
                elif ct == "text/plain" and not text_body:
                    text_body = decoded
        else:
            ct = msg.get_content_type()
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True) or b""
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/html":
                html_body = decoded
            else:
                text_body = decoded

        return html_to_text(html_body) if html_body else text_body.strip()

    async def _check_budget_alert(self, entry: ExpenseEntry, notion=None, cache=None) -> None:
        try:
            notion = notion or self._notion
            cache = cache or self._cache_getter()
            budgets = await notion.fetch_budgets(cache)
            # The model may return an abbreviated or slightly different subcategory name
            # (e.g., "Cafe" vs "Cafe/Coffee Shop"). Resolve it to the canonical cache name
            # before comparing against the budget's subcategory list.
            canonical = entry.subcategory
            if cache is not None:
                matched = cache.closest_subcategory(entry.subcategory)
                if matched is not None:
                    canonical = matched[0]
            for b in budgets:
                if canonical not in b["subcategories"]:
                    continue
                pct = b["percentage"]
                if pct >= 100:
                    await self._alert(
                        f"🔴 *Budget Alert!* Anggaran *{b['name']}* sudah terlampaui!\n"
                        f"Rp {b['spent']:,.0f} / Rp {b['budget']:,.0f} ({pct:.0f}%)"
                    )
                elif pct >= 80:
                    await self._alert(
                        f"🟡 *Budget Alert!* Anggaran *{b['name']}* hampir habis.\n"
                        f"Rp {b['spent']:,.0f} / Rp {b['budget']:,.0f} ({pct:.0f}%)"
                    )
        except Exception as e:
            log.warning(f"Budget alert check failed: {e}")

    # ── Notification helpers ────────────────────────────────────────────────────

    async def _notify(self, text: str, user_id: int | None = None) -> None:
        target = user_id or self._owner_id
        if self._bot and target:
            try:
                await self._bot.send_message(
                    target, text, parse_mode="Markdown"
                )
            except Exception as e:
                log.error(f"Telegram notify failed: {e}")

    async def _notify_with_markup(self, text: str, markup: InlineKeyboardMarkup, user_id: int | None = None) -> None:
        target = user_id or self._owner_id
        if self._bot and target:
            try:
                await self._bot.send_message(
                    target, text, parse_mode="Markdown",
                    reply_markup=markup,
                )
            except Exception as e:
                log.error(f"Telegram notify failed: {e}")

    async def _alert(self, text: str) -> None:
        if self._alert_fn:
            try:
                await self._alert_fn(text)
            except Exception as e:
                log.error(f"alert_fn failed: {e}")
        else:
            await self._notify(text)

    async def _ask_debit_merchant(self, tx: EmailTransaction, user_id: int | None = None) -> bool:
        """
        For a Jago debit card tx with no merchant info:
        - check cache for known merchant name for this amount
        - if found, auto-create pending expense (no notification needed)
        - if not found: promote to pending_email_expense and notify
        - if a tx is already pending, queue it
        """
        target = user_id or self._owner_id
        if target is None:
            return False

        async def _set_pending_expense(entry: ExpenseEntry) -> None:
            await self._db.set_pending_expense(target, entry)
            if self._pending_since is not None:
                ts = time.time()
                self._pending_since[target] = ts
                await self._db.set_pending_since(target, ts)

        async def _clear_pending_expense() -> None:
            await self._db.clear_pending_expense(target)
            await self._db.clear_pending_since(target)
            if self._pending_since is not None:
                self._pending_since.pop(target, None)

        # Check cache for auto-learned merchant name
        cached = await self._db.get_debit_merchant(target, tx.amount)
        if cached:
            entry = ExpenseEntry(
                description=cached,
                amount=tx.amount,
                date=tx.date,
                subcategory=tx.subcategory,
                account=tx.account,
                confidence=0.9,
                merchant=cached,
            )
            await _set_pending_expense(entry)
            subcat_match = self._cache_getter().closest_subcategory(entry.subcategory)
            sub_text = subcat_match[0] if subcat_match else entry.subcategory
            acc_match = self._cache_getter().closest_account(entry.account)
            acc_text = acc_match[0] if acc_match else entry.account
            if self._bot and target:
                try:
                    await self._bot.send_message(
                        target,
                        f"💳 *Kartu debit Jago* — Rp {tx.amount:,.0f}\n"
                        f"🏷 {sub_text} · 🏦 {acc_text}\n"
                        f"📅 {tx.date}\n\n"
                        f"🔁 Merchant otomatis: *{cached}*\n"
                        f"Konfirmasi:",
                        parse_mode="Markdown",
                        reply_markup=make_confirm_keyboard(target),
                    )
                except Exception as e:
                    log.error(f"Telegram notify failed for cached debit: {e}")
                    await _clear_pending_expense()
                    return False
            return True

        # Check learned patterns for similar amounts
        pattern = await self._db.find_pattern(target, tx.amount)
        if pattern:
            entry = ExpenseEntry(
                description=pattern["merchant"],
                amount=tx.amount,
                date=tx.date,
                subcategory=pattern["subcategory"] or tx.subcategory,
                account=pattern["account"] or tx.account,
                confidence=0.8,
                merchant=pattern["merchant"],
            )
            await _set_pending_expense(entry)
            subcat_match = self._cache_getter().closest_subcategory(entry.subcategory)
            sub_text = subcat_match[0] if subcat_match else entry.subcategory
            acc_match = self._cache_getter().closest_account(entry.account)
            acc_text = acc_match[0] if acc_match else entry.account
            if self._bot and target:
                try:
                    await self._bot.send_message(
                        target,
                        f"💳 *Kartu debit Jago* — Rp {tx.amount:,.0f}\n"
                        f"🏷 {sub_text} · 🏦 {acc_text}\n"
                        f"📅 {tx.date}\n\n"
                        f"🔁 *Pola terdeteksi: {pattern['merchant']}*\n"
                        f"Konfirmasi:",
                        parse_mode="Markdown",
                        reply_markup=make_confirm_keyboard(target),
                    )
                except Exception as e:
                    log.error(f"Telegram notify failed for pattern match: {e}")
                    await _clear_pending_expense()
                    return False
            return True

        current = await self._db.get_pending_email_expense(target)
        if current is None:
            await self._db.set_pending_email_expense(target, tx)
            if self._bot:
                try:
                    await self._bot.send_message(
                        target,
                        f"💳 *Kartu debit Jago* — Rp {tx.amount:,.0f}\n"
                        f"📅 {tx.date}  🏦 {tx.account}\n\n"
                        f"Beli apa? Balas dengan nama merchant atau deskripsi.\n"
                        f"_(Ketik *batal* untuk lewati)_",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    log.error(f"Telegram notify failed for Jago debit follow-up: {e}")
                    await self._db.clear_pending_email_expense(target)
                    return False
            return True
        else:
            await self._db.push_debit(target, tx)
            depth = await self._db.debit_queue_depth(target)
            log.info(
                f"[email] Queued Jago debit card Rp {tx.amount:,.0f} "
                f"(queue depth: {depth})"
            )
            return True

    # ── User resolution (multi-user routing) ────────────────────────────────────

    async def _resolve_user(self, account_name: str) -> tuple[int, str, object, object] | None:
        """
        Look up the Telegram user who owns a given bank account.
        Returns (telegram_id, owner_name, NotionClient, NotionCache) or None.
        """
        telegram_id = await self._db.get_email_owner_for_account(account_name)
        if telegram_id is None:
            return None
        if self._user_data_fn:
            data = await self._user_data_fn(telegram_id)
            if data:
                return (telegram_id, data[2], data[0], data[1])
        return None

    # ── Email processing ────────────────────────────────────────────────────────

    async def _process(self, uid: str, sender: str, subject: str, body: str) -> None:
        subject_lower = subject.lower()
        if any(kw in subject_lower for kw in SKIP_SUBJECT_KEYWORDS):
            log.info(f"Skipping failed-transaction email [{uid}]: {subject}")
            await self._db.mark_processed(uid, sender)
            return

        cache = self._cache_getter()
        today = date.today().isoformat()

        try:
            if _is_jago_pocket_transfer(sender, subject, body):
                log.info(f"[email] Skipping Jago pocket transfer [{uid}]: {subject}")
                await self._db.mark_processed(uid, sender)
                return

            tx = await self._agent.parse_bank_email(
                subject=subject,
                body=body,
                sender=sender,
                cache=cache,
                today=today,
            )
        except Exception as e:
            log.error(f"parse_bank_email failed for [{uid}]: {e}")
            return  # don't mark processed — retry next cycle

        if tx.type == "skip":
            log.info(f"AI skipped [{uid}]: {tx.skip_reason}")
            await self._db.mark_processed(uid, sender)
            return

        # ── Multi-user routing ───────────────────────────────────────────────────
        resolved = await self._resolve_user(tx.account)
        if resolved:
            target_id, target_owner, target_notion, target_cache = resolved
        else:
            target_id = self._owner_id
            target_owner = self._owner_name
            target_notion = self._notion
            target_cache = cache

        if target_id is None:
            log.warning("[email] No target Telegram user for [%s] account=%r — retrying next cycle", uid, tx.account)
            return

        try:
            if tx.type == "expense":
                if tx.description.lower() == JAGO_DEBIT_DESCRIPTION:
                    recurring_list = target_cache.recurring_payments.get(int(round(tx.amount)), [])
                    recurring = next(
                        (item for item in recurring_list if item.get("account") == tx.account),
                        None,
                    )
                    if recurring is None:
                        blank_accounts = [item for item in recurring_list if not item.get("account")]
                        if len(blank_accounts) == 1:
                            recurring = blank_accounts[0]

                    if recurring:
                        # Check if user already has a pending entry for this
                        existing_pending = await self._db.get_pending_expense(target_id)
                        if existing_pending and existing_pending.amount == tx.amount:
                            log.info(f"[email] Recurring already pending [{uid}]: skipping duplicate")
                            # Terminal skip: persist before notifying so Telegram delays/failures
                            # cannot make the same email re-alert every poll.
                            await self._db.mark_processed(uid, sender)
                            await self._notify(
                                f"📧 *Email — pembayaran rutin sudah pending*\n"
                                f"📝 {recurring['name']}\n"
                                f"💰 Rp {tx.amount:,.0f}\n"
                                f"📅 {tx.date}\n\n"
                                f"Sudah ada transaksi ini yang menunggu konfirmasi.",
                                user_id=target_id,
                            )
                            return

                        entry = ExpenseEntry(
                            description=recurring["name"],
                            amount=tx.amount,
                            date=tx.date,
                            subcategory=recurring["subcategory"] or tx.subcategory,
                            account=recurring["account"] or tx.account,
                            confidence=1.0,
                            merchant=tx.merchant or recurring["name"],
                        )
                        # Store in both pending_expenses (for edit/confirm flow)
                        # and pending_recurring (to mark email processed on confirm)
                        await self._db.set_pending_expense(target_id, entry)
                        if self._pending_since is not None:
                            ts = time.time()
                            self._pending_since[target_id] = ts
                            await self._db.set_pending_since(target_id, ts)
                        await self._db.set_pending_recurring(
                            target_id, entry, recurring["page_url"], uid, sender,
                        )
                        sub_label = target_cache.closest_subcategory(entry.subcategory)
                        sub_text = sub_label[0] if sub_label else entry.subcategory
                        acc_label = target_cache.closest_account(entry.account)
                        acc_text = acc_label[0] if acc_label else entry.account
                        await self._notify_with_markup(
                            f"🔁 *Pembayaran rutin*\n"
                            f"📝 {entry.description}\n"
                            f"💰 Rp {entry.amount:,.0f}\n"
                            f"📅 {entry.date}\n"
                            f"🏷 {sub_text}\n"
                            f"🏦 {acc_text}\n\n"
                            f"Konfirmasi:",
                            make_confirm_keyboard(target_id),
                            user_id=target_id,
                        )
                        log.info(
                            f"[email] Recurring Rp {tx.amount:,.0f} ({recurring['name']}) "
                            f"— waiting for user confirmation via Simpan/Edit/Batal"
                        )
                        # Don't mark_processed yet — wait for user confirm/cancel
                        return
                    else:
                        log.info(
                            f"[email] Jago debit card Rp {tx.amount:,.0f} "
                            f"— asking user for merchant"
                        )
                        handled = await self._ask_debit_merchant(tx, user_id=target_id)
                        if not handled:
                            return  # notify failed — don't mark processed, retry next cycle

                else:
                    entry = ExpenseEntry(
                        description=tx.description,
                        amount=tx.amount,
                        date=tx.date,
                        subcategory=tx.subcategory,
                        account=tx.account,
                        confidence=0.95,
                        merchant=tx.merchant,
                    )
                    # Duplicate check — only skip if the pending manual entry is clearly the same
                    # transaction. Amount+date alone is not enough: unrelated manual/query mistakes can
                    # collide and must not cause a real bank email to be skipped.
                    pending = await self._db.get_pending_expense(target_id)
                    if pending and pending.amount == tx.amount and pending.date == tx.date:
                        pending_desc = (pending.description or "").strip().lower()
                        tx_desc = (tx.description or "").strip().lower()
                        pending_merchant = (pending.merchant or "").strip().lower()
                        tx_merchant = (tx.merchant or "").strip().lower()
                        same_pending_tx = bool(
                            (pending_merchant and tx_merchant and pending_merchant == tx_merchant)
                            or (pending_desc and tx_desc and (pending_desc in tx_desc or tx_desc in pending_desc))
                        )
                        if same_pending_tx:
                            log.info(
                                f"[email] Pending expense clearly matches [{uid}]: "
                                f"{tx.description} Rp {tx.amount:,.0f} — skipping email duplicate"
                            )
                            # Terminal skip: persist before notifying so Telegram delays/failures
                            # cannot make the same email re-alert every poll.
                            await self._db.mark_processed(uid, sender)
                            await self._notify(
                                f"📧 *Email — sudah pending*\n"
                                f"📝 {tx.description}\n"
                                f"💰 Rp {tx.amount:,.0f}\n"
                                f"📅 {tx.date}\n\n"
                                f"Transaksi ini sudah tercatat manual dan menunggu konfirmasi.",
                                user_id=target_id,
                            )
                            return
                        log.warning(
                            f"[email] Pending amount/date collision ignored [{uid}]: "
                            f"pending={pending.description!r}, email={tx.description!r}, "
                            f"amount=Rp {tx.amount:,.0f}, date={tx.date}"
                        )
                    try:
                        matches = await target_notion.fetch_duplicates(target_owner, tx.amount, tx.date)
                        if matches:
                            is_dup = await self._agent.check_duplicate(
                                matches, tx.description, tx.amount, tx.date,
                                new_merchant=tx.merchant,
                            )
                            if is_dup:
                                log.info(f"[email] Duplicate skipped [{uid}]: {tx.description} Rp {tx.amount:,.0f}")
                                # Persist the terminal decision before notifying. Telegram/network
                                # failures should not make a confirmed duplicate re-alert every poll.
                                await self._db.mark_processed(uid, sender)
                                subcat_match = target_cache.closest_subcategory(tx.subcategory)
                                sub_text = subcat_match[0] if subcat_match else "📦 Miscellaneous"
                                merchant_line = f"🏪 {tx.merchant}\n" if tx.merchant else ""
                                await self._notify(
                                    f"📧 *Email — duplikat, dilewati*\n"
                                    f"📝 {tx.description}\n"
                                    f"💰 Rp {tx.amount:,.0f}\n"
                                    f"📅 {tx.date}\n"
                                    f"{merchant_line}"
                                    f"🏷 {sub_text}\n"
                                    f"🏦 {tx.account}\n\n"
                                    f"Transaksi ini sudah tercatat sebelumnya.",
                                    user_id=target_id,
                                )
                                return
                    except Exception as e:
                        log.warning(f"[email] Duplicate check failed for [{uid}]: {e}")

                    # Merchant-based prediction: check for same merchant + similar amount
                    if tx.merchant:
                        try:
                            similar = await target_notion.find_similar_by_merchant(
                                target_owner, tx.merchant, tx.amount, tx.date, target_cache
                            )
                            if similar:
                                prev = similar[0]
                                log.info(
                                    f"[email] Merchant match [{uid}]: {tx.merchant} "
                                    f"Rp {tx.amount:,.0f} — previously Rp {prev['amount']:,.0f} on {prev['date']}"
                                )
                        except Exception as e:
                            log.warning(f"[email] Merchant similarity check failed [{uid}]: {e}")

                    url = await target_notion.log_expense(entry, target_owner, target_cache)
                    # The Notion write is already durable. Mark the email processed before
                    # best-effort side effects (budget task, local edit/undo state, Telegram)
                    # so a later side-effect failure cannot create a duplicate Notion entry
                    # on the next IMAP poll.
                    await self._db.mark_processed(uid, sender)
                    alert_task = asyncio.create_task(self._check_budget_alert(entry, notion=target_notion, cache=target_cache))
                    alert_task.add_done_callback(lambda t: t.exception() and log.warning(f"Budget alert task failed: {t.exception()}"))
                    if self._on_save_fn:
                        await self._on_save_fn(
                            target_id, url, tx.description, tx.amount, tx.date, tx.subcategory, tx.merchant,
                        )
                    subcat_match = target_cache.closest_subcategory(tx.subcategory)
                    acc_match = target_cache.closest_account(tx.account)
                    log.info(
                        f"[email→Notion] {tx.description} "
                        f"Rp {tx.amount:,.0f} [{tx.subcategory}]"
                    )
                    sub_text = subcat_match[0] if subcat_match else "📦 Miscellaneous"
                    acc_text = acc_match[0] if acc_match else f"❓ {tx.account}"
                    merchant_line = f"🏪 {tx.merchant}\n" if tx.merchant else ""
                    await self._notify_with_markup(
                        f"📧 *Otomatis tercatat dari email*\n"
                        f"📝 {tx.description}\n"
                        f"💰 Rp {tx.amount:,.0f}\n"
                        f"📅 {tx.date}\n"
                        f"{merchant_line}"
                        f"🏷 {sub_text}\n"
                        f"🏦 {acc_text}\n"
                        f"[Lihat di Notion]({url})",
                        make_email_edit_keyboard(target_id),
                        user_id=target_id,
                    )

            elif tx.type == "self_transfer":
                source = tx.source_account or tx.account or "rekening sumber"
                dest = tx.destination_account or tx.recipient_bank or "rekening tujuan"
                log.info(f"[email] Self-transfer Rp {tx.amount:,.0f} {source} → {dest}")

                # Record the outgoing transfer from the source account.
                transfer_out = ExpenseEntry(
                    description=format_self_transfer_label(source, dest, "out"),
                    amount=tx.amount,
                    date=tx.date,
                    subcategory=tx.subcategory,
                    account=source,
                    confidence=0.95,
                    merchant="",
                )
                out_url = None
                out_duplicate = False
                try:
                    out_matches = await target_notion.fetch_duplicates(target_owner, tx.amount, tx.date)
                    if out_matches:
                        is_out_dup = await self._agent.check_duplicate(
                            out_matches, transfer_out.description, tx.amount, tx.date,
                            new_merchant="",
                        )
                        if is_out_dup:
                            out_duplicate = True
                            log.info(f"[email] Transfer-out duplicate skipped [{uid}]: Rp {tx.amount:,.0f}")
                        else:
                            out_url = await target_notion.log_expense(transfer_out, target_owner, target_cache)
                    else:
                        out_url = await target_notion.log_expense(transfer_out, target_owner, target_cache)
                except Exception as e:
                    log.warning(f"[email] Transfer-out logging failed: {e}")
                    raise
                if out_url and self._on_save_fn:
                    await self._on_save_fn(
                        target_id, out_url, transfer_out.description, transfer_out.amount, transfer_out.date, transfer_out.subcategory, transfer_out.merchant,
                    )
                    log.info(f"[email→Notion] Transfer-out Rp {tx.amount:,.0f} logged")

                # Record the incoming transfer to the destination account.
                income_subcategory = tx.income_subcategory or tx.subcategory or "Transfer"
                transfer_in = IncomeEntry(
                    description=format_self_transfer_label(source, dest, "in"),
                    amount=tx.amount,
                    date=tx.date,
                    subcategory=income_subcategory,
                    account=dest,
                    confidence=0.95,
                )
                in_url = None
                in_duplicate = False
                try:
                    income_matches = await target_notion.fetch_duplicates(target_owner, tx.amount, tx.date, db_key="income_ds")
                    if income_matches:
                        is_in_dup = await self._agent.check_duplicate(
                            income_matches, transfer_in.description, tx.amount, tx.date,
                            new_merchant="",
                        )
                        if is_in_dup:
                            in_duplicate = True
                            log.info(f"[email] Transfer-in duplicate skipped [{uid}]: Rp {tx.amount:,.0f}")
                        else:
                            in_url = await target_notion.log_income(transfer_in, target_owner, target_cache)
                    else:
                        in_url = await target_notion.log_income(transfer_in, target_owner, target_cache)
                except Exception as e:
                    log.warning(f"[email] Transfer-in logging failed: {e}")
                    raise
                if in_url and self._on_save_fn:
                    await self._on_save_fn(
                        target_id, in_url, transfer_in.description, transfer_in.amount, transfer_in.date, transfer_in.subcategory, "",
                    )
                    log.info(f"[email→Notion] Transfer-in Rp {tx.amount:,.0f} logged")

                # Record the admin fee separately, if any.
                fee_url = None
                fee_duplicate = False
                if tx.admin_fee > 0:
                    fee_entry = ExpenseEntry(
                        description=format_self_transfer_label(source, dest, "fee"),
                        amount=tx.admin_fee,
                        date=tx.date,
                        subcategory=tx.subcategory,
                        account=source,
                        confidence=0.9,
                        merchant="",
                    )
                    try:
                        fee_matches = await target_notion.fetch_duplicates(target_owner, tx.admin_fee, tx.date)
                        if fee_matches:
                            is_fee_dup = await self._agent.check_duplicate(
                                fee_matches, fee_entry.description, tx.admin_fee, tx.date,
                                new_merchant="",
                            )
                            if is_fee_dup:
                                fee_duplicate = True
                                log.info(f"[email] Admin fee duplicate skipped [{uid}]: Rp {tx.admin_fee:,.0f}")
                            else:
                                fee_url = await target_notion.log_expense(fee_entry, target_owner, target_cache)
                        else:
                            fee_url = await target_notion.log_expense(fee_entry, target_owner, target_cache)
                    except Exception as e:
                        log.warning(f"[email] Admin fee logging failed: {e}")
                        raise
                    if fee_url and self._on_save_fn:
                        await self._on_save_fn(
                            target_id, fee_url, fee_entry.description, fee_entry.amount, fee_entry.date, fee_entry.subcategory,
                        )
                        log.info(f"[email→Notion] Admin fee Rp {tx.admin_fee:,.0f} logged")

                # All self-transfer Notion writes/duplicate decisions above are durable.
                # Mark the email processed before Telegram summary side effects so a
                # notification delay/failure cannot make the next poll write duplicates.
                await self._db.mark_processed(uid, sender)

                if not any([out_url, in_url, fee_url]):
                    duplicate_bits = []
                    if out_duplicate:
                        duplicate_bits.append("keluar")
                    if in_duplicate:
                        duplicate_bits.append("masuk")
                    if fee_duplicate:
                        duplicate_bits.append("biaya admin")
                    if duplicate_bits:
                        fee_line = f"\n💸 Biaya admin: Rp {tx.admin_fee:,.0f}" if tx.admin_fee > 0 else ""
                        await self._notify(
                            f"📧 *Transfer antar rekening duplikat, dilewati*\n"
                            f"💰 Rp {tx.amount:,.0f}\n"
                            f"📅 {tx.date}\n"
                            f"🏦 {source} → {dest}{fee_line}\n"
                            f"Bagian yang sudah ada: {', '.join(duplicate_bits)}.",
                            user_id=target_id,
                        )
                    else:
                        log.warning(f"[email] Self-transfer [{uid}] produced no Notion writes and no duplicates")
                else:
                    summary_lines = [
                        f"📧 *Transfer antar rekening* Rp {tx.amount:,.0f}",
                        f"🏦 {source} → {dest}",
                    ]
                    if tx.admin_fee > 0:
                        summary_lines.append(f"💸 Biaya admin: Rp {tx.admin_fee:,.0f}")
                    if out_url:
                        summary_lines.append(f"➡️ [Keluar]({out_url})")
                    if in_url:
                        summary_lines.append(f"⬅️ [Masuk]({in_url})")
                    if fee_url:
                        summary_lines.append(f"🧾 [Biaya admin]({fee_url})")
                    await self._notify_with_markup(
                        "\n".join(summary_lines),
                        make_email_edit_keyboard(target_id),
                        user_id=target_id,
                    )

            else:
                log.warning(
                    f"Unknown EmailTransaction.type '{tx.type}' for [{uid}] — skipping"
                )
                return  # don't mark processed; let a future model fix surface it

        except Exception as e:
            log.error(f"Failed to log email [{uid}] to Notion: {e}")
            self._notion_fail_streak += 1
            if self._notion_fail_streak == 3 or self._notion_fail_streak % 5 == 0:
                await self._alert(
                    f"⚠️ *Email watcher: gagal menyimpan ke Notion*\n"
                    f"Gagal {self._notion_fail_streak}x berturut-turut.\n"
                    f"`{type(e).__name__}: {str(e)[:120]}`"
                )
            return  # don't mark processed — retry next cycle

        self._notion_fail_streak = 0

    # ── Main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        interval = getattr(self._config, "email_poll_interval", 300)
        log.info(f"Email watcher started — polling every {interval}s")

        while True:
            try:
                log.info("Email watcher: cycle start")
                pruned = await self._db.prune_processed()
                if pruned:
                    log.info(f"Pruned {pruned} processed email(s) older than 90 days")

                log.info("Email watcher: fetching processed UIDs")
                processed = await self._db.get_all_processed_uids()
                log.info(f"Email watcher: {len(processed)} processed UIDs")
                self._last_imap_error = None
                try:
                    log.info("Email watcher: starting IMAP fetch...")
                    emails = await asyncio.wait_for(
                        asyncio.to_thread(self._imap_fetch, processed),
                        timeout=60,
                    )
                    log.info(f"Email watcher: IMAP fetch returned {len(emails)} email(s)")
                except asyncio.TimeoutError:
                    log.error("IMAP fetch timed out after 60s")
                    self._last_imap_error = "timeout"
                    emails = []
                except Exception as e:
                    log.error(f"Email watcher: IMAP fetch exception: {type(e).__name__}: {e}")
                    self._last_imap_error = str(e)
                    emails = []

                if self._last_imap_error:
                    self._imap_fail_streak += 1
                    is_auth_error = self._is_imap_auth_error(self._last_imap_error)
                    should_alert = (
                        self._imap_fail_streak == 1
                        or (not is_auth_error and self._imap_fail_streak % 5 == 0)
                        or (is_auth_error and self._imap_fail_streak % 360 == 0)
                    )
                    if should_alert:
                        await self._alert(
                            f"⚠️ *Email watcher: IMAP error*\n"
                            f"Fail #{self._imap_fail_streak}: `{self._last_imap_error}`\n"
                            "Cek Gmail App Password di `.env` jika berlanjut."
                        )
                else:
                    self._imap_fail_streak = 0

                if emails:
                    log.info(f"Found {len(emails)} new bank email(s) to process")
                for uid, sender, subject, body in emails:
                    await self._process(uid, sender, subject, body)
                    self._total_processed += 1
                self._last_poll_time = time.time()
            except Exception as e:
                log.error(f"Email watcher cycle error: {e}")

            await asyncio.sleep(interval)

    def status_info(self) -> dict:
        return {
            "running": True,
            "last_poll": self._last_poll_time,
            "uptime_seconds": time.time() - self._start_time,
            "total_processed": self._total_processed,
            "notion_fail_streak": self._notion_fail_streak,
            "imap_error": self._last_imap_error,
        }
