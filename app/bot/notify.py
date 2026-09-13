import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import format_ready_item
from app.bot.keyboards import item_keyboard
from app.storage.models import Item, User

log = logging.getLogger(__name__)


async def send_item_result(bot: Bot, session_factory: async_sessionmaker, item: Item) -> None:
    """Доставка готового результата пользователю (auxiliary-операция).

    Ошибки доставки логируются вызывающим кодом и не переводят READY Item в FAILED.
    """
    async with session_factory() as session:
        user = await session.get(User, item.user_id)
        chat_id = user.telegram_chat_id if user else None
    if chat_id is None:
        log.warning("no chat_id for result delivery item_id=%s user_id=%s", item.id, item.user_id)
        return
    await bot.send_message(chat_id, format_ready_item(item), reply_markup=item_keyboard(item))


async def send_item_failure(bot: Bot, session_factory: async_sessionmaker, item: Item) -> None:
    """Expose a failed Item's Retry action without pretending analysis succeeded."""
    async with session_factory() as session:
        user = await session.get(User, item.user_id)
        chat_id = user.telegram_chat_id if user else None
    if chat_id is None:
        log.warning("no chat_id for failure delivery item_id=%s user_id=%s", item.id, item.user_id)
        return
    await bot.send_message(
        chat_id,
        f"Не удалось обработать Item. Можно повторить попытку ({item.error_code or 'ошибка'}).",
        reply_markup=item_keyboard(item),
    )
