"""Owner-scoped lookup for reproducing a Telegram capture through the Bot API."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.storage.models import Item, Reminder, User

PROACTIVE_ATTENTION = "PROACTIVE_ATTENTION"


@dataclass(frozen=True, slots=True)
class OriginalMessageTarget:
    """Database-validated Telegram identity; no transport IDs come from callback data."""

    item_id: int
    source_chat_id: int
    destination_chat_id: int
    message_id: int


async def item_original_target(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    *,
    current_chat_id: int | None,
) -> OriginalMessageTarget | None:
    """Resolve an owned Item and close SQLite before the caller performs Telegram I/O."""
    if type(item_id) is not int or item_id < 1 or current_chat_id is None:
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Item.id, Item.telegram_message_id, User.telegram_chat_id)
                .join(User, User.id == Item.user_id)
                .where(Item.id == item_id, User.telegram_user_id == telegram_user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        stored_chat_id = row.telegram_chat_id
        message_id = row.telegram_message_id
        if stored_chat_id is None or message_id is None or stored_chat_id != current_chat_id:
            return None
        return OriginalMessageTarget(
            item_id=row.id,
            source_chat_id=stored_chat_id,
            destination_chat_id=current_chat_id,
            message_id=message_id,
        )


async def reminder_original_target(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    reminder_id: int,
    *,
    current_chat_id: int | None,
) -> OriginalMessageTarget | None:
    """Resolve the Item behind one owned, sent proactive Reminder before copying it."""
    if type(reminder_id) is not int or reminder_id < 1 or current_chat_id is None:
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Item.id, Item.telegram_message_id, User.telegram_chat_id)
                .join(Reminder, Reminder.item_id == Item.id)
                .join(User, User.id == Reminder.user_id)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "SENT",
                    Reminder.user_id == Item.user_id,
                    User.telegram_user_id == telegram_user_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        stored_chat_id = row.telegram_chat_id
        message_id = row.telegram_message_id
        if stored_chat_id is None or message_id is None or stored_chat_id != current_chat_id:
            return None
        return OriginalMessageTarget(
            item_id=row.id,
            source_chat_id=stored_chat_id,
            destination_chat_id=current_chat_id,
            message_id=message_id,
        )
