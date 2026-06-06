import asyncio
import logging
from datetime import date, datetime

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
    make_change_category_button,
    make_recommended_category_keyboard,
    make_edit_field_keyboard,
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

    notion = NotionClient(config)
    agent = Agent(config)

    cache: NotionCache = NotionCache()
    last_desc: dict[int, str] = {}
    rec_markup: dict[str, InlineKeyboardMarkup] = {}
    pending_edit: dict[int, str] = {}
    page_desc: dict[str, str] = {}
    photo_queue: dict[int, list[str]] = {}
    processing_group: set[int] = set()

    all_user_ids: list[int] = list(config.users.keys())

    async def refresh_cache() -> None:
        nonlocal cache
        log.info("Refreshing Notion cache...")
        cache = await notion.load_cache()
        log.info(
            f"Cache loaded: {len(cache.subcategories)} subcategories, "
            f"{len(cache.accounts)} accounts, "
            f"{len(cache.recurring_payments)} recurring payments"
        )

    def get_owner(user_id: int) -> str | None:
        return config.users.get(user_id)

    def format_entry(entry: ExpenseEntry) -> str:
        sub_match = cache.closest_subcategory(entry.subcategory)
        acc_match = cache.closest_account(entry.account)
        sub_label = sub_match[0] if sub_match else f"❓ {entry.subcategory}"
        acc_label = acc_match[0] if acc_match else f"❓ {entry.account}"
        return (
            f"📝 *Deskripsi:* {entry.description}\n"
            f"💰 *Jumlah:* Rp {entry.amount:,.0f}\n"
            f"📅 *Tanggal:* {entry.date}\n"
            f"🏷 *Kategori:* {sub_label}\n"
            f"🏦 *Akun:* {acc_label}"
        )

    def format_income_entry(entry: IncomeEntry) -> str:
        sub_match = cache.closest_income_subcategory(entry.subcategory)
        acc_match = cache.closest_account(entry.account)
        sub_label = sub_match[0] if sub_match else f"❓ {entry.subcategory}"
        acc_label = acc_match[0] if acc_match else f"❓ {entry.account}"
        return (
            f"📝 *Deskripsi:* {entry.description}\n"
            f"💵 *Jumlah:* Rp {entry.amount:,.0f}\n"
            f"📅 *Tanggal:* {entry.date}\n"
            f"🏷 *Kategori:* {sub_label}\n"
            f"🏦 *Akun:* {acc_label}"
        )

    async def alert_owner(text: str) -> None:
        for uid in all_user_ids:
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
            except Exception as e:
                log.error(f"Failed to send alert to {uid}: {e}")

    async def _process_next_photo(user_id: int, owner: str) -> None:
        q = photo_queue.get(user_id, [])
        if not q:
            processing_group.discard(user_id)
            return
        file_id = q.pop(0)
        chat_id = user_id
        status_msg = await bot.send_message(chat_id, "🔍 Membaca struk berikutnya...")
        try:
            file = await bot.get_file(file_id)
            image_bytes = await bot.download_file(file.file_path)
            today = date.today().isoformat()
            entry = await agent.extract_from_image(image_bytes.read(), cache, today)
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
            matches = await notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
                if is_dup:
                    dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
        except Exception as e:
            log.warning(f"Duplicate check failed: {e}")
        await bot.send_message(
            chat_id,
            f"{confidence_emoji} Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    # ── Handlers ──────────────────────────────────────────────────────────────

    @dp.message(CommandStart())
    async def handle_start(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Maaf, kamu tidak punya akses ke bot ini.")
            return
        await msg.answer(
            f"Halo {owner}\\! 👋\n\n"
            "Kirim ke saya:\n"
            "📸 *Foto struk* → otomatis ekstrak & catat\n"
            "💬 *Teks* kayak `Nasi goreng 25k cash` → langsung dicatat\n"
            "❓ *Pertanyaan* kayak `Berapa pengeluaran bulan ini?` → dijawab\n"
            "💰 *Pemasukan* kayak `Gaji bulanan masuk 3 juta` → dicatat\n\n"
            "Ketik /help untuk bantuan lengkap\\.",
            parse_mode="MarkdownV2",
        )

    @dp.message(Command("help"))
    async def handle_help(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
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
            "/networth — lihat ringkasan aset\n"
            "/budget — cek status anggaran bulanan\n"
            "/search <kata kunci> — cari pengeluaran\n"
            "/stats — ringkasan pengeluaran bulan ini\n"
            "/refresh — muat ulang data kategori dari Notion\n"
            "/help — tampilkan pesan ini",
            parse_mode="Markdown",
        )

    @dp.message(Command("networth"))
    async def handle_networth(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        try:
            assets = await notion.fetch_assets()
            if not assets:
                await msg.answer("Belum ada aset. Tambahkan dulu di database Assets Notion.")
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
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        try:
            budgets = await notion.fetch_budgets(cache)
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

    @dp.message(Command("refresh"))
    async def handle_refresh(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        await msg.answer("🔄 Memuat ulang cache...")
        try:
            await refresh_cache()
            await msg.answer(
                f"✅ Selesai! {len(cache.subcategories)} subkategori pengeluaran, "
                f"{len(cache.income_subcategories)} subkategori pemasukan, "
                f"{len(cache.accounts)} akun, "
                f"{len(cache.recurring_payments)} pembayaran rutin."
            )
        except Exception as e:
            log.error(f"/refresh failed: {e}")
            await msg.answer(f"❌ Gagal refresh: `{type(e).__name__}: {e}`", parse_mode="Markdown")

    @dp.message(Command("search"))
    async def handle_search(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await msg.answer("🔍 *Cari pengeluaran*\n\nGunakan: `/search kata kunci`\nContoh: `/search indomie`", parse_mode="Markdown")
            return
        keyword = parts[1].strip()
        await msg.answer(f"🔍 Mencari \"{keyword}\"...")
        try:
            results = await notion.search_expenses(owner, keyword)
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
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        await msg.answer("📊 Mengambil data pengeluaran...")
        try:
            expenses = await notion.fetch_expenses(owner, cache)
        except Exception as e:
            log.error(f"/stats failed: {e}")
            await msg.answer(f"❌ Gagal ambil data.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")
            return
        now = datetime.now()
        month_str = now.strftime("%Y-%m")
        month_expenses = [e for e in expenses if e["date"].startswith(month_str)]
        if not month_expenses:
            await msg.answer(f"📊 *Ringkasan Bulan Ini*\n\nBelum ada pengeluaran untuk bulan ini.", parse_mode="Markdown")
            return
        total = sum(e["amount"] for e in month_expenses)
        count = len(month_expenses)
        avg = total / count
        cat_totals: dict[str, float] = {}
        for e in month_expenses:
            sub = e.get("subcategory", "-")
            cat_totals[sub] = cat_totals.get(sub, 0) + e["amount"]
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
        lines = [f"📊 *Ringkasan Bulan Ini* ({month_str})\n"]
        lines.append(f"💰 *Total:* Rp {total:,.0f}")
        lines.append(f"📋 *Jumlah:* {count} transaksi")
        lines.append(f"📊 *Rata-rata:* Rp {avg:,.0f}/transaksi\n")
        lines.append("*Per Kategori:*")
        for cat, amt in sorted_cats[:5]:
            pct = amt / total * 100
            lines.append(f"  • {cat}: Rp {amt:,.0f} ({pct:.0f}%)")
        if len(sorted_cats) > 5:
            others = sum(v for _, v in sorted_cats[5:])
            lines.append(f"  • Lainnya: Rp {others:,.0f}")
        await msg.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(F.photo)
    async def handle_photo(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Maaf, kamu tidak punya akses.")
            return

        user_id = msg.from_user.id

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
            entry = await agent.extract_from_image(image_bytes.read(), cache, today)
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
            matches = await notion.fetch_duplicates(owner, entry.amount, entry.date)
            if matches:
                is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
                if is_dup:
                    dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
        except Exception as e:
            log.warning(f"Duplicate check failed: {e}")

        await msg.answer(
            f"{confidence_emoji} Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(user_id),
        )

    @dp.message(F.text)
    async def handle_text(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Maaf, kamu tidak punya akses.")
            return

        text = msg.text.strip()
        user_id = msg.from_user.id

        # ── Pending edit (check before Jago to avoid race) ──────────────────────
        edit_field = pending_edit.pop(user_id, None)
        if edit_field and text.lower().strip() in ("batal", "cancel", "/cancel"):
            entry = await db.get_pending_expense(user_id)
            if entry:
                await msg.answer(
                    f"Oke! Konfirmasi:\n\n{format_entry(entry)}",
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
                f"✅ Diubah! Konfirmasi:\n\n{format_entry(entry)}",
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
                f"Oke! Konfirmasi:\n\n{format_entry(entry)}",
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
                    notion.fetch_expenses(owner),
                    notion.fetch_assets(),
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
                entry = await agent.extract_from_text(text, cache, today)
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
                matches = await notion.fetch_duplicates(owner, entry.amount, entry.date)
                if matches:
                    is_dup = await agent.check_duplicate(matches, entry.description, entry.amount, entry.date)
                    if is_dup:
                        dup_warning = "\n\n⚠️ *Duplikat terdeteksi!* Transaksi serupa sudah tercatat sebelumnya."
            except Exception as e:
                log.warning(f"Duplicate check failed: {e}")
            await msg.answer(
                f"Oke! Konfirmasi:{dup_warning}\n\n{format_entry(entry)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )

        elif intent.type == "log_income":
            today = date.today().isoformat()
            try:
                income = await agent.extract_income_from_text(text, cache, today)
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
                f"💰 Pemasukan! Konfirmasi:\n\n{format_income_entry(income)}",
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
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Tidak punya akses.")
            return

        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("Tidak ada pengeluaran pending.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Menyimpan...")

        status_msg = await callback.message.answer("⏳ Menyimpan ke Notion...")
        try:
            url = await notion.log_expense(entry, owner, cache)
            last_desc[user_id] = entry.description
            await db.clear_pending_expense(user_id)
            page_id = _url_to_id(url)
            page_desc[page_id] = entry.description
            await status_msg.edit_text(
                f"✅ Tersimpan! [Lihat di Notion]({url})",
                parse_mode="Markdown",
                reply_markup=make_change_category_button(page_id),
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
        await callback.answer()
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
        await callback.answer()
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
        await callback.answer()
        await callback.message.answer("✏️ Ketik tanggal baru (YYYY-MM-DD):")

    @dp.callback_query(F.data.startswith("edit_cat:"))
    async def handle_edit_cat(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        entry = await db.get_pending_expense(user_id)
        cats = list(cache.category_subcategories.keys())
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
        cats = list(cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = cache.category_subcategories[cat_name]
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
        entry = await db.get_pending_expense(user_id)
        cats = list(cache.category_subcategories.keys())
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
        cats = list(cache.category_subcategories.keys())
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
        cats = list(cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = cache.category_subcategories[cat_name]
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
            f"✅ Kategori diubah ke *{subcat_name}*\n\n{format_entry(entry)}",
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
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        await callback.message.answer(
            f"Oke! Konfirmasi:\n\n{format_entry(entry)}",
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
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Tidak punya akses.")
            return

        await db.clear_pending_expense(user_id)
        await db.clear_pending_email_expense(user_id)
        await db.clear_debit_queue(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("Dibatalkan ❌")
        asyncio.ensure_future(_process_next_photo(user_id, owner))

    @dp.callback_query(F.data.startswith("income_confirm:"))
    async def handle_income_confirm(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Tidak punya akses.")
            return
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Tidak punya akses.")
            return

        income = await db.get_pending_income(user_id)
        if not income:
            await callback.answer("Tidak ada pemasukan pending.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Menyimpan pemasukan...")

        status_msg = await callback.message.answer("⏳ Menyimpan pemasukan ke Notion...")
        try:
            url = await notion.log_income(income, owner, cache)
            await db.clear_pending_income(user_id)
            await status_msg.edit_text(
                f"✅ Pemasukan tersimpan! [Lihat di Notion]({url})", parse_mode="Markdown"
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
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Tidak punya akses.")
            return

        await db.clear_pending_income(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Dibatalkan.")
        await callback.message.answer("Dibatalkan ❌")

    @dp.callback_query(F.data.startswith("cat_pick:"))
    async def handle_cat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id, cat_idx_str = callback.data.split(":", 2)
        cat_index = int(cat_idx_str)
        await callback.message.edit_reply_markup(
            reply_markup=make_subcategory_keyboard(page_id, cat_index, cache)
        )
        await callback.answer("Pilih subkategori")

    @dp.callback_query(F.data.startswith("cat_back:"))
    async def handle_cat_back(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id = callback.data.split(":", 1)
        markup = rec_markup.get(page_id) or make_category_keyboard(page_id, cache)
        await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Kembali ke daftar kategori")

    @dp.callback_query(F.data.startswith("cat_all:"))
    async def handle_cat_all(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id = callback.data.split(":", 1)
        markup = make_category_keyboard(page_id, cache)
        rec_markup[page_id] = markup
        await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Pilih kategori")

    @dp.callback_query(F.data.startswith("cat_change:"))
    async def handle_cat_change(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        _, page_id = callback.data.split(":", 1)
        user_id = callback.from_user.id
        desc = last_desc.get(user_id) or page_desc.get(page_id, "")
        all_cats = list(cache.category_subcategories.keys())
        markup: InlineKeyboardMarkup | None = None
        if desc:
            recommended = await agent.suggest_categories(desc, all_cats)
            markup = make_recommended_category_keyboard(page_id, recommended, cache)
        if not markup:
            markup = make_category_keyboard(page_id, cache)
        rec_markup[page_id] = markup
        await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Pilih kategori")

    @dp.callback_query(F.data.startswith("subcat_pick:"))
    async def handle_subcat_pick(callback: CallbackQuery) -> None:
        log.debug(f"Callback received: {callback.data}")
        if not get_owner(callback.from_user.id):
            await callback.answer("Tidak punya akses.")
            return
        _, page_id, cat_idx_str, subcat_idx_str = callback.data.split(":", 3)
        cat_index = int(cat_idx_str)
        subcat_index = int(subcat_idx_str)

        cats = list(cache.category_subcategories.keys())
        if cat_index >= len(cats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        cat_name = cats[cat_index]
        subcats = cache.category_subcategories[cat_name]
        if subcat_index >= len(subcats):
            await callback.answer("❌ Data tidak ditemukan.")
            return
        subcat_name = subcats[subcat_index]

        try:
            await notion.update_expense_subcategory(page_id, subcat_name, cache)
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
    try:
        await refresh_cache()
    except Exception as e:
        log.error(f"Initial cache load failed: {e}")

    target_owner = config.email_owner
    afif_id = next(
        (uid for uid, name in config.users.items() if name == target_owner), None
    ) if target_owner else next(iter(config.users), None)
    afif_name = config.users.get(afif_id, target_owner or "Unknown") if afif_id else (target_owner or "Unknown")
    if afif_id is None:
        log.warning("Email watcher: no matching user found for EMAIL_OWNER=%r — notifications disabled", target_owner)

    email_watcher = EmailWatcher(
        config=config,
        db=db,
        notion=notion,
        agent=agent,
        cache_getter=lambda: cache,
        bot=bot,
        owner_telegram_id=afif_id,
        owner_name=afif_name,
        alert_fn=alert_owner,
        page_desc=page_desc,
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
        await notion.aclose()
        log.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
