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
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable

from models import ExpenseEntry, EmailTransaction, ConversationState

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

PROCESSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_emails.json")
IMAP_HOST = "imap.gmail.com"

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
    notion            : NotionClient instance
    agent             : Agent instance
    cache_getter      : zero-arg callable returning current NotionCache
    bot               : aiogram Bot instance (for Telegram notifications)
    owner_telegram_id : Telegram user_id to notify (email account owner)
    owner_name        : owner name string (e.g. "Afif") for Notion expense prefix
    conversations     : shared ConversationState dict from main.py (for debit follow-up)
    alert_fn          : async fn(text) that broadcasts to all users
    """

    def __init__(
        self,
        config,
        notion,
        agent,
        cache_getter: Callable,
        bot=None,
        owner_telegram_id: int | None = None,
        owner_name: str = "Afif",
        conversations: dict | None = None,
        alert_fn=None,
    ):
        self._config = config
        self._notion = notion
        self._agent = agent
        self._cache_getter = cache_getter
        self._bot = bot
        self._owner_id = owner_telegram_id
        self._owner_name = owner_name
        self._conversations = conversations
        self._alert_fn = alert_fn  # async fn(text) → broadcasts to all users
        self._processed: dict[str, str] = self._load_processed()  # uid → ISO timestamp
        self._last_imap_error: str | None = None
        self._notion_fail_streak: int = 0  # consecutive Notion write failures

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_processed(self) -> dict[str, str]:
        """
        Load processed email UIDs with timestamps.
        Prune entries older than 90 days to prevent unbounded growth.
        """
        if os.path.exists(PROCESSED_FILE):
            try:
                with open(PROCESSED_FILE) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    # Legacy format: list of uid strings → migrate
                    return {uid: "" for uid in data}
                return data  # {uid: ISO_timestamp}
            except Exception:
                pass
        return {}

    def _save_processed(self) -> None:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(self._processed, f)

    def _prune_processed(self) -> None:
        """Remove entries older than 90 days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        before = len(self._processed)
        self._processed = {
            uid: ts
            for uid, ts in self._processed.items()
            if ts
            and datetime.fromisoformat(ts) > cutoff
        }
        pruned = before - len(self._processed)
        if pruned:
            log.info(f"Pruned {pruned} processed email(s) older than 90 days")
            self._save_processed()

    # ── IMAP (synchronous — called via asyncio.to_thread) ──────────────────────

    def _imap_fetch(self) -> list[tuple[str, str, str, str]]:
        """
        Connect to Gmail IMAP and fetch unprocessed emails from bank senders.
        Returns list of (uid, sender_email, subject, body_text).
        """
        results = []
        imap = None
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(self._config.gmail_address, self._config.gmail_app_password)
            imap.select("INBOX")

            for sender in BANK_SENDERS:
                typ, data = imap.uid("search", None, f'FROM "{sender}"')
                if typ != "OK" or not data[0]:
                    continue

                uids = data[0].split()
                # Only process the most recent 100 to bound memory + time
                for uid_bytes in uids[-100:]:
                    uid = uid_bytes.decode()
                    if uid in self._processed:
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
            # Store for async alerting — can't await inside sync method
            self._last_imap_error = str(e)
        except Exception as e:
            log.error(f"IMAP fetch error: {e}")
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass
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
        """Send a message to the owner (Afif)."""
        if self._bot and self._owner_id:
            try:
                await self._bot.send_message(
                    self._owner_id, text, parse_mode="Markdown"
                )
            except Exception as e:
                log.error(f"Telegram notify failed: {e}")

    async def _alert(self, text: str) -> None:
        """Broadcast a critical error to all users via alert_fn, or fall back to _notify."""
        if self._alert_fn:
            try:
                await self._alert_fn(text)
            except Exception as e:
                log.error(f"alert_fn failed: {e}")
        else:
            await self._notify(text)

    async def _ask_debit_merchant(self, tx: EmailTransaction) -> None:
        """
        For a Jago debit card tx with no merchant info:
        - enqueue the tx on the user's ConversationState
        - if no tx is currently pending, promote this one to pending and notify
        - if a tx is already pending, just queue it (user will be prompted after
          they respond to the current one)
        """
        if self._conversations is not None and self._owner_id is not None:
            if self._owner_id not in self._conversations:
                self._conversations[self._owner_id] = ConversationState()
            state = self._conversations[self._owner_id]

            if state.pending_email_expense is None:
                # No pending question — prompt user immediately
                state.pending_email_expense = tx
                await self._notify(
                    f"💳 *Jago debit card* — Rp {tx.amount:,.0f}\n"
                    f"📅 {tx.date}  🏦 {tx.account}\n\n"
                    f"Beli apa? Balas dengan nama merchant/deskripsi."
                )
            else:
                # Already waiting for a reply — queue this one
                state.pending_debit_queue.append(tx)
                log.info(
                    f"[email] Queued Jago debit card Rp {tx.amount:,.0f} "
                    f"(queue depth: {len(state.pending_debit_queue)})"
                )

    # ── Email processing ────────────────────────────────────────────────────────

    async def _process(self, uid: str, sender: str, subject: str, body: str) -> None:
        # Pre-filter: skip failed transactions by subject
        subject_lower = subject.lower()
        if any(kw in subject_lower for kw in SKIP_SUBJECT_KEYWORDS):
            log.info(f"Skipping failed-transaction email [{uid}]: {subject}")
            self._processed[uid] = datetime.now(timezone.utc).isoformat()
            self._save_processed()
            return

        cache = self._cache_getter()
        today = date.today().isoformat()

        # Parse with AI
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
            self._processed[uid] = datetime.now(timezone.utc).isoformat()
            self._save_processed()
            return

        owner = self._owner_name

        try:
            if tx.type == "expense":
                # ── Jago debit card: no merchant info ─────────────────────────
                if tx.description.lower() == JAGO_DEBIT_DESCRIPTION:
                    recurring = cache.recurring_payments.get(int(round(tx.amount)))

                    if recurring:
                        # Known recurring expense — auto-log and link back
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
                        # Unknown debit — ask user for merchant name
                        log.info(
                            f"[email] Jago debit card Rp {tx.amount:,.0f} "
                            f"— asking user for merchant"
                        )
                        await self._ask_debit_merchant(tx)

                else:
                    # Normal expense/transfer with known merchant
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

        except Exception as e:
            log.error(f"Failed to log email [{uid}] to Notion: {e}")
            self._notion_fail_streak += 1
            if self._notion_fail_streak >= 3:
                await self._alert(
                    f"⚠️ *Email watcher: Notion write failing*\n"
                    f"Failed {self._notion_fail_streak}x in a row.\n"
                    f"`{type(e).__name__}: {str(e)[:120]}`"
                )
            return  # don't mark processed — retry next cycle

        self._notion_fail_streak = 0  # reset on success

        self._processed.add(uid)
        self._save_processed()

    # ── Main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        interval = getattr(self._config, "email_poll_interval", 300)
        log.info(f"Email watcher started — polling every {interval}s")

        while True:
            try:
                self._prune_processed()
                self._last_imap_error = None
                emails = await asyncio.to_thread(self._imap_fetch)

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
