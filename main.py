import asyncio
import logging
from datetime import date

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
from models import NotionCache, ExpenseEntry, IncomeEntry, EmailTransaction
from notion import NotionClient
from agent import Agent
from email_watcher import EmailWatcher


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def make_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Log it", callback_data=f"confirm:{user_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{user_id}"),
    ]])


def make_income_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Log income", callback_data=f"income_confirm:{user_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"income_cancel:{user_id}"),
    ]])


async def main() -> None:
    config = load_config()
    bot = Bot(token=config.telegram_token)
    dp = Dispatcher()

    db = await Database.connect(config.db_path)
    log.info(f"Database connected: {config.db_path}")

    notion = NotionClient(config)
    agent = Agent(config)

    cache: NotionCache = NotionCache()

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
            f"📝 *{entry.description}*\n"
            f"💰 Rp {entry.amount:,.0f}\n"
            f"📅 {entry.date}\n"
            f"🏷 {sub_label}\n"
            f"🏦 {acc_label}"
        )

    def format_income_entry(entry: IncomeEntry) -> str:
        sub_match = cache.closest_income_subcategory(entry.subcategory)
        acc_match = cache.closest_account(entry.account)
        sub_label = sub_match[0] if sub_match else f"❓ {entry.subcategory}"
        acc_label = acc_match[0] if acc_match else f"❓ {entry.account}"
        return (
            f"📝 *{entry.description}*\n"
            f"💵 Rp {entry.amount:,.0f}\n"
            f"📅 {entry.date}\n"
            f"🏷 {sub_label}\n"
            f"🏦 {acc_label}"
        )

    async def alert_owner(text: str) -> None:
        for uid in all_user_ids:
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
            except Exception as e:
                log.error(f"Failed to send alert to {uid}: {e}")

    # ── Handlers ──────────────────────────────────────────────────────────────

    @dp.message(CommandStart())
    async def handle_start(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Sorry, you're not authorized to use this bot.")
            return
        await msg.answer(
            f"Hey {owner}! 👋\n\n"
            "Send me:\n"
            "📸 A photo of a receipt → I'll log it\n"
            "💬 A text like 'Nasi goreng 25k cash' → I'll log it\n"
            "❓ A question like 'How much did I spend this month?' → I'll answer\n\n"
            "/help — show what I can do\n"
            "/refresh — reload categories from Notion"
        )

    @dp.message(Command("help"))
    async def handle_help(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        await msg.answer(
            "Here's what I can do:\n\n"
            "📸 *Receipt photo* — send a photo of a receipt and I'll extract and log the expense\n"
            "💬 *Text expense* — describe it naturally: `Nasi goreng 25k jago`\n"
            "💰 *Log income* — report money received: `Gaji bulanan masuk 3 juta ke Mandiri`\n"
            "❓ *Spending query* — ask anything: `How much did I spend this week?`\n\n"
            "Commands:\n"
            "/networth — show your assets summary\n"
            "/refresh — reload categories and recurring payments from Notion\n"
            "/help — show this message",
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
                await msg.answer("No assets found. Add them to the Assets database in Notion.")
                return
            total = sum(a["value_idr"] for a in assets if a["value_idr"])
            lines = ["💼 *Assets*\n"]
            for a in assets:
                val = f"Rp {a['value_idr']:,.0f}" if a["value_idr"] else "_(no value set)_"
                qty = f"{a['quantity']:g} {a['unit']}" if a["quantity"] else ""
                lines.append(f"• *{a['name']}*{' — ' + qty if qty else ''}: {val}")
            if total:
                lines.append(f"\n💰 *Total: Rp {total:,.0f}*")
            await msg.answer("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            log.error(f"/networth failed: {e}")
            await msg.answer(f"❌ Couldn't fetch assets.\n`{type(e).__name__}: {str(e)[:80]}`", parse_mode="Markdown")

    @dp.message(Command("refresh"))
    async def handle_refresh(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        await msg.answer("Refreshing cache...")
        try:
            await refresh_cache()
            await msg.answer(
                f"✅ Done! {len(cache.subcategories)} expense subcategories, "
                f"{len(cache.income_subcategories)} income subcategories, "
                f"{len(cache.accounts)} accounts, "
                f"{len(cache.recurring_payments)} recurring payments loaded."
            )
        except Exception as e:
            log.error(f"/refresh failed: {e}")
            await msg.answer(f"❌ Refresh failed: `{type(e).__name__}: {e}`", parse_mode="Markdown")

    @dp.message(F.photo)
    async def handle_photo(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Not authorized.")
            return

        await msg.answer("🔍 Reading receipt...")

        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        today = date.today().isoformat()

        try:
            entry = await agent.extract_from_image(image_bytes.read(), cache, today)
        except Exception as e:
            log.error(f"extract_from_image failed: {e}")
            await msg.answer(
                "❌ Couldn't read the receipt. Try a clearer photo.\n"
                f"`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )
            return

        await db.set_pending_expense(msg.from_user.id, entry)
        confidence_emoji = "✅" if entry.confidence >= 0.8 else "⚠️"

        await msg.answer(
            f"{confidence_emoji} Got it! Confirm:\n\n{format_entry(entry)}",
            parse_mode="Markdown",
            reply_markup=make_confirm_keyboard(msg.from_user.id),
        )

    @dp.message(F.text)
    async def handle_text(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            await msg.answer("Not authorized.")
            return

        text = msg.text.strip()
        user_id = msg.from_user.id

        # ── Jago debit card follow-up ──────────────────────────────────────────
        pending_tx = await db.get_pending_email_expense(user_id)
        if pending_tx:
            if text.lower().strip() in ("batal", "cancel", "skip", "/cancel"):
                await db.clear_pending_email_expense(user_id)
                await db.clear_debit_queue(user_id)
                await msg.answer("Debit card follow-up cancelled ❌")
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
                f"Got it! Confirm:\n\n{format_entry(entry)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(user_id),
            )
            # Promote next queued debit card tx (if any)
            next_tx = await db.pop_debit(user_id)
            if next_tx:
                await db.set_pending_email_expense(user_id, next_tx)
                await msg.answer(
                    f"💳 *Jago debit card* — Rp {next_tx.amount:,.0f}\n"
                    f"📅 {next_tx.date}  🏦 {next_tx.account}\n\n"
                    f"Beli apa? Balas dengan nama merchant/deskripsi."
                )
            return

        # ── Intent detection ──────────────────────────────────────────────────
        try:
            intent = await agent.detect_intent(text)
        except Exception as e:
            log.error(f"detect_intent failed: {e}")
            await msg.answer(
                "❌ Couldn't understand that right now. Please try again.\n"
                f"`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )
            return

        if intent.type == "query":
            await msg.answer("🔎 Fetching your expenses...")
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
                    f"❌ Couldn't fetch your expenses right now.\n"
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
                    "❌ Couldn't parse that. Try: 'Nasi goreng 25k cash'\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )
                return

            await db.set_pending_expense(user_id, entry)
            await msg.answer(
                f"Got it! Confirm:\n\n{format_entry(entry)}",
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
                    "❌ Couldn't parse that income. Try: 'Gaji bulanan masuk 3 juta ke Jago'\n"
                    f"`{type(e).__name__}: {str(e)[:80]}`",
                    parse_mode="Markdown",
                )
                return

            await db.set_pending_income(user_id, income)
            await msg.answer(
                f"💰 Income received! Confirm:\n\n{format_income_entry(income)}",
                parse_mode="Markdown",
                reply_markup=make_income_confirm_keyboard(user_id),
            )

        else:
            await msg.answer(
                "Hmm, not sure what you mean. Send a receipt photo or describe an expense."
            )

    # ── Inline keyboard callbacks ─────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("confirm:"))
    async def handle_confirm(callback: CallbackQuery) -> None:
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Not authorized.")
            return
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Not authorized.")
            return

        entry = await db.get_pending_expense(user_id)
        if not entry:
            await callback.answer("No pending expense.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Logging...")

        status_msg = await callback.message.answer("⏳ Logging to Notion...")
        try:
            url = await notion.log_expense(entry, owner, cache)
            await db.clear_pending_expense(user_id)
            await status_msg.edit_text(
                f"✅ Logged! [View in Notion]({url})", parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Notion write failed: {e}")
            await status_msg.edit_text(
                f"❌ Failed to log to Notion.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("cancel:"))
    async def handle_cancel(callback: CallbackQuery) -> None:
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Not authorized.")
            return
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Not authorized.")
            return

        await db.clear_pending_expense(user_id)
        await db.clear_pending_email_expense(user_id)
        await db.clear_debit_queue(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Cancelled.")
        await callback.message.answer("Cancelled ❌")

    @dp.callback_query(F.data.startswith("income_confirm:"))
    async def handle_income_confirm(callback: CallbackQuery) -> None:
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Not authorized.")
            return
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Not authorized.")
            return

        income = await db.get_pending_income(user_id)
        if not income:
            await callback.answer("No pending income.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Logging income...")

        status_msg = await callback.message.answer("⏳ Logging income to Notion...")
        try:
            url = await notion.log_income(income, owner, cache)
            await db.clear_pending_income(user_id)
            await status_msg.edit_text(
                f"✅ Income logged! [View in Notion]({url})", parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Notion income write failed: {e}")
            await status_msg.edit_text(
                f"❌ Failed to log income to Notion.\n`{type(e).__name__}: {str(e)[:80]}`",
                parse_mode="Markdown",
            )

    @dp.callback_query(F.data.startswith("income_cancel:"))
    async def handle_income_cancel(callback: CallbackQuery) -> None:
        user_id = int(callback.data.split(":")[1])
        if callback.from_user.id != user_id:
            await callback.answer("Not authorized.")
            return
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Not authorized.")
            return

        await db.clear_pending_income(user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Cancelled.")
        await callback.message.answer("Cancelled ❌")

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
    )
    _watcher_task = asyncio.create_task(email_watcher.run())
    log.info("Email watcher scheduled.")

    async def _watch_over(task: asyncio.Task) -> None:
        try:
            await task
        except Exception:
            log.critical("Email watcher task died!", exc_info=True)

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
