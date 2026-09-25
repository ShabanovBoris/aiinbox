import logging

from aiogram import Bot
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import format_item_failure, format_ready_item_compact
from app.bot.keyboards import item_keyboard
from app.domain.enums import SourceType
from app.storage.models import Item, ItemSource, User

log = logging.getLogger(__name__)


def _video_source_reply_parameters(item: Item, sources: list[ItemSource]) -> ReplyParameters | None:
    """Make video results navigable from Telegram's reply preview.

    Telegram has no message permalink for a private user-bot chat, so the
    application anchors the result to the submitted video message instead.
    """
    has_video = item.source_type is SourceType.VIDEO or any(
        source.source_type is SourceType.VIDEO for source in sources
    )
    if not has_video or item.telegram_message_id is None:
        return None
    return ReplyParameters(
        message_id=item.telegram_message_id,
        allow_sending_without_reply=True,
    )


async def send_item_result(bot: Bot, session_factory: async_sessionmaker, item: Item) -> None:
    """Доставка готового результата пользователю (auxiliary-операция).

    Ошибки доставки логируются вызывающим кодом и не переводят READY Item в FAILED.
    """
    async with session_factory() as session:
        user = await session.get(User, item.user_id)
        chat_id = user.telegram_chat_id if user else None
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item.id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
    if chat_id is None:
        log.warning("no chat_id for result delivery item_id=%s user_id=%s", item.id, item.user_id)
        return
    await bot.send_message(
        chat_id,
        format_ready_item_compact(item, sources),
        reply_markup=item_keyboard(item, sources),
        reply_parameters=_video_source_reply_parameters(item, sources),
    )


async def send_item_failure(bot: Bot, session_factory: async_sessionmaker, item: Item) -> None:
    """Expose a failed Item's Retry action without pretending analysis succeeded."""
    async with session_factory() as session:
        user = await session.get(User, item.user_id)
        chat_id = user.telegram_chat_id if user else None
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item.id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
    if chat_id is None:
        log.warning("no chat_id for failure delivery item_id=%s user_id=%s", item.id, item.user_id)
        return
    # ❌ Удалено локальное дублирование failure-copy: чистый formatter нужен и
    # доставке, и кнопке Back, чтобы обе проекции восстанавливали один текст.
    message = format_item_failure(item, sources)
    await bot.send_message(
        chat_id,
        message,
        reply_markup=item_keyboard(item, sources),
        reply_parameters=_video_source_reply_parameters(item, sources),
    )
