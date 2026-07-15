import asyncio
import csv
import io
import math
import logging
import os
import socket
from logging.handlers import RotatingFileHandler
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)

from config import load_config
from db import Database
from keyboards import (
    make_confirm_keyboard,
    make_income_confirm_keyboard,
    make_category_keyboard,
    make_subcategory_keyboard,
    make_edit_field_keyboard,
    make_undo_keyboard,
    make_email_edit_keyboard,
    make_income_edit_field_keyboard,
)
from models import NotionCache, ExpenseEntry, IncomeEntry
from notion import NotionClient, _url_to_id
from agent import Agent
from email_watcher import EmailWatcher


logging.basicConfig(level=logging.INFO)
logging.getLogger("__main__").setLevel(logging.DEBUG)

_log_path = Path(os.getenv("BOT_LOG_PATH", "data/bot.log"))
_log_path.parent.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(_log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_file_handler)

log = logging.getLogger(__name__)


class ResilientBot(Bot):
    """Bot wrapper that retries transient Telegram network resets.

    aiogram surfaces send/connect resets as TelegramNetworkError. Without a
    retry, a single reset makes the current handler fail even though polling
    keeps running. Retrying the API method is safe for our send/edit/delete
    calls and prevents dropped replies during brief Telegram/network hiccups.
    """

    async def __call__(self, method, request_timeout=None):
        delays = (0, 2, 5, 10, 20)
        last_error = None
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await super().__call__(method, request_timeout=request_timeout)
            except TelegramNetworkError as e:
                last_error = e
                try:
                    await self.session.close()
                except Exception:
                    log.debug("Failed to reset Telegram aiohttp session", exc_info=True)
                if attempt == len(delays):
                    break
                log.warning(
                    "Telegram API network error on %s (attempt %s/%s), resetting session and retrying: %s",
                    type(method).__name__, attempt, len(delays), e,
                )
        if last_error is not None:
            raise last_error
        raise RuntimeError("Telegram API call failed without an exception")


