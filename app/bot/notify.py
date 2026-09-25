import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import format_item_failure, format_ready_item_compact
from app.bot.keyboards import item_keyboard
from app.storage.models import Item, ItemSource, User

log = logging.getLogger(__name__)


def _item_reply_parameters(item: Item) -> ReplyParameters | None:
    """Anchor any Telegram-captured Item result to the message that created it."""
    # ❌ Удален video-only reply gate: Telegram-captured Items all retain the same
    # message identity, so READY and FAILED use one reply projection.
    if item.telegram_message_id is None:
        return None
    return ReplyParameters(
        message_id=item.telegram_message_id,
        allow_sending_without_reply=True,
    )


def _reply_target_is_missing(exc: TelegramBadRequest) -> bool:
    """Limit unanchored fallback to Telegram errors that identify the reply target."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "reply message not found",
            "message to be replied not found",
            "replied message not found",
            "reply target not found",
        )
    )


async def _send_item_notification(bot, chat_id: int, item: Item, text: str, sources) -> None:
    """Retry once without anchoring only when Telegram says the source message vanished."""
    common_kwargs = {
        "reply_markup": item_keyboard(item, sources, original_available=True),
    }
    reply_parameters = _item_reply_parameters(item)
    if reply_parameters is None:
        await bot.send_message(chat_id, text, **common_kwargs)
        return
    try:
        await bot.send_message(
            chat_id,
            text,
            **common_kwargs,
            reply_parameters=reply_parameters,
        )
    except TelegramBadRequest as exc:
        if not _reply_target_is_missing(exc):
            raise
        await bot.send_message(chat_id, text, **common_kwargs)


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
    await _send_item_notification(
        bot, chat_id, item, format_ready_item_compact(item, sources), sources
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
    await _send_item_notification(bot, chat_id, item, message, sources)
