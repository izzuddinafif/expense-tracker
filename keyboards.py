from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from models import NotionCache


def make_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Simpan", callback_data=f"confirm:{user_id}"),
        InlineKeyboardButton(text="✏️ Edit", callback_data=f"edit:{user_id}"),
        InlineKeyboardButton(text="❌ Batal", callback_data=f"cancel:{user_id}"),
    ]])


def make_edit_field_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Deskripsi", callback_data=f"edit_desc:{user_id}")],
        [InlineKeyboardButton(text="💰 Jumlah", callback_data=f"edit_amount:{user_id}")],
        [InlineKeyboardButton(text="📅 Tanggal", callback_data=f"edit_date:{user_id}")],
        [InlineKeyboardButton(text="🏷 Kategori", callback_data=f"edit_cat:{user_id}")],
        [InlineKeyboardButton(text="❌ Batal edit", callback_data=f"edit_cancel:{user_id}")],
    ])


def make_income_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Simpan", callback_data=f"income_confirm:{user_id}"),
        InlineKeyboardButton(text="❌ Batal", callback_data=f"income_cancel:{user_id}"),
    ]])


def make_category_keyboard(page_id: str, cache: NotionCache) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=cat,
            callback_data=f"cat_pick:{page_id}:{i}",
        )]
        for i, cat in enumerate(cache.category_subcategories)
    ]
    buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data=f"cat_cancel:{page_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def make_subcategory_keyboard(
    page_id: str, cat_index: int, cache: NotionCache
) -> InlineKeyboardMarkup:
    cats = list(cache.category_subcategories.keys())
    if cat_index >= len(cats):
        return InlineKeyboardMarkup(inline_keyboard=[])
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
                callback_data=f"subcat_pick:{page_id}:{cat_index}:{offset + si}",
            ))
        buttons.append(row_buttons)
        offset += len(row)
    buttons.append([
        InlineKeyboardButton(text="⬅️ Kembali", callback_data=f"cat_back:{page_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)




