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
from models import NotionCache, ConversationState, ExpenseEntry
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


async def main() -> None:
    config = load_config()
    bot = Bot(token=config.telegram_token)
    dp = Dispatcher()

    notion = NotionClient(config)
    agent = Agent(config)

    # in-memory state
    cache: NotionCache = NotionCache()
    conversations: dict[int, ConversationState] = {}

    # All configured user IDs (for broadcasting critical errors)
    all_user_ids: list[int] = list(config.users.keys())

    async def get_state(user_id: int) -> ConversationState:
        if user_id not in conversations:
            conversations[user_id] = ConversationState()
        return conversations[user_id]

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

    async def alert_owner(text: str) -> None:
        """Send a system alert to all configured users."""
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
            "/refresh — reload categories from Notion"
        )

    @dp.message(Command("refresh"))
    async def handle_refresh(msg: Message) -> None:
        owner = get_owner(msg.from_user.id)
        if not owner:
            return
        await msg.answer("Refreshing cache...")
        try:
            await refresh_cache()
            await msg.answer(
                f"✅ Done! {len(cache.subcategories)} subcategories, "
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

        state = await get_state(msg.from_user.id)
        state.pending_expense = entry
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
        state = await get_state(msg.from_user.id)

        # ── Jago debit card follow-up ──────────────────────────────────────────
        if state.pending_email_expense:
            tx = state.pending_email_expense
            state.pending_email_expense = None
            entry = ExpenseEntry(
                description=text,
                amount=tx.amount,
                date=tx.date,
                subcategory=tx.subcategory,
                account=tx.account,
                confidence=0.9,
            )
            state.pending_expense = entry
            await msg.answer(
                f"Got it! Confirm:\n\n{format_entry(entry)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(msg.from_user.id),
            )
            # Promote next queued debit card tx (if any)
            if state.pending_debit_queue:
                next_tx = state.pending_debit_queue.pop(0)
                state.pending_email_expense = next_tx
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
                expenses = await notion.fetch_expenses(owner)
                answer = await agent.answer_query(text, expenses, owner, state)
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

            state.pending_expense = entry
            await msg.answer(
                f"Got it! Confirm:\n\n{format_entry(entry)}",
                parse_mode="Markdown",
                reply_markup=make_confirm_keyboard(msg.from_user.id),
            )

        else:
            await msg.answer(
                "Hmm, not sure what you mean. Send a receipt photo or describe an expense."
            )

    # ── Inline keyboard callbacks ─────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("confirm:"))
    async def handle_confirm(callback: CallbackQuery) -> None:
        user_id = int(callback.data.split(":")[1])
        owner = get_owner(user_id)
        if not owner:
            await callback.answer("Not authorized.")
            return

        state = await get_state(user_id)
        entry = state.pending_expense
        if not entry:
            await callback.answer("No pending expense.")
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        state.pending_expense = None
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Logging...")

        status_msg = await callback.message.answer("⏳ Logging to Notion...")
        try:
            url = await notion.log_expense(entry, owner, cache)
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
        state = await get_state(user_id)
        state.pending_expense = None
        state.pending_email_expense = None
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Cancelled.")
        await callback.message.answer("Cancelled ❌")

    # ── Startup ───────────────────────────────────────────────────────────────
    try:
        await refresh_cache()
    except Exception as e:
        log.error(f"Initial cache load failed: {e}")
        # Don't abort startup — bot can still work, user can /refresh

    afif_id = next(
        (uid for uid, name in config.users.items() if name == "Afif"), None
    )

    afif_name = config.users.get(afif_id, "Afif") if afif_id else "Afif"
    email_watcher = EmailWatcher(
        config=config,
        notion=notion,
        agent=agent,
        cache_getter=lambda: cache,
        bot=bot,
        owner_telegram_id=afif_id,
        owner_name=afif_name,
        conversations=conversations,
        alert_fn=alert_owner,
    )
    _watcher_task = asyncio.create_task(email_watcher.run())
    log.info("Email watcher scheduled.")

    # Prevent silent task death — log critically if the watcher dies
    async def _watch_over(task: asyncio.Task) -> None:
        try:
            await task
        except Exception:
            log.critical("Email watcher task died!", exc_info=True)

    _watcher_task.add_done_callback(
        lambda t: asyncio.ensure_future(_watch_over(t))
    )

    log.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