async def main() -> None:
    config = load_config()
    telegram_session = AiohttpSession(limit=20)
    telegram_session._connector_init.update({
        "family": socket.AF_INET,
        "enable_cleanup_closed": True,
    })
    bot = ResilientBot(token=config.telegram_token, session=telegram_session)
    dp = Dispatcher()

    db = await Database.connect(config.db_path)
    log.info(f"Database connected: {config.db_path}")

    # Migrate legacy env vars to users table (one-time)
    await db.migrate_from_env(config.notion_token, config.users)

    agent = Agent(config)

    pending_edit: dict[int, str] = {}
    photo_queue: dict[int, list[str]] = {}
    processing_group: set[int] = set()
    email_cache_holder: list[NotionCache] = []
    watcher_holder: list[EmailWatcher | None] = []
    watcher_task_ref: asyncio.Task | None = None
    last_saved_page: dict[int, str] = await db.get_all_user_undo()  # user_id → notion page_id (for undo), persisted
    email_saved_pages: dict[int, dict] = await db.get_all_email_saved_pages()  # persisted across restarts
    email_pending_edit: dict[int, str] = {}  # user_id → field being edited (post-save email edit)
    income_pending_edit: dict[int, str] = {}  # user_id → field being edited (pre-save income edit)
    pending_since: dict[int, float] = await db.get_all_pending_since()  # user_id → timestamp when pending expense was created
    cat_suggestions_cache: dict[tuple[int, str], list[str]] = {}  # (user_id, description) → recommended categories
    saving_locks: dict[int, asyncio.Lock] = {}  # per-user lock to prevent confirm vs auto-confirm double-save
    auto_confirm_task: asyncio.Task | None = None  # background task handle for graceful shutdown

    def _parse_cb(data: str, idx: int = 1) -> int | None:
        try:
            return int(data.split(":")[idx])
        except (IndexError, ValueError):
            return None

    def _search_keyword(text: str) -> str | None:
        """Extract a merchant-like keyword from user expense text.
        Strips amounts, payment methods, and short noise tokens."""
        tokens = text.split()
        meaningful = []
        for t in tokens:
            clean = t.lower().replace("rp", "").replace(",", "").strip()
            # Skip numeric tokens
            try:
                float(clean.replace(".", ""))
                continue
            except ValueError:
                pass
            # Skip known payment-method tokens
            if clean in ("cash", "cas", "transfer", "tf", "debit", "kredit", "cashless", "gojek", "gopay", "ovo", "dana", "shopeepay", "qris", "kartu", "jago", "mandiri", "bsi", "bca", "bni", "bri", "cimb"):
                continue
            if len(clean) < 3 and not clean.isalpha():
                continue
            meaningful.append(t)
        return " ".join(meaningful[:4]) if meaningful else None

    def _parse_idr_amount(text: str) -> float:
        """Parse Indonesian-formatted money input into a positive float."""
        cleaned = text.replace("Rp", "").replace("rp", "").replace(" ", "").strip()
        if not cleaned:
            raise ValueError("Jumlah tidak valid")

        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned and "." not in cleaned:
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2:
                cleaned = parts[0] + "." + parts[1]
            else:
                cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "")

        amount_val = float(cleaned)
        if not math.isfinite(amount_val) or amount_val <= 0:
            raise ValueError("Jumlah tidak valid")
        return amount_val

    async def _get_email_saved(user_id: int) -> dict | None:
        saved = email_saved_pages.get(user_id)
        if not saved:
            return None
        if datetime.now().timestamp() - saved["timestamp"] > 600:
            email_saved_pages.pop(user_id, None)
            await db.clear_email_saved_page(user_id)
            return None
        return saved

    async def _prompt_next_debit(user_id: int) -> None:
        """Promote the next queued Jago debit email after the current pending expense is terminal.

        A user's reply to "Beli apa?" creates a normal pending expense that still
        needs Simpan/Edit/Batal. Prompting the next queued debit before that
        terminal action lets the next reply overwrite pending_expenses for the
        same user, losing the first transaction. Only call this after confirm,
        duplicate-removal, cancel, or auto-confirm has cleared the pending row.
        """
        if await db.get_pending_expense(user_id):
            return
        if await db.get_pending_email_expense(user_id):
            return
        if photo_queue.get(user_id) or user_id in processing_group:
            return
        next_tx = await db.pop_debit(user_id)
        if not next_tx:
            return
        await db.set_pending_email_expense(user_id, next_tx)
        await bot.send_message(
            user_id,
            f"💳 *Kartu debit Jago* — Rp {next_tx.amount:,.0f}\n"
            f"📅 {next_tx.date}  🏦 {next_tx.account}\n\n"
            f"Beli apa? Balas dengan nama merchant atau deskripsi.\n"
            f"_(Ketik *batal* untuk lewati)_",
            parse_mode="Markdown",
        )

    async def alert_owner(text: str) -> None:
        all_users = await db.get_all_users()
        for uid in all_users:
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
            except Exception as e:
                log.error(f"Failed to send alert to {uid}: {e}")

    def format_entry(entry: ExpenseEntry, cache: NotionCache | None = None) -> str:
        sub_match = cache.closest_subcategory(entry.subcategory) if cache else (entry.subcategory,)
        acc_match = cache.closest_account(entry.account) if cache else (entry.account,)
        sub_label = sub_match[0] if sub_match else f"❓ {entry.subcategory}"
        acc_label = acc_match[0] if acc_match else f"❓ {entry.account}"
        return (
            f"📝 *Deskripsi:* {entry.description}\n"
            f"💰 *Jumlah:* Rp {entry.amount:,.0f}\n"
            f"📅 *Tanggal:* {entry.date}\n"
            f"🏷 *Kategori:* {sub_label}\n"
            f"🏦 *Akun:* {acc_label}"
        )

    def format_income_entry(entry: IncomeEntry, cache: NotionCache | None = None) -> str:
        sub_match = cache.closest_income_subcategory(entry.subcategory) if cache else (entry.subcategory,)
        acc_match = cache.closest_account(entry.account) if cache else (entry.account,)
        sub_label = sub_match[0] if sub_match else f"❓ {entry.subcategory}"
        acc_label = acc_match[0] if acc_match else f"❓ {entry.account}"
        return (
            f"📝 *Deskripsi:* {entry.description}\n"
            f"💵 *Jumlah:* Rp {entry.amount:,.0f}\n"
            f"📅 *Tanggal:* {entry.date}\n"
            f"🏷 *Kategori:* {sub_label}\n"
            f"🏦 *Akun:* {acc_label}"
        )

    # ── Per-user Notion client/cache (multi-tenant) ───────────────────────────

    user_notions: dict[int, NotionClient] = {}
    user_caches: dict[int, NotionCache] = {}

    async def get_user_notion(user_id: int) -> tuple[NotionClient, NotionCache] | None:
        """Return (NotionClient, NotionCache) for user, or None if not set up."""
        if user_id in user_notions:
            return user_notions[user_id], user_caches[user_id]
        user = await db.get_user(user_id)
        if not user or not user.is_setup_complete:
            return None
        client = NotionClient.from_user(user)
        try:
            cache_entry = await client.load_cache()
        except Exception as e:
            log.error(f"Failed to load cache for user {user_id}: {e}")
            return None  # don't cache broken state
        user_notions[user_id] = client
        user_caches[user_id] = cache_entry
        return client, cache_entry

    async def _undo_last_saved(user_id: int) -> tuple[bool, str]:
        """Archive the user's latest saved Notion page and clear undo state."""
        undo = await db.get_user_undo(user_id)
        if not undo:
            return False, "Tidak ada transaksi terakhir yang bisa di-undo."

        result = await get_user_notion(user_id)
        if not result:
            return False, "Ketik /setup untuk menghubungkan Notion workspace kamu."
        user_notion, _ = result

        page_id = undo["page_id"]
        desc = undo.get("description") or "transaksi terakhir"
        amount = undo.get("amount") or 0
        tx_date = undo.get("date") or ""
        try:
            await user_notion.archive_page(page_id)
        except Exception as e:
            log.error("Undo archive failed for user %s page %s: %s", user_id, page_id, e)
            return False, f"❌ Gagal undo di Notion.\n`{type(e).__name__}: {str(e)[:80]}`"

        await db.clear_user_undo(user_id)
        last_saved_page.pop(user_id, None)
        saved = email_saved_pages.get(user_id)
        if saved and saved.get("page_id") == page_id:
            email_saved_pages.pop(user_id, None)
            await db.clear_email_saved_page(user_id)

        lines = ["↩️ *Undo berhasil.*", f"Transaksi diarsipkan dari Notion: *{desc}*"]
        if amount:
            lines.append(f"💰 Rp {amount:,.0f}")
        if tx_date:
            lines.append(f"📅 {tx_date}")
        return True, "\n".join(lines)

    # ── Setup state machine ───────────────────────────────────────────────────

    async def run_setup(msg: Message) -> None:
        """Handle the setup flow for a user based on their current setup_step."""
        user_id = msg.from_user.id
        text = msg.text.strip() if msg.text else ""

        user = await db.get_user(user_id)
        if user is None:
            # New user — create record and start setup
            await db.upsert_user(user_id, owner_name="", notion_token="", setup_step="start")
            user = await db.get_user(user_id)

        step = user.setup_step

        if step == "start":
            await db.set_user_setup_step(user_id, "await_name")
            await msg.answer(
                "👋 *Selamat datang!*\n\n"
                "Ayo hubungkan bot ini dengan workspace Notion kamu.\n"
                "Ketik nama kamu (akan digunakan sebagai prefix di Notion):",
                parse_mode="Markdown",
            )
            return

        if text.lower() in ("/setup", "setup", "/config", "config") and step not in ("start", "await_name", "migrated"):
            # Allow /setup to restart from any non-initial step
            await db.set_user_setup_step(user_id, "await_name")
            await msg.answer(
                "🔄 Setup diulang dari awal.\n"
                "Ketik nama kamu (akan digunakan sebagai prefix di Notion):",
                parse_mode="Markdown",
            )
            return

        if step == "await_name":
            if not text or len(text) > 50:
                await msg.answer("❌ Nama tidak valid. Ketik nama singkat (maks 50 karakter):")
                return
            await db.upsert_user(user_id, owner_name=text, setup_step="await_token")
            await msg.answer(
                f"✅ Oke, {text}!\n\n"
                "Sekarang butuh *Notion Integration Token*.\n\n"
                "Cara membuat:\n"
                "1. Buka https://www.notion.so/my-integrations\n"
                "2. Klik *+ New connection*\n"
                "3. Isi *Connection name* (wajib, misal: \"Expense Bot\"), pilih workspace, klik *Create connection*\n"
                "4. Buka tab *Configuration* → copy *Access token*-nya (format: `ntn_...`)\n"
                "5. *Penting:* Buka tab *Content access* → klik *Edit access* → centang halaman template kamu → Save\n\n"
                "Ketik token-nya di sini:",
                parse_mode="Markdown",
            )
            return

        if step == "await_token":
            if not text.startswith(("ntn_", "secret_")):
                await msg.answer("❌ Token tidak valid. Harus diawali `ntn_` atau `secret_`. Coba lagi:", parse_mode="Markdown")
                return
            await db.upsert_user(user_id, notion_token=text, setup_step="discovering")
            await msg.answer("🔍 Mencari database Notion di workspace kamu...")
            await _do_discovery(msg, user_id)
            return

        if step == "discovering":
            await _do_discovery(msg, user_id)
            return

        if step == "migrated":
            # User migrated from env vars — has token but needs discovery
            await msg.answer(
                f"✅ Token sudah tersimpan untuk *{user.owner_name}*.\n"
                "🔍 Mencari database Notion di workspace kamu...",
                parse_mode="Markdown",
            )
            await _do_discovery(msg, user_id)
            return

        # step == "done" — shouldn't reach here, but just in case
        await msg.answer("✅ Setup sudah selesai! Ketik /help untuk bantuan.")

    async def _do_discovery(msg: Message, user_id: int) -> None:
        """Run database discovery for a user."""
        user = await db.get_user(user_id)
        if not user:
            await msg.answer("❌ Terjadi kesalahan. Ketik /setup untuk mulai ulang.")
            return

        await db.set_user_setup_step(user_id, "discovering")
        try:
            temp_client = NotionClient(notion_token=user.notion_token, db_ids={})
            db_ids = await temp_client.discover_databases()
            await temp_client.aclose()
        except Exception as e:
            log.error(f"Discovery failed for user {user_id}: {e}")
            fail_step = "await_token" if user.setup_step != "migrated" else "migrated"
            await msg.answer(
                f"❌ Gagal menemukan database:\n`{e}`\n\n"
                "Pastikan kamu sudah membagikan halaman template ke integration. "
                + (
                    "Ketik /start untuk coba lagi."
                    if user.setup_step == "migrated"
                    else "Ketik token lagi untuk retry, atau /setup untuk mulai ulang."
                ),
                parse_mode="Markdown",
            )
            await db.set_user_setup_step(user_id, fail_step)
            return

        await db.upsert_user(user_id, setup_step="done", **db_ids)
        await msg.answer(
            "✅ *Setup selesai!*\n\n"
            "Semua database berhasil ditemukan. Sekarang kamu bisa:\n"
            "📸 Kirim foto struk → otomatis ekstrak & catat\n"
            "💬 Ketik deskripsi pengeluaran → langsung dicatat\n"
            "💰 Catat pemasukan\n"
            "❓ Tanya pengeluaran\n\n"
            "Ketik /help untuk bantuan lengkap.",
            parse_mode="Markdown",
        )
        # Initialize the user's Notion client and cache
        await get_user_notion(user_id)

    async def _process_next_photo(user_id: int, owner: str) -> None:
        while True:
            q = photo_queue.get(user_id, [])
            if not q:
                processing_group.discard(user_id)
                return

            # Get per-user Notion client and cache
            result = await get_user_notion(user_id)
            if not result:
                await bot.send_message(user_id, "Ketik /setup untuk menghubungkan Notion workspace kamu.")
                processing_group.discard(user_id)
                return

            file_id = q[0]
            user_notion, user_cache = result
            retries = 0
            while retries < 3:
                status_msg = await bot.send_message(user_id, "🔍 Membaca struk berikutnya...")
                try:
                    file = await bot.get_file(file_id)
                    image_bytes = await bot.download_file(file.file_path)
                    today = date.today().isoformat()
                    recent = await user_notion.fetch_recent_expenses(owner, user_cache, 10)
                    entry = await agent.extract_from_image(image_bytes.read(), user_cache, today, recent_expenses=recent)
                except Exception as e:
                    retries += 1
                    log.error(f"Next photo failed (attempt {retries}/3): {e}")
                    await status_msg.delete()
                    if retries >= 3:
                        await bot.send_message(user_id, f"❌ Gagal baca struk setelah 3x: `{type(e).__name__}`", parse_mode="Markdown")
                    else:
                        await bot.send_message(user_id, f"⚠️ Gagal baca struk, coba lagi... ({retries}/3)")
                        continue
                    break
                else:
                    q.pop(0)
                    await db.set_pending_expense(user_id, entry)
                    ts = datetime.now().timestamp()
                    pending_since[user_id] = ts
                    await db.set_pending_since(user_id, ts)
                    confidence_emoji = "✅" if entry.confidence >= 0.8 else "⚠️"
                    await status_msg.delete()
                    dup_warning = ""
                    try:
                        matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
                        if matches:
                            is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date, new_merchant=entry.merchant)
                            if is_dup:
                                dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
                    except Exception as e:
                        log.warning(f"Duplicate check failed: {e}")
                    await bot.send_message(
                        user_id,
                        f"{confidence_emoji} Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry, user_cache)}",
                        parse_mode="Markdown",
                        reply_markup=make_confirm_keyboard(user_id),
                    )
                    break
            if retries >= 3:
                q.pop(0)  # discard failed photo after 3 retries

    # ── Handlers ──────────────────────────────────────────────────────────────

    @dp.message(CommandStart())
    async def handle_start(msg: Message) -> None:
        user_id = msg.from_user.id
        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await run_setup(msg)
            return
        await msg.answer(
            f"Halo {user.owner_name}! 👋\n\n"
            "Kirim ke saya:\n"
            "📸 *Foto struk* → otomatis ekstrak & catat\n"
            "💬 *Teks* kayak `Nasi goreng 25k cash` → langsung dicatat\n"
            "❓ *Pertanyaan* kayak `Berapa pengeluaran bulan ini?` → dijawab\n"
            "💰 *Pemasukan* kayak `Gaji bulanan masuk 3 juta` → dicatat\n\n"
            "Ketik /help untuk bantuan lengkap.",
            parse_mode="Markdown",
        )

    @dp.message(Command("help"))
    async def handle_help(msg: Message) -> None:
        user_id = msg.from_user.id
        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        await msg.answer(
            "✨ *Apa yang bisa saya lakukan?*\n\n"
            "📸 *Foto struk*\n"
            "Kirim foto struk belanja, saya akan baca otomatis.\n"
            "Contoh: kirim foto struk Indomaret\n\n"
            "💬 *Catat pengeluaran*\n"
            "Teks biasa saja, saya paham bahasa sehari-hari.\n"
            "Contoh: `Nasi goreng 25k jago` atau `bensin 50rb mandiri`\n\n"
            "💰 *Catat pemasukan*\n"
            "Laporkan uang yang kamu terima.\n"
            "Contoh: `Gaji bulanan masuk 3 juta ke Mandiri`\n\n"
            "❓ *Tanya pengeluaran*\n"
            "Tanya soal keuangan kamu, saya jawab pakai data Notion.\n"
            "Contoh: `Berapa yang aku habiskan minggu ini?`\n\n"
            "📋 *Perintah khusus*\n"
            "/networth — lihat ringkasan aset *(butuh database Assets)*\n"
            "/budget — cek status anggaran bulanan\n"
            "/search <kata kunci> — cari pengeluaran\n"
            "/stats — ringkasan pengeluaran bulan ini\n"
            "/export — ekspor pengeluaran ke CSV (thismonth / YYYY-MM / all)\n"
            "/undo — batalkan transaksi terakhir yang tersimpan\n"
            "/recurring — lihat daftar pembayaran rutin aktif\n"
            "/refresh — muat ulang data kategori dari Notion\n"
            "/status — status email watcher dan bot\n"
            "/health — health check (DB, Notion, Watcher)\n"
            "/linkemail — hubungkan akun bank ke email watcher\n"
            "/help — tampilkan pesan ini",
            parse_mode="Markdown",
        )

    @dp.message(Command("setup"))
    async def handle_setup(msg: Message) -> None:
        await run_setup(msg)

    @dp.message(Command("undo"))
    async def handle_undo_command(msg: Message) -> None:
        user_id = msg.from_user.id
        ok, text = await _undo_last_saved(user_id)
        await msg.answer(text, parse_mode="Markdown")

    @dp.message(Command("networth"))
    async def handle_networth(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, _ = result
        user = await db.get_user(user_id)
        if not user.assets_ds:
            await msg.answer(
                "💼 *Net Worth* — fitur ini belum tersedia.\n\n"
                "Kamu perlu database *Assets* di Notion untuk menggunakan fitur ini.",
                parse_mode="Markdown",
            )
            return
        try:
            assets = await user_notion.fetch_assets()
            if not assets:
                await msg.answer(
                    "💼 *Net Worth*\n\n"
                    "Belum ada aset.\n"
                    "Tambahkan data aset di database *Assets* Notion kamu.",
                    parse_mode="Markdown",
                )
                return
            total = sum(a["value_idr"] for a in assets if a["value_idr"])
            lines = ["💼 *Kekayaan (Net Worth)*\n"]
            for a in assets:
                val = f"Rp {a['value_idr']:,.0f}" if a["value_idr"] else "_(belum diisi)_"
                qty = f"{a['quantity']:g} {a['unit']}" if a["quantity"] else ""
                lines.append(f"• *{a['name']}*{' — ' + qty if qty else ''}: {val}")
            if total:
                lines.append(f"\n💰 *Total: Rp {total:,.0f}*")
            await msg.answer("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            log.error(f"/networth failed: {e}")
            await msg.answer(f"❌ Gagal ambil data aset.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")

    @dp.message(Command("budget"))
    async def handle_budget(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        try:
            budgets = await user_notion.fetch_budgets(user_cache)
            if not budgets:
                await msg.answer("Belum ada anggaran. Tambahkan dulu di database Budget Notion.")
                return
            lines = ["💰 *Anggaran (Budget)*\n"]
            for b in budgets:
                if b["percentage"] > 100:
                    status = "🔴 OVER"
                elif b["percentage"] > 80:
                    status = "🟡"
                else:
                    status = "🟢"
                subs = f" ({', '.join(b['subcategories'])})" if b["subcategories"] else ""
                lines.append(
                    f"{status} *{b['name']}{subs}* ({b['period']})\n"
                    f"  Rp {b['spent']:,.0f} / Rp {b['budget']:,.0f}  ({b['percentage']:.0f}%)"
                )
            await msg.answer("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            log.error(f"/budget failed: {e}")
            await msg.answer(f"❌ Gagal ambil data anggaran.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")

    @dp.message(Command("recurring"))
    async def handle_recurring(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        _, user_cache = result
        recurring = user_cache.recurring_payments
        if not recurring:
            await msg.answer(
                "🔁 *Pembayaran Rutin*\n\n"
                "Belum ada pembayaran rutin aktif.\n"
                "Tambahkan di database Recurring Payment di Notion.",
                parse_mode="Markdown",
            )
            return
        lines = ["🔁 *Pembayaran Rutin Aktif*\n"]
        total = 0
        for amount, entries in sorted(recurring.items()):
            # NotionCache groups recurring payments by amount because different
            # active subscriptions can share the same nominal value. Older
            # caches used a single dict value, so keep a tiny compatibility shim.
            items = [entries] if isinstance(entries, dict) else entries
            for info in items:
                name = info.get("name", "-")
                sub = info.get("subcategory", "")
                acc = info.get("account", "")
                parts = []
                if sub:
                    parts.append(f"🏷 {sub}")
                if acc:
                    parts.append(f"🏦 {acc}")
                extra = " · ".join(parts)
                lines.append(f"• *{name}* — Rp {amount:,.0f} {extra}")
                total += amount
        lines.append(f"\n💰 *Total: Rp {total:,.0f}/bulan*")
        await msg.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("linkemail"))
    async def handle_linkemail(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup dulu untuk menghubungkan Notion.")
            return

        parts = msg.text.strip().split(maxsplit=2)
        cmd = parts[1].lower() if len(parts) > 1 else "list"

        if cmd == "list":
            accounts = await db.get_email_accounts_for_user(user_id)
            if not accounts:
                await msg.answer(
                    "📧 *Email account links*\n\n"
                    "Belum ada akun yang terhubung.\n"
                    "Gunakan: `/linkemail <nama_bank>` untuk menghubungkan.\n"
                    "Contoh: `/linkemail Mandiri`\n\n"
                    "Setelah di-link, email dari bank tersebut akan "
                    "dirutekan ke chat ini.",
                    parse_mode="Markdown",
                )
            else:
                lines = ["📧 *Akun email terhubung:*\n"]
                for a in accounts:
                    lines.append(f"• {a}")
                lines.append("\nGunakan `/linkemail remove <nama>` untuk putuskan.")
                await msg.answer("\n".join(lines), parse_mode="Markdown")

        elif cmd == "remove":
            if len(parts) < 3:
                await msg.answer(
                    "❌ Format salah. Gunakan: `/linkemail remove <nama_akun>`\n"
                    "Contoh: `/linkemail remove Mandiri`",
                    parse_mode="Markdown",
                )
                return
            pattern = parts[2].strip()
            owners = await db.get_email_accounts_for_user(user_id)
            if pattern not in owners:
                await msg.answer(f"❌ Akun `{pattern}` tidak terdaftar untuk kamu.", parse_mode="Markdown")
                return
            await db.remove_email_account_owner(pattern)
            await msg.answer(f"✅ Akun `{pattern}` telah diputuskan.", parse_mode="Markdown")

        else:
            pattern = cmd
            await db.set_email_account_owner(pattern, user_id)
            await msg.answer(
                f"✅ Akun `{pattern}` terhubung!\n\n"
                f"Email yang menyebutkan akun ini akan dirutekan ke chat kamu.",
                parse_mode="Markdown",
            )

    @dp.message(Command("refresh"))
    async def handle_refresh(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        await msg.answer("🔄 Memuat ulang cache...")
        try:
            # Rebuild NotionClient from DB to pick up any DB ID changes
            user = await db.get_user(user_id)
            if user:
                user_notion = NotionClient.from_user(user)
                user_notions[user_id] = user_notion
            new_cache = await user_notion.load_cache()
            user_caches[user_id] = new_cache
            if email_cache_holder and email_owner_record and user_id == email_owner_record.telegram_id:
                email_cache_holder[0] = new_cache
            await msg.answer(
                f"✅ Selesai! {len(new_cache.subcategories)} subkategori pengeluaran, "
                f"{len(new_cache.income_subcategories)} subkategori pemasukan, "
                f"{len(new_cache.accounts)} akun, "
                f"{len(new_cache.recurring_payments)} pembayaran rutin."
            )
        except Exception as e:
            log.error(f"/refresh failed: {e}")
            await msg.answer(f"❌ Gagal refresh: `{type(e).__name__}: {e}`", parse_mode="Markdown")

    @dp.message(Command("status"))
    async def handle_status(msg: Message) -> None:
        user_id = msg.from_user.id
        lines = ["📊 *Status Bot*\n"]
        if watcher_holder:
            w = watcher_holder[0]
            info = w.status_info()
            uptime = timedelta(seconds=int(info["uptime_seconds"]))
            lines.append(f"📧 *Email Watcher:* {'✅ Aktif' if info['running'] else '❌ Mati'}")
            if info["last_poll"]:
                last_poll = datetime.fromtimestamp(info["last_poll"])
                lines.append(f"🕐 Poll terakhir: {last_poll.strftime('%H:%M:%S')}")
            lines.append(f"⏱ Uptime: {uptime}")
            lines.append(f"📨 Diproses: {info['total_processed']} email")
            if info["notion_fail_streak"]:
                lines.append(f"⚠️ Notion gagal: {info['notion_fail_streak']}x berturut-turut")
            if info["imap_error"]:
                lines.append(f"❌ IMAP error: `{info['imap_error'][:80]}`")
        else:
            lines.append("📧 *Email Watcher:* ❌ Tidak aktif")
        total_users = len(await db.get_all_users())
        lines.append(f"👥 Pengguna: {total_users}")
        await msg.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("health"))
    async def handle_health(msg: Message) -> None:
        user_id = msg.from_user.id
        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        try:
            # Quick DB check
            user_count = len(await db.get_all_users())
            # Quick Notion check
            result = await get_user_notion(user_id)
            notion_ok = result is not None
            watcher_running = watcher_holder[0].status_info()["running"] if watcher_holder else False
            lines = [
                "🩺 *Health Check*\n",
                f"✅ Database: OK ({user_count} pengguna)",
                f"✅ Notion: {'OK' if notion_ok else '❌ Gagal'}",
                f"✅ Email Watcher: {'Aktif' if watcher_running else '❌ Mati'}",
            ]
            await msg.answer("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await msg.answer(f"❌ Health check gagal.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")

    @dp.message(Command("search"))
    async def handle_search(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, _ = result
        user = await db.get_user(user_id)
        owner = user.owner_name
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await msg.answer("🔍 *Cari pengeluaran*\n\nGunakan: `/search kata kunci`\nContoh: `/search indomie`", parse_mode="Markdown")
            return
        keyword = parts[1].strip()
        await msg.answer(f"🔍 Mencari \"{keyword}\"...")
        try:
            results = await user_notion.search_expenses(owner, keyword)
        except Exception as e:
            log.error(f"/search failed: {e}")
            await msg.answer(f"❌ Gagal mencari.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")
            return
        if not results:
            await msg.answer(f"Tidak ada pengeluaran dengan kata kunci \"{keyword}\".")
            return
        total = sum(r["amount"] for r in results)
        lines = [f"🔍 *Hasil pencarian: \"{keyword}\"* ({len(results)} transaksi, Rp {total:,.0f})\n"]
        for r in results[:10]:
            lines.append(f"• Rp {r['amount']:,.0f} — {r['description']} ({r['date']})")
        if len(results) > 10:
            lines.append(f"\n...dan {len(results) - 10} transaksi lainnya.")
        lines.append(f"\n🔍 *Pencarian selesai.*")
        await msg.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("stats"))
    async def handle_stats(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        user = await db.get_user(user_id)
        owner = user.owner_name

        await msg.answer("📊 Mengambil data pengeluaran...")

        expenses, budgets = await asyncio.gather(
            user_notion.fetch_expenses(owner, user_cache),
            user_notion.fetch_budgets(user_cache),
            return_exceptions=True,
        )
        if isinstance(expenses, Exception):
            log.error(f"/stats fetch_expenses failed: {expenses}")
            await msg.answer(f"❌ Gagal ambil data.\n`{type(expenses).__name__}: {str(expenses)[:80]}`", parse_mode="Markdown")
            return
        if isinstance(budgets, Exception):
            log.warning(f"/stats fetch_budgets failed (skipping budget section): {budgets}")
            budgets = None

        now = datetime.now()
        month_str = now.strftime("%Y-%m")
        last_month_str = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

        month_expenses = [e for e in expenses if e["date"].startswith(month_str)]
        if not month_expenses:
            await msg.answer("📊 Belum ada pengeluaran untuk bulan ini.")
            return

        total = sum(e["amount"] for e in month_expenses)
        count = len(month_expenses)

        # ── Trend line ───────────────────────────────────────────────────────
        last_month_expenses = [e for e in expenses if e["date"].startswith(last_month_str)]
        trend_line = None
        if last_month_expenses:
            last_total = sum(e["amount"] for e in last_month_expenses)
            if last_total > 0:
                delta_pct = (total - last_total) / last_total * 100
                sign = "+" if delta_pct >= 0 else ""
                trend_line = f"📈 vs bulan lalu: {sign}{delta_pct:.0f}% (Rp {last_total:,.0f})"

        # ── Daily average + projection ───────────────────────────────────────
        days_elapsed = now.day
        days_in_month = (now.replace(month=now.month % 12 + 1, day=1) - timedelta(days=1)).day
        daily_avg = total / days_elapsed
        projected = daily_avg * days_in_month

        # ── Top categories ───────────────────────────────────────────────────
        cat_totals: dict[str, float] = {}
        for e in month_expenses:
            sub = e.get("subcategory", "-")
            cat_totals[sub] = cat_totals.get(sub, 0) + e["amount"]
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
        medals = ["🥇", "🥈", "🥉"]
        top3 = sorted_cats[:3]
        rest = sorted_cats[3:]
        rest_total = sum(v for _, v in rest) if rest else 0

        # ── Biggest transaction ──────────────────────────────────────────────
        biggest = max(month_expenses, key=lambda e: e["amount"])
        big_date_parts = biggest["date"].split("-")
        months_id = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        big_day = int(big_date_parts[2])
        big_month = months_id[int(big_date_parts[1])]
        biggest_line = f"💸 Terbesar: Rp {biggest['amount']:,.0f} — {biggest.get('description', '-')} ({big_day} {big_month})"

        # ── Build message ────────────────────────────────────────────────────
        month_names = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                       "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
        lines = [f"📊 *Ringkasan {month_names[now.month]} {now.year}*\n"]
        lines.append(f"💰 Total: Rp {total:,.0f}")
        if trend_line:
            lines.append(trend_line)
        lines.append(f"📋 Transaksi: {count}  •  Rata-rata/hari: Rp {daily_avg:,.0f}")
        lines.append(f"🔮 Proyeksi akhir bulan: Rp ~{projected:,.0f}")

        lines.append("")
        lines.append("Top kategori:")
        for i, (cat, amt) in enumerate(top3):
            pct = amt / total * 100
            lines.append(f"  {medals[i]} {cat}: Rp {amt:,.0f} ({pct:.0f}%)")
        if rest:
            pct = rest_total / total * 100
            lines.append(f"  • Lainnya: Rp {rest_total:,.0f} ({pct:.0f}%)")

        lines.append("")
        lines.append(biggest_line)

        # ── Budget comparison ────────────────────────────────────────────────
        if budgets:
            lines.append("")
            lines.append("📊 vs Anggaran:")
            for b in budgets:
                if b["budget"] <= 0:
                    continue
                spent = b["spent"]
                budget = b["budget"]
                pct = b["percentage"]
                if pct >= 100:
                    emoji = "🔴"
                    warn = " ⚠️"
                elif pct >= 80:
                    emoji = "🟡"
                    warn = ""
                else:
                    emoji = "🟢"
                    warn = ""
                lines.append(f"  {emoji} {b['name']}: Rp {spent:,.0f} / {budget:,.0f} ({pct:.0f}%{warn})")

        await msg.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("export"))
    async def handle_export(msg: Message) -> None:
        user_id = msg.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        user = await db.get_user(user_id)
        owner = user.owner_name

        parts = msg.text.split(maxsplit=1)
        # Optional filter: /export thismonth | /export <month> | /export all
        filter_type = parts[1].lower().strip() if len(parts) > 1 else "thismonth"

        try:
            expenses = await user_notion.fetch_expenses(owner, user_cache)
        except Exception as e:
            log.error(f"/export fetch_expenses failed: {e}")
            await msg.answer(f"❌ Gagal ambil data.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")
            return

        if not expenses:
            await msg.answer("Belum ada pengeluaran untuk diekspor.")
            return

        # Filter by date
        now = datetime.now()
        if filter_type == "thismonth":
            prefix = now.strftime("%Y-%m")
            expenses = [e for e in expenses if e["date"].startswith(prefix)]
            filename = f"expenses_{prefix}.csv"
        elif filter_type.strip().count("-") == 1 and len(filter_type.strip()) == 7:
            # looks like YYYY-MM
            expenses = [e for e in expenses if e["date"].startswith(filter_type.strip())]
            filename = f"expenses_{filter_type.strip()}.csv"
        else:
            filename = "expenses_all.csv"

        if not expenses:
            await msg.answer(f"Tidak ada data untuk filter `{filter_type}`.")
            return

        # Build CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Tanggal", "Deskripsi", "Jumlah (IDR)", "Kategori"])
        for e in expenses:
            writer.writerow([e["date"], e.get("description", ""), int(e["amount"]), e.get("subcategory", "-")])

        csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility

        await msg.answer_document(
            BufferedInputFile(csv_bytes, filename=filename),
            caption=f"📊 *Export {len(expenses)} transaksi*\n{filter_type}"
        )

    @dp.message(F.photo)
    async def handle_photo(msg: Message) -> None:
        user_id = msg.from_user.id

        # Check setup state
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result

        user = await db.get_user(user_id)
        owner = user.owner_name

        # ── Multi-photo queuing ────────────────────────────────────────────────
        if msg.media_group_id:
            if user_id in processing_group:
                q = photo_queue.setdefault(user_id, [])
                q.append(msg.photo[-1].file_id)
                await msg.answer(f"📸 Foto #{len(q)} diantrekan...")
                return
            else:
                processing_group.add(user_id)

        status_msg = await msg.answer("🔍 Membaca struk...")

        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        today = date.today().isoformat()
        recent = await user_notion.fetch_recent_expenses(owner, user_cache, 10)

        try:
            entry = await agent.extract_from_image(image_bytes.read(), user_cache, today, recent_expenses=recent)
        except Exception as e:
            log.error(f"extract_from_image failed: {e}")
            await status_msg.delete()
            await msg.answer(
                "❌ Gagal baca struk. Coba foto yang lebih jelas.\n"
                f"`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )
            return

        await db.set_pending_expense(user_id, entry)
        ts = datetime.now().timestamp()
        pending_since[user_id] = ts
        await db.set_pending_since(user_id, ts)
        confidence_emoji = "✅" if entry.confidence >= 0.8 else "⚠️"
        await status_msg.delete()

        dup_warning = ""
        try:
            matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date, new_merchant=entry.merchant)
                if is_dup:
                    dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
        except Exception as e:
            log.warning(f"Duplicate check failed: {e}")

        await msg.answer(
            f"{confidence_emoji} Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.message(F.text)
    async def handle_text(msg: Message) -> None:
        user_id = msg.from_user.id
        if not msg.text:
            return
        text = msg.text.strip()

        # ── Setup flow (check before anything else) ───────────────────────────
        user = await db.get_user(user_id)
        if user and user.setup_step != "done":
            await run_setup(msg)
            return
        if not user:
            # New user — start setup
            await run_setup(msg)
            return

        # Get per-user Notion client and cache
        result = await get_user_notion(user_id)
        if not result:
            await msg.answer("Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        owner = user.owner_name

        # ── Pending edit (check before Jago to avoid race) ──────────────────────
        edit_field = pending_edit.pop(user_id, None)
        if edit_field and text.lower().strip() in ("batal", "cancel", "/cancel"):
            entry = await db.get_pending_expense(user_id)
            if entry:
                await msg.answer(
                    f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
                    parse_mode="Markdown",
                    reply_markup=make_confirm_keyboard(user_id),
                )
            return
        if edit_field:
            entry = await db.get_pending_expense(user_id)
            if not entry:
                await msg.answer("Tidak ada pengeluaran pending.")
                return
            if edit_field == "desc":
                for k in list(cat_suggestions_cache):
                    if k[0] == user_id:
                        del cat_suggestions_cache[k]
                entry.description = text
            elif edit_field == "amount":
                try:
                    entry.amount = _parse_idr_amount(text)
                except ValueError:
                    pending_edit[user_id] = "amount"
                    await msg.answer("❌ Angka tidak valid. Ketik jumlah angka saja (contoh: 25000):")
                    return
            elif edit_field == "date":
                try:
                    datetime.strptime(text, "%Y-%m-%d")
                except ValueError:
                    await msg.answer("❌ Format tanggal harus YYYY-MM-DD (contoh: 2026-06-10):")
                    return
                entry.date = text
            await db.set_pending_expense(user_id, entry)
            ts = datetime.now().timestamp()
            pending_since[user_id] = ts
            await db.set_pending_since(user_id, ts)
            await msg.answer(
                f"✅ Diubah! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )
            return

        # ── Pre-save income edit (desc/amount/date/subcategory text input) ──────
        income_field = income_pending_edit.pop(user_id, None)
        if income_field and text.lower().strip() in ("batal", "cancel", "/cancel"):
            income = await db.get_pending_income(user_id)
            if income:
                await msg.answer(
                    f"Oke! Konfirmasi:\n\n{format_income_entry(income, user_cache)}",
                    parse_mode="Markdown",
                    reply_markup=make_income_confirm_keyboard(user_id),
                )
            return
        if income_field:
            income = await db.get_pending_income(user_id)
            if not income:
                await msg.answer("Tidak ada pemasukan pending.")
                return
            if income_field == "desc":
                income.description = text
            elif income_field == "amount":
                try:
                    income.amount = _parse_idr_amount(text)
                except ValueError:
                    income_pending_edit[user_id] = "amount"
                    await msg.answer("❌ Angka tidak valid. Ketik jumlah angka saja (contoh: 25000):")
                    return
            elif income_field == "date":
                try:
                    datetime.strptime(text, "%Y-%m-%d")
                except ValueError:
                    await msg.answer("❌ Format tanggal harus YYYY-MM-DD (contoh: 2026-06-10):")
                    return
                income.date = text
            elif income_field == "subcategory":
                income.subcategory = text
            await db.set_pending_income(user_id, income)
            await msg.answer(
                f"✅ Diubah! Konfirmasi:\n\n{format_income_entry(income, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_income_confirm_keyboard(user_id),
            )
            return

        # ── Email post-save edit (desc/amount/date text input) ──────────────────
        email_field = email_pending_edit.pop(user_id, None)
        if email_field and text.lower().strip() in ("batal", "cancel", "/cancel"):
            await msg.answer("✅ Edit dibatalkan.")
            return
        if email_field:
            saved = email_saved_pages.get(user_id)
            if not saved:
                await msg.answer("Sesi kedaluwarsa.")
                return
            page_id = saved["page_id"]
            try:
                if email_field == "desc":
                    await user_notion.update_expense_title(page_id, text, owner)
                    email_saved_pages[user_id]["description"] = text
                elif email_field == "amount":
                    amount = _parse_idr_amount(text)
                    await user_notion.update_expense_amount(page_id, amount)
                    email_saved_pages[user_id]["amount"] = amount
                elif email_field == "date":
                    await user_notion.update_expense_date(page_id, text)
                    email_saved_pages[user_id]["date"] = text
                elif email_field == "account":
                    await user_notion.update_expense_account(page_id, text, user_cache)
                elif email_field == "detail":
                    cats = list(user_cache.category_subcategories.keys())
                    past = None
                    try:
                        kw = _search_keyword(text)
                        if kw:
                            past = await user_notion.search_expenses(owner, kw, user_cache)
                    except Exception as e:
                        log.warning(f"Past suggest-category search failed: {e}")
                    recommended = await agent.suggest_categories(text, cats, past_similar=past)
                    new_desc = f"{saved['description']} — {text}"
                    await user_notion.update_expense_title(page_id, new_desc, owner)
                    email_saved_pages[user_id]["description"] = new_desc
                    if recommended:
                        top_cat = recommended[0]
                        subcats = user_cache.category_subcategories.get(top_cat, [])
                        if subcats:
                            new_subcat = subcats[0]
                            await user_notion.update_expense_subcategory(page_id, new_subcat, user_cache)
                            email_saved_pages[user_id]["subcat"] = new_subcat
                s = email_saved_pages[user_id]
                await db.set_email_saved_page(user_id, s["page_id"], s["description"], s["amount"], s["date"], s["subcat"], s["timestamp"], merchant=s.get("merchant", ""))
                await msg.answer(f"✅ Tersimpan!")

            except Exception as e:
                log.error(f"Email post-save edit failed: {e}")
                email_pending_edit[user_id] = email_field
                await msg.answer(f"❌ Gagal menyimpan.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")
            return

        # ── Jago debit card follow-up ──────────────────────────────────────────
        pending_tx = await db.get_pending_email_expense(user_id)
        if pending_tx:
            if text.lower().strip() in ("batal", "cancel", "skip", "/cancel"):
                await db.clear_pending_email_expense(user_id)
                await db.clear_debit_queue(user_id)
                await msg.answer("Transaksi debit dibatalkan ❌")
                return
            await db.clear_pending_email_expense(user_id)
            # Use AI to extract proper subcategory from user's description
            try:
                today = date.today().isoformat()
                # Fetch recent expenses for context
                recent = await user_notion.fetch_recent_expenses(owner, user_cache, 10)
                entry = await agent.extract_from_text(
                    text, user_cache, today, recent_expenses=recent
                )
                # Override amount/date/account from the original email
                entry.amount = pending_tx.amount
                entry.date = pending_tx.date
                entry.account = pending_tx.account
                entry.confidence = 0.9
            except Exception as e:
                log.warning(f"AI re-classify failed for debit reply: {e}")
                # Fallback: use description as-is with old subcategory
                entry = ExpenseEntry(
                    description=text,
                    amount=pending_tx.amount,
                    date=pending_tx.date,
                    subcategory=pending_tx.subcategory,
                    account=pending_tx.account,
                    confidence=0.9,
                    merchant=text,
                )
            # Learn the merchant name for this amount (Jago debit cache)
            await db.set_debit_merchant(user_id, pending_tx.amount, entry.merchant or text)
            await db.set_pending_expense(user_id, entry)
            ts = datetime.now().timestamp()
            pending_since[user_id] = ts
            await db.set_pending_since(user_id, ts)
            await msg.answer(
                f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )
            return

        # ── Intent detection ──────────────────────────────────────────────────
        try:
            intent = await agent.detect_intent(text)
        except Exception as e:
            log.error(f"detect_intent failed: {e}")
            await msg.answer(
                "❌ Gagal memahami pesan. Coba lagi ya.\n"
                f"`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )
            return

        if intent.type == "query":
            await msg.answer("🔎 Mengambil data pengeluaran...")
            try:
                expenses, assets = await asyncio.gather(
                    user_notion.fetch_expenses(owner, user_cache),
                    user_notion.fetch_assets(),
                )
                history = await db.get_history(user_id)
                answer, history = await agent.answer_query(text, expenses, owner, history, assets=assets)
                await db.append_history(user_id, "user", text)
                await db.append_history(user_id, "assistant", answer)
                await msg.answer(answer)
            except Exception as e:
                log.error(f"query flow failed: {e}")
                await msg.answer(
                    f"❌ Gagal ambil data pengeluaran.\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )

        elif intent.type == "log_text":
            today = date.today().isoformat()
            # Fetch recent expenses and search past transactions in parallel
            kw = _search_keyword(text)
            recent, past = await asyncio.gather(
                user_notion.fetch_recent_expenses(owner, user_cache, 10),
                user_notion.search_expenses(owner, kw, user_cache) if kw else asyncio.sleep(0, result=None),
                return_exceptions=True,
            )
            if isinstance(recent, Exception):
                log.warning(f"fetch_recent_expenses failed: {recent}")
                recent = []
            if isinstance(past, Exception):
                log.warning(f"Past expense search failed: {past}")
                past = None
            try:
                entry = await agent.extract_from_text(text, user_cache, today, recent_expenses=recent, past_similar=past)
            except Exception as e:
                log.error(f"extract_from_text failed: {e}")
                await msg.answer(
                    "❌ Gagal baca pengeluaran. Contoh: 'Nasi goreng 25k cash'\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )
                return

            await db.set_pending_expense(user_id, entry)
            ts = datetime.now().timestamp()
            pending_since[user_id] = ts
            await db.set_pending_since(user_id, ts)
            dup_warning = ""
            try:
                matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
                if matches:
                    is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date, new_merchant=entry.merchant)
                    if is_dup:
                        dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
            except Exception as e:
                log.warning(f"Duplicate check failed: {e}")
            await msg.answer(
                f"Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )

        elif intent.type == "log_income":
            today = date.today().isoformat()
            try:
                income = await agent.extract_income_from_text(text, user_cache, today)
            except Exception as e:
                log.error(f"extract_income_from_text failed: {e}")
                await msg.answer(
                    "❌ Gagal baca pemasukan. Contoh: 'Gaji bulanan masuk 3 juta ke Jago'\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )
                return

            await db.set_pending_income(user_id, income)
            await msg.answer(
                f"💰 Pemasukan! Konfirmasi:\n\n{format_income_entry(income, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_income_confirm_keyboard(user_id),
            )

        else:
            await handle_help(msg)

    # ── Inline keyboard callbacks ─────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("confirm:"))
    async def handle_confirm(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        lock = saving_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await callback.answer("Sedang diproses...")
            return
        async with lock:
            await _do_confirm(callback, user_id)

    async def _do_confirm(callback: CallbackQuery, user_id: int) -> None:
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        user_notion, user_cache = result
        user = await db.get_user(user_id)
        owner = user.owner_name

        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("Tidak ada pengeluaran pending.")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Re-check duplicates before saving — email may have logged it in the meantime
        try:
            matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date, new_merchant=entry.merchant)
                if is_dup:
                    pending_since.pop(user_id, None)
                    await db.clear_pending_expense(user_id)
                    await db.clear_pending_since(user_id)
                    await callback.answer("Duplikat — sudah tercatat.")
                    await callback.message.answer(
                        "⚠️ *Duplikat terdeteksi!* Transaksi ini sudah tercatat sebelumnya.\n"
                        "Pending expense dihapus.",
                        parse_mode="Markdown",
                    )
                    await _prompt_next_debit(user_id)
                    return
        except Exception as e:
            log.warning(f"Confirm duplicate check failed: {e}")

        await callback.answer("Menyimpan...")
        status_msg = await callback.message.answer("⏳ Menyimpan ke Notion...")
        try:
            pending_rec = await db.get_pending_recurring(user_id)
            rec_page_url = pending_rec["recurring_page_url"] if pending_rec else None

            url = await user_notion.log_expense(entry, owner, user_cache, recurring_page_url=rec_page_url)
            page_id = _url_to_id(url)
            last_saved_page[user_id] = page_id

            if pending_rec:
                await db.mark_processed(pending_rec["uid"], pending_rec["sender"])

            await db.clear_pending_expense(user_id)
            pending_since.pop(user_id, None)
            await db.clear_pending_since(user_id)
            if pending_rec:
                await db.clear_pending_recurring(user_id)

            await db.set_user_undo(user_id, page_id, entry.description, entry.amount, entry.date, entry.subcategory, merchant=entry.merchant)
            await db.record_pattern(
                user_id, entry.merchant, entry.subcategory,
                entry.account, entry.amount, entry.date,
            )
            await status_msg.edit_text(
                f"✅ Tersimpan! [Lihat di Notion]({url})",
                parse_mode="Markdown",
                reply_markup=make_undo_keyboard(user_id),
            )
            await _prompt_next_debit(user_id)
            photo_task = asyncio.create_task(_process_next_photo(user_id, owner))
            photo_task.add_done_callback(lambda t: t.exception() and log.warning(f"Photo queue task failed: {t.exception()}"))
        except Exception as e:
            log.error(f"Notion write failed: {e}")
            await db.set_pending_expense(user_id, entry)
            ts = datetime.now().timestamp()
            pending_since[user_id] = ts
            await db.set_pending_since(user_id, ts)
            await status_msg.edit_text(
                f"❌ Gagal simpan ke Notion.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("edit:"))
    async def handle_edit(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        await callback.message.edit_reply_markup(
            reply_markup=make_edit_field_keyboard(user_id)
        )
        await callback.answer("Pilih field yang diedit")

    @dp.callback_query(F.data.startswith("edit_desc:"))
    async def handle_edit_desc(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "desc"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik deskripsi baru")
        await callback.message.answer("✏️ Ketik deskripsi baru:")

    @dp.callback_query(F.data.startswith("edit_amount:"))
    async def handle_edit_amount(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "amount"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik jumlah baru")
        await callback.message.answer("✏️ Ketik jumlah baru (contoh: 25000):")

    @dp.callback_query(F.data.startswith("edit_date:"))
    async def handle_edit_date(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "date"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik tanggal baru")
        await callback.message.answer("✏️ Ketik tanggal baru (YYYY-MM-DD):")

    async def _show_cat_keyboard(callback: CallbackQuery, user_id: int) -> None:
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        entry = await db.get_pending_expense(user_id)
        cats = list(user_cache.category_subcategories.keys())
        markup: InlineKeyboardMarkup | None = None
        if entry:
            cache_key = (user_id, entry.description)
            recommended = cat_suggestions_cache.get(cache_key)
            if recommended is None:
                recommended = await agent.suggest_categories(entry.description, cats)
                # LRU eviction: keep cache under 500 entries
                if len(cat_suggestions_cache) >= 500:
                    oldest = next(iter(cat_suggestions_cache))
                    del cat_suggestions_cache[oldest]
                cat_suggestions_cache[cache_key] = recommended
            rec_buttons: list[list[InlineKeyboardButton]] = []
            for cat in recommended:
                if cat in cats:
                    i = cats.index(cat)
                    rec_buttons.append([InlineKeyboardButton(
                        text=cat, callback_data=f"edit_cat_pick:{user_id}:{i}",
                    )])
            if rec_buttons:
                rec_buttons.append([InlineKeyboardButton(
                    text="Lainnya →", callback_data=f"edit_cat_all:{user_id}",
                )])
                rec_buttons.append([InlineKeyboardButton(
                    text="❌ Batal", callback_data=f"edit_cat_cancel:{user_id}",
                )])
                markup = InlineKeyboardMarkup(inline_keyboard=rec_buttons)
        if not markup:
            all_buttons = [[InlineKeyboardButton(text=cat, callback_data=f"edit_cat_pick:{user_id}:{i}")] for i, cat in enumerate(cats)]
            all_buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data=f"edit_cat_cancel:{user_id}")])
            markup = InlineKeyboardMarkup(inline_keyboard=all_buttons)
        await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Pilih kategori")

    @dp.callback_query(F.data.startswith("edit_cat:"))
    async def handle_edit_cat(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        await _show_cat_keyboard(callback, user_id)

    @dp.callback_query(F.data.startswith("edit_cat_pick:"))
    async def handle_edit_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        split_data = callback.data.split(":", 2)
        if len(split_data) != 3:
            await callback.answer("Data tidak valid.")
            return
        user_id = _parse_cb(callback.data, 1)
        cat_index = _parse_cb(callback.data, 2)
        if user_id is None or cat_index is None:
            await callback.answer("Data tidak valid.")
            return
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = user_cache.category_subcategories[cat_name]
        rows = [subcats[i:i + 2] for i in range(0, len(subcats), 2)]
        buttons = []
        offset = 0
        for row in rows:
            row_buttons = []
            for si, s in enumerate(row):
                row_buttons.append(InlineKeyboardButton(
                    text=s,
                    callback_data=f"edit_subcat_pick:{user_id}:{cat_index}:{offset + si}",
                ))
            buttons.append(row_buttons)
            offset += len(row)
        buttons.append([
            InlineKeyboardButton(text="⬅️ Kembali", callback_data=f"edit_cat_back:{user_id}")
        ])
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer(f"Pilih subkategori {cat_name}")

    @dp.callback_query(F.data.startswith("edit_cat_back:"))
    async def handle_edit_cat_back(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        await _show_cat_keyboard(callback, user_id)

    @dp.callback_query(F.data.startswith("edit_cat_all:"))
    async def handle_edit_cat_all(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        all_buttons = [[InlineKeyboardButton(text=cat, callback_data=f"edit_cat_pick:{user_id}:{i}")] for i, cat in enumerate(cats)]
        all_buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data=f"edit_cat_cancel:{user_id}")])
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=all_buttons)
        )
        await callback.answer("Semua kategori")

    @dp.callback_query(F.data.startswith("edit_subcat_pick:"))
    async def handle_edit_subcat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        split_data = callback.data.split(":", 3)
        if len(split_data) != 4:
            await callback.answer("Data tidak valid.")
            return
        user_id = _parse_cb(callback.data, 1)
        cat_index = _parse_cb(callback.data, 2)
        subcat_index = _parse_cb(callback.data, 3)
        if user_id is None or cat_index is None or subcat_index is None:
            await callback.answer("Data tidak valid.")
            return
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = user_cache.category_subcategories[cat_name]
        if subcat_index >= len(subcats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        subcat_name = subcats[subcat_index]
        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("Tidak ada pengeluaran pending.")
            return
        entry.subcategory = subcat_name
        await db.set_pending_expense(user_id, entry)
        ts = datetime.now().timestamp()
        pending_since[user_id] = ts
        await db.set_pending_since(user_id, ts)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Kategori diubah!")
        await callback.message.answer(
            f"✅ Kategori diubah ke *{subcat_name}*\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.callback_query(F.data.startswith("edit_cat_cancel:"))
    async def handle_edit_cat_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("Tidak ada pengeluaran pending.")
            return
        result = await get_user_notion(user_id)
        user_cache = result[1] if result else None
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        ts = datetime.now().timestamp()
        pending_since[user_id] = ts
        await db.set_pending_since(user_id, ts)
        await callback.message.answer(
            f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.callback_query(F.data.startswith("edit_cancel:"))
    async def handle_edit_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("Tidak ada pengeluaran pending.")
            return
        result = await get_user_notion(user_id)
        user_cache = result[1] if result else None
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        ts = datetime.now().timestamp()
        pending_since[user_id] = ts
        await db.set_pending_since(user_id, ts)
        await callback.message.answer(
            f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.callback_query(F.data.startswith("cancel:"))
    async def handle_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await callback.answer("Tidak punya akses.")
            return

        pending_rec = await db.get_pending_recurring(user_id)
        if pending_rec:
            await db.mark_processed(pending_rec["uid"], pending_rec["sender"])

        await db.clear_pending_expense(user_id)
        pending_since.pop(user_id, None)
        await db.clear_pending_since(user_id)
        await db.clear_pending_email_expense(user_id)
        await db.clear_debit_queue(user_id)
        await db.clear_pending_recurring(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("Dibatalkan ❌")
        await _prompt_next_debit(user_id)
        photo_task = asyncio.create_task(_process_next_photo(user_id, user.owner_name))
        photo_task.add_done_callback(lambda t: t.exception() and log.warning(f"Photo queue task failed: {t.exception()}"))

    @dp.callback_query(F.data.startswith("income_confirm:"))
    async def handle_income_confirm(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        user_notion, user_cache = result
        user = await db.get_user(user_id)
        owner = user.owner_name

        income = await db.get_pending_income(user_id)
        if not income:
            await callback.answer("Tidak ada pemasukan pending.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)

        # Re-check duplicates before saving
        try:
            income_matches = await user_notion.fetch_duplicates(owner, income.amount, income.date, db_key="income_ds")
            if income_matches:
                is_dup = await agent.check_duplicate(income_matches, income.description, income.amount, income.date, new_merchant="")
                if is_dup:
                    await db.clear_pending_income(user_id)
                    await callback.answer("Duplikat — sudah tercatat.")
                    await callback.message.answer(
                        "⚠️ *Duplikat terdeteksi!* Pemasukan ini sudah tercatat sebelumnya.",
                        parse_mode="Markdown",
                    )
                    return
        except Exception as e:
            log.warning(f"Income confirm duplicate check failed: {e}")

        await callback.answer("Menyimpan pemasukan...")
        status_msg = await callback.message.answer("⏳ Menyimpan pemasukan ke Notion...")
        try:
            url = await user_notion.log_income(income, owner, user_cache)
            await db.clear_pending_income(user_id)
            page_id = _url_to_id(url)
            last_saved_page[user_id] = page_id
            await db.set_user_undo(user_id, page_id, income.description, income.amount, income.date, income.subcategory)
            await status_msg.edit_text(
                f"✅ Pemasukan tersimpan! [Lihat di Notion]({url})",
                parse_mode="Markdown",
                reply_markup=make_undo_keyboard(user_id),
            )
        except Exception as e:
            log.error(f"Notion income write failed: {e}")
            await status_msg.edit_text(
                f"❌ Gagal simpan pemasukan ke Notion.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("income_cancel:"))
    async def handle_income_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await callback.answer("Tidak punya akses.")
            return

        await db.clear_pending_income(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("Dibatalkan ❌")

    # ── Pre-save income edit callbacks ──────────────────────────────────────────

    @dp.callback_query(F.data.startswith("income_edit:"))
    async def handle_income_edit(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income = await db.get_pending_income(user_id)
        if not income:
            await callback.answer("Tidak ada pemasukan pending.")
            return
        await callback.message.edit_reply_markup(
            reply_markup=make_income_edit_field_keyboard(user_id),
        )
        await callback.answer("Pilih field yang diedit")

    @dp.callback_query(F.data.startswith("income_edit_desc:"))
    async def handle_income_edit_desc(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income_pending_edit[user_id] = "desc"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik deskripsi baru")
        await callback.message.answer("✏️ Ketik deskripsi baru:")

    @dp.callback_query(F.data.startswith("income_edit_amount:"))
    async def handle_income_edit_amount(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income_pending_edit[user_id] = "amount"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik jumlah baru")
        await callback.message.answer("✏️ Ketik jumlah baru (contoh: 25000):")

    @dp.callback_query(F.data.startswith("income_edit_date:"))
    async def handle_income_edit_date(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income_pending_edit[user_id] = "date"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik tanggal baru")
        await callback.message.answer("✏️ Ketik tanggal baru (YYYY-MM-DD):")

    @dp.callback_query(F.data.startswith("income_edit_cat:"))
    async def handle_income_edit_cat(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income_pending_edit[user_id] = "subcategory"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik kategori baru")
        await callback.message.answer(
            "✏️ Ketik kategori pemasukan baru (contoh: Gaji / Hadiah / Refund):"
        )

    @dp.callback_query(F.data.startswith("income_edit_cancel:"))
    async def handle_income_edit_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        income = await db.get_pending_income(user_id)
        if not income:
            await callback.answer("Tidak ada pemasukan pending.")
            return
        await callback.message.edit_reply_markup(
            reply_markup=make_income_confirm_keyboard(user_id),
        )
        await callback.answer("Edit dibatalkan")

    # ── Undo ─────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("undo:"))
    async def handle_undo(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        ok, text = await _undo_last_saved(user_id)
        if ok:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer("Dibatalkan!")
        else:
            await callback.answer("❌ Gagal undo." if text.startswith("❌") else text[:60])
        await callback.message.answer(text, parse_mode="Markdown")

    # ── Email auto-log edit (post-save) ──────────────────────────────────────

    @dp.callback_query(F.data.startswith("email_edit:"))
    async def handle_email_edit(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        saved = await _get_email_saved(user_id)
        if not saved:
            await callback.answer("Waktu edit sudah habis (10 menit).")
            await callback.message.edit_reply_markup(reply_markup=None)
            return
        await callback.message.answer(
            f"✏️ *Edit transaksi*\n\n"
            f"📝 {saved['description']}\n"
            f"💰 Rp {saved['amount']:,.0f}\n"
            f"📅 {saved['date']}\n"
            f"🏷 {saved['subcat']}\n\n"
            f"Pilih field yang ingin diubah:",
            parse_mode="Markdown",
            reply_markup=make_email_edit_keyboard(user_id),
        )
        await callback.answer("Pilih field")

    @dp.callback_query(F.data.startswith("email_edit_desc:"))
    async def handle_email_edit_desc(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        if not await _get_email_saved(user_id):
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        email_pending_edit[user_id] = "desc"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik deskripsi baru")
        await callback.message.answer("✏️ Ketik deskripsi baru:")

    @dp.callback_query(F.data.startswith("email_edit_amount:"))
    async def handle_email_edit_amount(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        if not await _get_email_saved(user_id):
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        email_pending_edit[user_id] = "amount"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik jumlah baru")
        await callback.message.answer("✏️ Ketik jumlah baru (contoh: 25000):")

    @dp.callback_query(F.data.startswith("email_edit_date:"))
    async def handle_email_edit_date(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        if not await _get_email_saved(user_id):
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        email_pending_edit[user_id] = "date"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik tanggal baru")
        await callback.message.answer("✏️ Ketik tanggal baru (YYYY-MM-DD):")

    @dp.callback_query(F.data.startswith("email_edit_account:"))
    async def handle_email_edit_account(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        if not await _get_email_saved(user_id):
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        email_pending_edit[user_id] = "account"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik akun baru")
        await callback.message.answer("✏️ Ketik akun baru (contoh: Mandiri / Jago):")

    @dp.callback_query(F.data.startswith("email_edit_detail:"))
    async def handle_email_edit_detail(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        if not await _get_email_saved(user_id):
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        email_pending_edit[user_id] = "detail"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik rincian belanja")
        await callback.message.answer(
            "📋 Ketik rincian barang yang dibeli.\n"
            "Contoh: `sosro fruit tea 350ml x1, indomie goreng rendang 2x + telur 1x`\n\n"
            "AI akan otomatis menentukan kategori berdasarkan rincian."
        )

    @dp.callback_query(F.data.startswith("email_edit_subcat:"))
    async def handle_email_edit_subcat(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        saved = await _get_email_saved(user_id)
        if not saved:
            await callback.answer("Waktu edit sudah habis (10 menit).")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        buttons = [[InlineKeyboardButton(text=cat, callback_data=f"email_edit_cat_pick:{user_id}:{i}")] for i, cat in enumerate(cats)]
        buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data=f"email_edit_cancel:{user_id}")])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🏷 Pilih kategori:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer("Pilih kategori")

    @dp.callback_query(F.data.startswith("email_edit_cat_pick:"))
    async def handle_email_edit_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        split_data = callback.data.split(":", 2)
        if len(split_data) != 3:
            await callback.answer("Data tidak valid.")
            return
        user_id = _parse_cb(callback.data, 1)
        cat_index = _parse_cb(callback.data, 2)
        if user_id is None or cat_index is None:
            await callback.answer("Data tidak valid.")
            return
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        saved = await _get_email_saved(user_id)
        if not saved:
            await callback.answer("Sesi kedaluwarsa.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = user_cache.category_subcategories[cat_name]
        rows = [subcats[i:i + 2] for i in range(0, len(subcats), 2)]
        buttons = []
        offset = 0
        for row in rows:
            row_buttons = []
            for si, s in enumerate(row):
                row_buttons.append(InlineKeyboardButton(
                    text=s,
                    callback_data=f"email_edit_subcat_pick:{user_id}:{cat_index}:{offset + si}",
                ))
            buttons.append(row_buttons)
            offset += len(row)
        buttons.append([InlineKeyboardButton(text="⬅️ Kembali", callback_data=f"email_edit_subcat_back:{user_id}")])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"🏷 Pilih subkategori *{cat_name}*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer(f"Pilih subkategori {cat_name}")

    @dp.callback_query(F.data.startswith("email_edit_subcat_back:"))
    async def handle_email_edit_subcat_back(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        saved = await _get_email_saved(user_id)
        if not saved:
            await callback.answer("Sesi kedaluwarsa.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        buttons = [[InlineKeyboardButton(text=cat, callback_data=f"email_edit_cat_pick:{user_id}:{i}")] for i, cat in enumerate(cats)]
        buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data=f"email_edit_cancel:{user_id}")])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🏷 Pilih kategori:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer("Kembali")

    @dp.callback_query(F.data.startswith("email_edit_subcat_pick:"))
    async def handle_email_edit_subcat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        split_data = callback.data.split(":", 3)
        if len(split_data) != 4:
            await callback.answer("Data tidak valid.")
            return
        user_id = _parse_cb(callback.data, 1)
        cat_index = _parse_cb(callback.data, 2)
        subcat_index = _parse_cb(callback.data, 3)
        if user_id is None or cat_index is None or subcat_index is None:
            await callback.answer("Data tidak valid.")
            return
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        saved = await _get_email_saved(user_id)
        if not saved:
            await callback.answer("Sesi kedaluwarsa.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        user_notion, user_cache = result
        cats = list(user_cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        subcats = user_cache.category_subcategories[cats[cat_index]]
        if subcat_index >= len(subcats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        subcat_name = subcats[subcat_index]
        try:
            await user_notion.update_expense_subcategory(saved["page_id"], subcat_name, user_cache)
            email_saved_pages[user_id]["subcat"] = subcat_name
            s = email_saved_pages[user_id]
            await db.set_email_saved_page(user_id, s["page_id"], s["description"], s["amount"], s["date"], s["subcat"], s["timestamp"], merchant=s.get("merchant", ""))
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("✅ Kategori diubah!")
            await callback.message.answer(
                f"✅ Kategori diubah ke *{subcat_name}*",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Email edit subcategory failed: {e}")
            await callback.answer("❌ Gagal.")

    @dp.callback_query(F.data.startswith("email_edit_cancel:"))
    async def handle_email_edit_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = _parse_cb(callback.data, 1)
        if user_id is None or callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        email_pending_edit.pop(user_id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("✅ Edit dibatalkan.")

    @dp.callback_query(F.data.startswith("cat_pick:"))
    async def handle_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = callback.from_user.id
        split_data = callback.data.split(":", 2)
        if len(split_data) != 3:
            await callback.answer("Data tidak valid.")
            return
        page_id = split_data[1]
        cat_index = _parse_cb(callback.data, 2)
        if cat_index is None:
            await callback.answer("Data tidak valid.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        await callback.message.edit_reply_markup(
            reply_markup=make_subcategory_keyboard(page_id, cat_index, user_cache)
        )
        await callback.answer("Pilih subkategori")

    @dp.callback_query(F.data.startswith("cat_back:"))
    async def handle_cat_back(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id = callback.data.split(":", 1)
        result = await get_user_notion(callback.from_user.id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        await callback.message.edit_reply_markup(
            reply_markup=make_category_keyboard(page_id, user_cache)
        )
        await callback.answer("Kembali ke daftar kategori")

    @dp.callback_query(F.data.startswith("cat_all:"))
    async def handle_cat_all(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id = callback.data.split(":", 1)
        result = await get_user_notion(callback.from_user.id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        await callback.message.edit_reply_markup(
            reply_markup=make_category_keyboard(page_id, user_cache)
        )
        await callback.answer("Pilih kategori")

    @dp.callback_query(F.data.startswith("subcat_pick:"))
    async def handle_subcat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        split_data = callback.data.split(":", 3)
        if len(split_data) != 4:
            await callback.answer("Data tidak valid.")
            return
        page_id = split_data[1]
        cat_index = _parse_cb(callback.data, 2)
        subcat_index = _parse_cb(callback.data, 3)
        if cat_index is None or subcat_index is None:
            await callback.answer("Data tidak valid.")
            return

        # Find user by callback.from_user.id
        user_id = callback.from_user.id
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Tidak punya akses.")
            return
        user_notion, user_cache = result

        cats = list(user_cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = user_cache.category_subcategories[cat_name]
        if subcat_index >= len(subcats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        subcat_name = subcats[subcat_index]

        try:
            await user_notion.update_expense_subcategory(page_id, subcat_name, user_cache)
        except Exception as e:
            log.error(f"update_expense_subcategory failed: {e}")
            await callback.answer("❌ Gagal mengubah kategori.")
            return

        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Kategori diubah: *{subcat_name}*",
            parse_mode="Markdown",
            reply_markup=None,
        )
        await callback.answer("Tersimpan!")

    @dp.callback_query(F.data.startswith("cat_cancel:"))
    async def handle_cat_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        # page_id is embedded in callback data but unused
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan")


    @dp.callback_query()
    async def handle_unknown_callback(callback: CallbackQuery) -> None:
        log.warning(f"Unmatched callback: {callback.data!r}")
        await callback.answer("❌ Aksi tidak dikenali.")

    # ── Auto-confirm stale pending expenses ─────────────────────────────

    async def _auto_confirm_stale() -> None:
        """Auto-confirm pending expenses older than 10 minutes."""
        while True:
            await asyncio.sleep(60)
            now = datetime.now().timestamp()
            for user_id, since in list(pending_since.items()):
                if now - since < 600:
                    continue
                # Acquire the per-user lock to prevent race with handle_confirm
                user_lock = saving_locks.setdefault(user_id, asyncio.Lock())
                if user_lock.locked():
                    continue  # confirm in progress — skip
                async with user_lock:
                    # Re-check inside the lock — state may have changed
                    if user_id not in pending_since or now - pending_since[user_id] < 600:
                        continue
                    pending_rec = await db.get_pending_recurring(user_id)
                    entry = await db.get_pending_expense(user_id)
                    if not entry:
                        continue
                    result = await get_user_notion(user_id)
                    if not result:
                        continue
                    n, c = result
                    user_record = await db.get_user(user_id)
                    if not user_record:
                        continue
                    owner = user_record.owner_name
                    try:
                        rec_url = pending_rec["recurring_page_url"] if pending_rec else None
                        # Re-check duplicates — email may have logged it
                        matches = await n.fetch_duplicates(owner, entry.amount, entry.date)
                        is_dup = False
                        if matches:
                            is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date, new_merchant=entry.merchant)
                        if is_dup:
                            pending_since.pop(user_id, None)
                            await db.clear_pending_since(user_id)
                            await db.clear_pending_expense(user_id)
                            if pending_rec:
                                await db.clear_pending_recurring(user_id)
                            await bot.send_message(
                                user_id,
                                "⚠️ *Duplikat terdeteksi!* Transaksi ini sudah tercatat sebelumnya.\n"
                                "Pending otomatis dihapus.",
                                parse_mode="Markdown",
                            )
                            await _prompt_next_debit(user_id)
                            continue
                        url = await n.log_expense(entry, owner, c, recurring_page_url=rec_url)
                        page_id = _url_to_id(url)
                        last_saved_page[user_id] = page_id
                        if pending_rec:
                            await db.mark_processed(pending_rec["uid"], pending_rec["sender"])
                        await db.set_user_undo(user_id, page_id, entry.description, entry.amount, entry.date, entry.subcategory, merchant=entry.merchant)
                        await db.record_pattern(
                            user_id, entry.merchant, entry.subcategory,
                            entry.account, entry.amount, entry.date,
                        )
                        await db.clear_pending_expense(user_id)
                        pending_since.pop(user_id, None)
                        await db.clear_pending_since(user_id)
                        if pending_rec:
                            await db.clear_pending_recurring(user_id)
                        await bot.send_message(
                            user_id,
                            f"⏰ Waktu habis — otomatis tersimpan!\n\n{format_entry(entry, c)}",
                            parse_mode="Markdown",
                            reply_markup=make_undo_keyboard(user_id),
                        )
                        await _prompt_next_debit(user_id)
                    except Exception as e:
                        log.error(f"Auto-confirm failed for user {user_id}: {e}")

    # ── Startup ───────────────────────────────────────────────────────────────
    watch_over_task: asyncio.Task | None = None
    # Look up email owner from DB
    target_owner = config.email_owner
    email_owner_record = None
    if target_owner:
        email_owner_record = await db.get_user_by_name(target_owner)
    if not email_owner_record:
        log.warning("Email watcher: no matching user found for EMAIL_OWNER=%r — notifications disabled", target_owner)
    elif email_owner_record.setup_step != "done":
        log.warning("Email watcher: EMAIL_OWNER user setup incomplete — run /setup first")
        email_owner_record = None

    if email_owner_record:
        email_notion = NotionClient.from_user(email_owner_record)
        email_cache_holder.append(await email_notion.load_cache())

        async def _resolve_email_user(telegram_id: int) -> tuple | None:
            data = await get_user_notion(telegram_id)
            if not data:
                return None
            client, cache = data
            user = await db.get_user(telegram_id)
            return (client, cache, user.owner_name if user else "")

        async def _on_email_saved(user_id: int, url: str, description: str, amount: float, date: str, subcategory: str, merchant: str = "") -> None:
            page_id = _url_to_id(url)
            ts = datetime.now().timestamp()
            email_saved_pages[user_id] = {
                "page_id": page_id,
                "description": description,
                "amount": amount,
                "date": date,
                "subcat": subcategory,
                "merchant": merchant,
                "timestamp": ts,
            }
            await db.set_email_saved_page(user_id, page_id, description, amount, date, subcategory, ts, merchant=merchant)
            last_saved_page[user_id] = page_id
            await db.set_user_undo(user_id, page_id, description, amount, date, subcategory, merchant=merchant)

        email_watcher = EmailWatcher(
            config=config,
            db=db,
            notion=email_notion,
            agent=agent,
            cache_getter=lambda: email_cache_holder[0],
            bot=bot,
            email_owner_id=email_owner_record.telegram_id,
            email_owner_name=email_owner_record.owner_name,
            user_data_fn=_resolve_email_user,
            on_save_fn=_on_email_saved,
            pending_since=pending_since,
            alert_fn=alert_owner,
        )
        watcher_holder.append(email_watcher)
        watcher_task_ref = asyncio.create_task(email_watcher.run())
        log.info("Email watcher scheduled.")

        async def _watch_over() -> None:
            """Supervise the email watcher, restarting on crash."""
            nonlocal watcher_task_ref
            while True:
                task = watcher_task_ref
                if task is None:
                    break
                try:
                    await task
                except asyncio.CancelledError:
                    break  # clean shutdown
                except Exception:
                    log.critical("Email watcher died, restarting in 10s...", exc_info=True)
                    await asyncio.sleep(10)
                    watcher_task_ref = asyncio.create_task(email_watcher.run())

        watch_over_task = asyncio.create_task(_watch_over())

    log.info("Bot starting...")
    auto_confirm_task = asyncio.create_task(_auto_confirm_stale())
    try:
        await dp.start_polling(bot)
    finally:
        # Cancel background tasks before closing DB
        if watcher_task_ref:
            watcher_task_ref.cancel()
        if auto_confirm_task:
            auto_confirm_task.cancel()
        if watch_over_task:
            watch_over_task.cancel()
        # Allow cancelled tasks to process their CancelledError
        await asyncio.gather(
            *(t for t in [watcher_task_ref, auto_confirm_task, watch_over_task] if t),
            return_exceptions=True,
        )
        await db.close()
        log.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
