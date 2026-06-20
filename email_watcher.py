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
from models import ExpenseEntry, EmailTransaction, NotionCache

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
        on_save_fn        : optional async fn(user_id, page_id, description, amount, date, subcategory)
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
        self._notion_fail_streak: int = 0
        self._last_poll_time: float | None = None
        self._total_processed: int = 0
        self._start_time: float = time.time()

    # ── IMAP (synchronous — called via asyncio.to_thread) ──────────────────────

    def _ensure_imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(self._config.gmail_address, self._config.gmail_app_password)
            imap.select("INBOX")
            self._imap = imap
        else:
            try:
                self._imap.noop()
            except Exception:
                self._close_imap()
                imap = imaplib.IMAP4_SSL(IMAP_HOST)
                imap.login(self._config.gmail_address, self._config.gmail_app_password)
                imap.select("INBOX")
                self._imap = imap
        return self._imap

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
        """
        results = []
        try:
            imap = self._ensure_imap()

            # Only fetch emails from the last LOOKBACK_DAYS days
            since = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")

            for sender in BANK_SENDERS:
                typ, data = imap.uid("search", None, f'FROM "{sender}" SINCE {since}')
                if typ != "OK" or not data[0]:
                    continue

                raw = data[0]
                if isinstance(raw, int):
                    raw = str(raw).encode()
                elif isinstance(raw, str):
                    raw = raw.encode()
                uids = raw.split()
                for uid_bytes in uids[-100:]:
                    uid = uid_bytes.decode()
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

        except imaplib.IMAP4.error as e:
            log.error(f"IMAP auth/connection error: {e}")
            self._last_imap_error = str(e)
            self._close_imap()
        except Exception as e:
            log.error(f"IMAP fetch error: {e}")
            self._close_imap()
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
            for b in budgets:
                if entry.subcategory not in b["subcategories"]:
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
            await self._db.set_pending_expense(target, entry)
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
                    await self._db.clear_pending_expense(target)
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

        try:
            if tx.type == "expense":
                if tx.description.lower() == JAGO_DEBIT_DESCRIPTION:
                    recurring = target_cache.recurring_payments.get(int(round(tx.amount)))

                    if recurring:
                        # Check if user already has a pending entry for this
                        existing_pending = await self._db.get_pending_expense(target_id)
                        if existing_pending and existing_pending.amount == tx.amount:
                            log.info(f"[email] Recurring already pending [{uid}]: skipping duplicate")
                            await self._notify(
                                f"📧 *Email — pembayaran rutin sudah pending*\n"
                                f"📝 {recurring['name']}\n"
                                f"💰 Rp {tx.amount:,.0f}\n"
                                f"📅 {tx.date}\n\n"
                                f"Sudah ada transaksi ini yang menunggu konfirmasi.",
                                user_id=target_id,
                            )
                            await self._db.mark_processed(uid, sender)
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
                            self._pending_since[target_id] = time.time()
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
                    # Duplicate check — skip if user has a pending expense with same amount+date
                    pending = await self._db.get_pending_expense(target_id)
                    if pending and pending.amount == tx.amount and pending.date == tx.date:
                        log.info(f"[email] Pending expense matches [{uid}]: {tx.description} Rp {tx.amount:,.0f} — skipping")
                        await self._notify(
                            f"📧 *Email — sudah pending*\n"
                            f"📝 {tx.description}\n"
                            f"💰 Rp {tx.amount:,.0f}\n"
                            f"📅 {tx.date}\n\n"
                            f"Transaksi ini sudah tercatat manual dan menunggu konfirmasi.",
                            user_id=target_id,
                        )
                        await self._db.mark_processed(uid, sender)
                        return
                    try:
                        matches = await target_notion.fetch_duplicates(target_owner, tx.amount, tx.date)
                        if matches:
                            is_dup = await self._agent.check_duplicate(
                                matches, tx.description, tx.amount, tx.date,
                                new_merchant=tx.merchant,
                            )
                            if is_dup:
                                log.info(f"[email] Duplicate skipped [{uid}]: {tx.description} Rp {tx.amount:,.0f}")
                                subcat_match = target_cache.closest_subcategory(tx.subcategory)
                                sub_text = subcat_match[0] if subcat_match else f"❓ {tx.subcategory}"
                                await self._notify(
                                    f"📧 *Email — duplikat, dilewati*\n"
                                    f"📝 {tx.description}\n"
                                    f"💰 Rp {tx.amount:,.0f}\n"
                                    f"📅 {tx.date}\n"
                                    f"🏷 {sub_text}\n"
                                    f"🏦 {tx.account}\n\n"
                                    f"Transaksi ini sudah tercatat sebelumnya.",
                                    user_id=target_id,
                                )
                                await self._db.mark_processed(uid, sender)
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
                    asyncio.create_task(self._check_budget_alert(entry, notion=target_notion, cache=target_cache))
                    if self._on_save_fn:
                        await self._on_save_fn(
                            target_id, url, tx.description, tx.amount, tx.date, tx.subcategory,
                        )
                    subcat_match = target_cache.closest_subcategory(tx.subcategory)
                    acc_match = target_cache.closest_account(tx.account)
                    log.info(
                        f"[email→Notion] {tx.description} "
                        f"Rp {tx.amount:,.0f} [{tx.subcategory}]"
                    )
                    sub_text = subcat_match[0] if subcat_match else f"❓ {tx.subcategory}"
                    acc_text = acc_match[0] if acc_match else f"❓ {tx.account}"
                    await self._notify_with_markup(
                        f"📧 *Otomatis tercatat dari email*\n"
                        f"📝 {tx.description}\n"
                        f"💰 Rp {tx.amount:,.0f}\n"
                        f"📅 {tx.date}\n"
                        f"🏷 {sub_text}\n"
                        f"🏦 {acc_text}\n"
                        f"[Lihat di Notion]({url})",
                        make_email_edit_keyboard(target_id),
                        user_id=target_id,
                    )

            elif tx.type == "self_transfer":
                dest = tx.recipient_bank or "rekening sendiri"
                log.info(f"[email] Self-transfer Rp {tx.amount:,.0f} → {dest}")

                skip_admin_fee = False
                if tx.admin_fee > 0:
                    fee_entry = ExpenseEntry(
                        description=f"Admin fee – transfer ke {dest}",
                        amount=tx.admin_fee,
                        date=tx.date,
                        subcategory=tx.subcategory,
                        account=tx.account,
                        confidence=0.9,
                        merchant="",
                    )
                    # Duplicate check for admin fee
                    try:
                        fee_matches = await target_notion.fetch_duplicates(target_owner, tx.admin_fee, tx.date)
                        if fee_matches:
                            is_fee_dup = await self._agent.check_duplicate(
                                fee_matches, fee_entry.description, tx.admin_fee, tx.date,
                                new_merchant="",
                            )
                            if is_fee_dup:
                                log.info(f"[email] Admin fee duplicate skipped [{uid}]: Rp {tx.admin_fee:,.0f}")
                                await self._notify(
                                    f"📧 *Admin fee duplikat, dilewati*\n"
                                    f"💰 Rp {tx.admin_fee:,.0f}\n"
                                    f"📅 {tx.date}\n\n"
                                    f"Biaya admin ini sudah tercatat sebelumnya.",
                                    user_id=target_id,
                                )
                                skip_admin_fee = True
                    except Exception as e:
                        log.warning(f"[email] Admin fee duplicate check failed: {e}")

                    url = None
                    if not skip_admin_fee:
                        url = await target_notion.log_expense(fee_entry, target_owner, target_cache)
                        if self._on_save_fn:
                            await self._on_save_fn(
                                target_id, url, fee_entry.description, fee_entry.amount, fee_entry.date, fee_entry.subcategory,
                            )
                        log.info(f"[email→Notion] Admin fee Rp {tx.admin_fee:,.0f} logged")

                    notify_text = (
                        f"📧 *Transfer sendiri* Rp {tx.amount:,.0f} → {dest}\n"
                        f"Biaya admin {'tercatat' if url else 'duplikat, dilewati'}: 💰 Rp {tx.admin_fee:,.0f}\n"
                    )
                    if url:
                        notify_text += f"[Lihat di Notion]({url})"
                    await self._notify_with_markup(
                        notify_text, make_email_edit_keyboard(target_id), user_id=target_id,
                    )
                else:
                    await self._notify(
                        f"📧 *Transfer sendiri* Rp {tx.amount:,.0f} → {dest}\n"
                        f"Tidak ada biaya admin — tidak dicatat.",
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
        await self._db.mark_processed(uid, sender)

    # ── Main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        interval = getattr(self._config, "email_poll_interval", 300)
        log.info(f"Email watcher started — polling every {interval}s")

        while True:
            try:
                pruned = await self._db.prune_processed()
                if pruned:
                    log.info(f"Pruned {pruned} processed email(s) older than 90 days")

                processed = await self._db.get_all_processed_uids()
                self._last_imap_error = None
                try:
                    emails = await asyncio.wait_for(
                        asyncio.to_thread(self._imap_fetch, processed),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    log.error("IMAP fetch timed out after 60s")
                    self._last_imap_error = "timeout"
                    emails = []

                if self._last_imap_error:
                    await self._alert(
                        f"⚠️ *Email watcher: login IMAP gagal*\n"
                        f"`{self._last_imap_error}`\n"
                        "Cek Gmail App Password di file `.env`."
                    )

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
