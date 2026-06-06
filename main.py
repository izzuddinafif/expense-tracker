import asyncio
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
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
)
from models import NotionCache, ExpenseEntry, IncomeEntry, EmailTransaction
from notion import NotionClient, _url_to_id
from agent import Agent
from email_watcher import EmailWatcher


logging.basicConfig(level=logging.INFO)
logging.getLogger("__main__").setLevel(logging.DEBUG)
log = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()
    bot = Bot(token=config.telegram_token)
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
    last_saved_page: dict[int, str] = {}  # user_id → notion page_id (for undo)

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
        q = photo_queue.get(user_id, [])
        if not q:
            processing_group.discard(user_id)
            return
        file_id = q.pop(0)
        chat_id = user_id
        status_msg = await bot.send_message(chat_id, "🔍 Membaca struk berikutnya...")
        
        # Get per-user Notion client and cache
        result = await get_user_notion(user_id)
        if not result:
            await status_msg.delete()
            await bot.send_message(chat_id, "Ketik /setup untuk menghubungkan Notion workspace kamu.")
            return
        user_notion, user_cache = result
        
        try:
            file = await bot.get_file(file_id)
            image_bytes = await bot.download_file(file.file_path)
            today = date.today().isoformat()
            entry = await agent.extract_from_image(image_bytes.read(), user_cache, today)
        except Exception as e:
            log.error(f"Next photo failed: {e}")
            await status_msg.delete()
            await bot.send_message(chat_id, f"❌ Gagal baca struk: `{type(e).__name__}`", parse_mode="Markdown")
            await _process_next_photo(user_id, owner)
            return
        await db.set_pending_expense(user_id, entry)
        confidence_emoji = "✅" if entry.confidence >= 0.8 else "⚠️"
        await status_msg.delete()
        dup_warning = ""
        try:
            matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
                if is_dup:
                    dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
        except Exception as e:
            log.warning(f"Duplicate check failed: {e}")
        await bot.send_message(
            chat_id,
            f"{confidence_emoji} Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

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
            "/refresh — muat ulang data kategori dari Notion\n"
            "/linkemail — hubungkan akun bank ke email watcher\n"
            "/help — tampilkan pesan ini",
            parse_mode="Markdown",
        )

    @dp.message(Command("setup"))
    async def handle_setup(msg: Message) -> None:
        await run_setup(msg)

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

        elif cmd == "remove" and len(parts) >= 3:
            pattern = parts[2].strip()
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
        lines.append(f"\n[Lihat semua di Notion](https://www.notion.so/search?q={keyword})")
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

        try:
            entry = await agent.extract_from_image(image_bytes.read(), user_cache, today)
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
        confidence_emoji = "✅" if entry.confidence >= 0.8 else "⚠️"
        await status_msg.delete()

        dup_warning = ""
        try:
            matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
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
                entry.description = text
            elif edit_field == "amount":
                try:
                    entry.amount = float(text.replace(",", "").replace("Rp", "").replace("rp", "").strip())
                except ValueError:
                    pending_edit[user_id] = "amount"
                    await msg.answer("❌ Angka tidak valid. Ketik jumlah angka saja (contoh: 25000):")
                    return
            elif edit_field == "date":
                entry.date = text
            await db.set_pending_expense(user_id, entry)
            await msg.answer(
                f"✅ Diubah! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )
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
            entry = ExpenseEntry(
                description=text,
                amount=pending_tx.amount,
                date=pending_tx.date,
                subcategory=pending_tx.subcategory,
                account=pending_tx.account,
                confidence=0.9,
            )
            await db.set_pending_expense(user_id, entry)
            await msg.answer(
                f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )
            next_tx = await db.pop_debit(user_id)
            if next_tx:
                await db.set_pending_email_expense(user_id, next_tx)
                await msg.answer(
                    f"💳 *Kartu debit Jago* — Rp {next_tx.amount:,.0f}\n"
                    f"📅 {next_tx.date}  🏦 {next_tx.account}\n\n"
                    f"Beli apa? Balas dengan nama merchant atau deskripsi."
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
            try:
                entry = await agent.extract_from_text(text, user_cache, today)
            except Exception as e:
                log.error(f"extract_from_text failed: {e}")
                await msg.answer(
                    "❌ Gagal baca pengeluaran. Contoh: 'Nasi goreng 25k cash'\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )
                return

            await db.set_pending_expense(user_id, entry)
            dup_warning = ""
            try:
                matches = await user_notion.fetch_duplicates(owner, entry.amount, entry.date)
                if matches:
                    is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
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
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

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
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Menyimpan...")

        status_msg = await callback.message.answer("⏳ Menyimpan ke Notion...")
        try:
            # Check for recurring expense link before logging
            pending_rec = await db.get_pending_recurring(user_id)
            rec_page_url = pending_rec["recurring_page_url"] if pending_rec else None

            url = await user_notion.log_expense(entry, owner, user_cache, recurring_page_url=rec_page_url)
            await db.clear_pending_expense(user_id)
            if pending_rec:
                await db.clear_pending_recurring(user_id)
            page_id = _url_to_id(url)
            last_saved_page[user_id] = page_id
            await status_msg.edit_text(
                f"✅ Tersimpan! [Lihat di Notion]({url})",
                parse_mode="Markdown",
                reply_markup=make_undo_keyboard(user_id),
            )
            asyncio.ensure_future(_process_next_photo(user_id, owner))
        except Exception as e:
            log.error(f"Notion write failed: {e}")
            await status_msg.edit_text(
                f"❌ Gagal simpan ke Notion.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("edit:"))
    async def handle_edit(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        await callback.message.edit_reply_markup(
            reply_markup=make_edit_field_keyboard(user_id)
        )
        await callback.answer("Pilih field yang diedit")

    @dp.callback_query(F.data.startswith("edit_desc:"))
    async def handle_edit_desc(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "desc"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik deskripsi baru")
        await callback.message.answer("✏️ Ketik deskripsi baru:")

    @dp.callback_query(F.data.startswith("edit_amount:"))
    async def handle_edit_amount(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "amount"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik jumlah baru")
        await callback.message.answer("✏️ Ketik jumlah baru (contoh: 25000):")

    @dp.callback_query(F.data.startswith("edit_date:"))
    async def handle_edit_date(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        pending_edit[user_id] = "date"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ketik tanggal baru")
        await callback.message.answer("✏️ Ketik tanggal baru (YYYY-MM-DD):")

    @dp.callback_query(F.data.startswith("edit_cat:"))
    async def handle_edit_cat(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        entry = await db.get_pending_expense(user_id)
        cats = list(user_cache.category_subcategories.keys())
        markup: InlineKeyboardMarkup | None = None
        if entry:
            recommended = await agent.suggest_categories(entry.description, cats)
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

    @dp.callback_query(F.data.startswith("edit_cat_pick:"))
    async def handle_edit_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, user_id_str, cat_idx_str = callback.data.split(":", 2)
        user_id = int(user_id_str)
        cat_index = int(cat_idx_str)
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
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        _, user_cache = result
        entry = await db.get_pending_expense(user_id)
        cats = list(user_cache.category_subcategories.keys())
        markup: InlineKeyboardMarkup | None = None
        if entry:
            recommended = await agent.suggest_categories(entry.description, cats)
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
        await callback.answer("Kembali")

    @dp.callback_query(F.data.startswith("edit_cat_all:"))
    async def handle_edit_cat_all(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
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
        _, user_id_str, cat_idx_str, subcat_idx_str = callback.data.split(":", 3)
        user_id = int(user_id_str)
        cat_index = int(cat_idx_str)
        subcat_index = int(subcat_idx_str)
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
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
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
        await callback.message.answer(
            f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.callback_query(F.data.startswith("edit_cancel:"))
    async def handle_edit_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
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
        await callback.message.answer(
            f"Oke! Konfirmasi:\n\n{format_entry(entry, user_cache)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.callback_query(F.data.startswith("cancel:"))
    async def handle_cancel(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        user = await db.get_user(user_id)
        if not user or user.setup_step != "done":
            await callback.answer("Tidak punya akses.")
            return

        await db.clear_pending_expense(user_id)
        await db.clear_pending_email_expense(user_id)
        await db.clear_debit_queue(user_id)
        await db.clear_pending_recurring(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("Dibatalkan ❌")
        asyncio.ensure_future(_process_next_photo(user_id, user.owner_name))

    @dp.callback_query(F.data.startswith("income_confirm:"))
    async def handle_income_confirm(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
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
        await callback.answer("Menyimpan pemasukan...")

        status_msg = await callback.message.answer("⏳ Menyimpan pemasukan ke Notion...")
        try:
            url = await user_notion.log_income(income, owner, user_cache)
            await db.clear_pending_income(user_id)
            page_id = _url_to_id(url)
            last_saved_page[user_id] = page_id
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
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
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

    # ── Undo ─────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("undo:"))
    async def handle_undo(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return

        page_id = last_saved_page.pop(user_id, None)
        if not page_id:
            await callback.answer("Tidak ada yang bisa di-undo.")
            return

        result = await get_user_notion(user_id)
        if not result:
            await callback.answer("Ketik /setup untuk menghubungkan Notion.")
            return
        user_notion, _ = result

        try:
            await user_notion.archive_page(page_id)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Dibatalkan!")
            await callback.message.answer("✅ Dibatalkan — entri telah dihapus dari Notion.")
        except Exception as e:
            log.error(f"Undo failed for user {user_id}: {e}")
            await callback.answer("❌ Gagal undo.")
            await callback.message.answer(
                f"❌ Gagal membatalkan.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("cat_pick:"))
    async def handle_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = callback.from_user.id
        _, page_id, cat_idx_str = callback.data.split(":", 2)
        cat_index = int(cat_idx_str)
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
        _, page_id, cat_idx_str, subcat_idx_str = callback.data.split(":", 3)
        cat_index = int(cat_idx_str)
        subcat_index = int(subcat_idx_str)

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
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan")

    @dp.callback_query()
    async def handle_unknown_callback(callback: CallbackQuery) -> None:
        log.warning(f"Unmatched callback: {callback.data!r}")
        await callback.answer("❌ Aksi tidak dikenali.")

    # ── Startup ───────────────────────────────────────────────────────────────
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
            alert_fn=alert_owner,
        )
        _watcher_task = asyncio.create_task(email_watcher.run())
        log.info("Email watcher scheduled.")

        async def _watch_over(task: asyncio.Task) -> None:
            nonlocal _watcher_task
            try:
                await task
            except Exception:
                log.critical("Email watcher died, restarting in 10s...", exc_info=True)
                await asyncio.sleep(10)
                _watcher_task = asyncio.create_task(email_watcher.run())
                _watcher_task.add_done_callback(
                    lambda t: asyncio.ensure_future(_watch_over(t))
                )

        _watcher_task.add_done_callback(
            lambda t: asyncio.ensure_future(_watch_over(t))
        )

    log.info("Bot starting...")
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        log.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
