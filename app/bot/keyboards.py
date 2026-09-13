"""Small Telegram keyboard projections for the Item action surface."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.storage.models import Item


def item_keyboard(item: Item) -> InlineKeyboardMarkup:
    """Keep action intent in callback data; lifecycle state stays in SQLite."""
    rows = [
        [
            InlineKeyboardButton(text="✅ Done", callback_data=f"item:done:{item.id}"),
            InlineKeyboardButton(text="⏰ Later", callback_data=f"item:later:{item.id}"),
        ],
        [InlineKeyboardButton(text="🗄 Archive", callback_data=f"item:archive:{item.id}")],
    ]
    if item.source_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть", url=item.source_url)])
    if item.processing_status.value == "FAILED":
        rows.append([InlineKeyboardButton(text="🔁 Retry", callback_data=f"item:retry:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """The Later choices are explicit callback values, not free-form timestamps."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Завтра", callback_data=f"item:snooze:{item_id}:tomorrow"
                ),
                InlineKeyboardButton(
                    text="Через неделю", callback_data=f"item:snooze:{item_id}:week"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Через месяц", callback_data=f"item:snooze:{item_id}:month"
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"item:cancel:{item_id}"),
            ],
        ]
    )


def settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Minimal settings projection: the common digest toggle is one tap."""
    label = "🔕 Выключить digest" if enabled else "🔔 Включить digest"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="settings:digest")]]
    )
