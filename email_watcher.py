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
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Callable

from db import Database
from models import ExpenseEntry, EmailTransaction

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
LOOKBACK_DAYS = 1    # only scan today's and yesterday's emails

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

    Parameters
    ----------
    config            : app Config
    db                : Database instance for persistence
    notion            : NotionClient instance
    agent             : Agent instance
    cache_getter      : zero-arg callable returning current NotionCache
    bot               : aiogram Bot instance (for Telegram notifications)
    owner_telegram_id : Telegram user_id to notify (email account owner)
    owner_name        : owner name string (e.g. "Afif") for Notion expense prefix
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
        owner_telegram_id: int | None = None,
        owner_name: str = "Afif",
        alert_fn=None,
    ):
        self._config = config
        self._db = db
        self._notion = notion
        self._agent = agent
        self._cache_getter = cache_getter
        self._bot = bot
        self._owner_id = owner_telegram_id
        self._owner_name = owner_name
        self._alert_fn = alert_fn
        self._imap: imaplib.IMAP4_SSL | None = None
        self._last_imap_error: str | None = None
        self._notion_fail_streak: int = 0

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

                uids = data[0].split()
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

    # ── Notification helpers ────────────────────────────────────────────────────

    async def _notify(self, text: str) -> None:
        if self._bot and self._owner_id:
            try:
                await self._bot.send_message(
                    self._owner_id, text, parse_mode="Markdown"
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

    async def _ask_debit_merchant(self, tx: EmailTransaction) -> bool:
        """
        For a Jago debit card tx with no merchant info:
        - if no tx is currently pending, promote this one and notify
        - if a tx is already pending, queue it

        Returns True if the transaction was successfully handled (notified or queued).
        Returns False if Telegram notification failed for a newly-promoted tx,
        so the caller can avoid marking the email as processed.
        """
        if self._owner_id is None:
            return False

        current = await self._db.get_pending_email_expense(self._owner_id)
        if current is None:
            await self._db.set_pending_email_expense(self._owner_id, tx)
            if self._bot:
                try:
                    await self._bot.send_message(
                        self._owner_id,
                        f"💳 *Jago debit card* — Rp {tx.amount:,.0f}\n"
                        f"📅 {tx.date}  🏦 {tx.account}\n\n"
                        f"Beli apa? Balas dengan nama merchant/deskripsi.\n"
                        f"_(Ketik *batal* untuk lewati)_",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    log.error(f"Telegram notify failed for Jago debit follow-up: {e}")
                    await self._db.clear_pending_email_expense(self._owner_id)
                    return False
            return True
        else:
            await self._db.push_debit(self._owner_id, tx)
            depth = await self._db.debit_queue_depth(self._owner_id)
            log.info(
                f"[email] Queued Jago debit card Rp {tx.amount:,.0f} "
                f"(queue depth: {depth})"
            )
            return True

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

        owner = self._owner_name

        try:
            if tx.type == "expense":
                if tx.description.lower() == JAGO_DEBIT_DESCRIPTION:
                    recurring = cache.recurring_payments.get(int(round(tx.amount)))

                    if recurring:
                        entry = ExpenseEntry(
                            description=recurring["name"],
                            amount=tx.amount,
                            date=tx.date,
                            subcategory=recurring["subcategory"] or tx.subcategory,
                            account=recurring["account"] or tx.account,
                            confidence=1.0,
                        )
                        url = await self._notion.log_expense(
                            entry, owner, cache,
                            recurring_page_url=recurring["page_url"],
                        )
                        log.info(
                            f"[email→Notion] Recurring: {entry.description} "
                            f"Rp {tx.amount:,.0f}"
                        )
                        await self._notify(
                            f"🔁 *Auto-logged recurring*\n"
                            f"📝 {entry.description}\n"
                            f"💰 Rp {tx.amount:,.0f}\n"
                            f"📅 {tx.date}\n"
                            f"[View in Notion]({url})"
                        )
                    else:
                        log.info(
                            f"[email] Jago debit card Rp {tx.amount:,.0f} "
                            f"— asking user for merchant"
                        )
                        handled = await self._ask_debit_merchant(tx)
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
                    )
                    url = await self._notion.log_expense(entry, owner, cache)
                    log.info(
                        f"[email→Notion] {tx.description} "
                        f"Rp {tx.amount:,.0f} [{tx.subcategory}]"
                    )
                    await self._notify(
                        f"📧 *Auto-logged from email*\n"
                        f"📝 {tx.description}\n"
                        f"💰 Rp {tx.amount:,.0f}\n"
                        f"📅 {tx.date}\n"
                        f"🏷 {tx.subcategory}\n"
                        f"🏦 {tx.account}\n"
                        f"[View in Notion]({url})"
                    )

            elif tx.type == "self_transfer":
                dest = tx.recipient_bank or "rekening sendiri"
                log.info(f"[email] Self-transfer Rp {tx.amount:,.0f} → {dest}")

                if tx.admin_fee > 0:
                    fee_entry = ExpenseEntry(
                        description=f"Admin fee – transfer ke {dest}",
                        amount=tx.admin_fee,
                        date=tx.date,
                        subcategory=tx.subcategory,
                        account=tx.account,
                        confidence=0.9,
                    )
                    url = await self._notion.log_expense(fee_entry, owner, cache)
                    log.info(f"[email→Notion] Admin fee Rp {tx.admin_fee:,.0f} logged")
                    await self._notify(
                        f"📧 *Self-transfer* Rp {tx.amount:,.0f} → {dest}\n"
                        f"Admin fee logged: 💰 Rp {tx.admin_fee:,.0f}\n"
                        f"[View in Notion]({url})"
                    )
                else:
                    await self._notify(
                        f"📧 *Self-transfer* Rp {tx.amount:,.0f} → {dest}\n"
                        f"No admin fee — not logged."
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
                    f"⚠️ *Email watcher: Notion write failing*\n"
                    f"Failed {self._notion_fail_streak}x in a row.\n"
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
                emails = await asyncio.to_thread(self._imap_fetch, processed)

                if self._last_imap_error:
                    await self._alert(
                        f"⚠️ *Email watcher: IMAP auth failed*\n"
                        f"`{self._last_imap_error}`\n"
                        "Check your Gmail App Password in `.env`."
                    )

                if emails:
                    log.info(f"Found {len(emails)} new bank email(s) to process")
                for uid, sender, subject, body in emails:
                    await self._process(uid, sender, subject, body)
            except Exception as e:
                log.error(f"Email watcher cycle error: {e}")

            await asyncio.sleep(interval)
