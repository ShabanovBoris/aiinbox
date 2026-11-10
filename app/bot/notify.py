import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions, ReplyParameters
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.item_card import build_item_card
from app.services.retrieval import load_item_sources_by_item
from app.storage.models import Item, User

log = logging.getLogger(__name__)


def _item_reply_parameters(item: Item) -> ReplyParameters | None:
    """Anchor any Telegram-captured Item result to the message that created it."""
    # ❌ Удален video-only reply gate: Telegram-captured Items all retain the same
    # message identity, so READY and FAILED use one reply projection.
    if item.telegram_message_id is None:
        return None
    # ❌ Удален allow_sending_without_reply: он мог скрыть потерю reply-якоря;
    # unanchored отправка разрешена только после классифицированного ответа Telegram ниже.
    return ReplyParameters(message_id=item.telegram_message_id)


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


async def _send_item_notification(
    bot: Bot, session_factory: async_sessionmaker, item: Item
) -> None:
    """Retry once without anchoring only when Telegram says the source message vanished."""
    async with session_factory() as session:
        user = await session.get(User, item.user_id)
        chat_id = user.telegram_chat_id if user else None
        if chat_id is None:
            log.warning("no chat_id for item delivery item_id=%s user_id=%s", item.id, item.user_id)
            return
        sources = (await load_item_sources_by_item(session, [item.id]))[item.id]
    text, markup = build_item_card(item, sources, original_available=True)

    async def send(reply_parameters: ReplyParameters | None) -> None:
        kwargs = text.as_kwargs()
        if reply_parameters is not None:
            kwargs["reply_parameters"] = reply_parameters
        await bot.send_message(
            chat_id,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            **kwargs,
        )

    reply_parameters = _item_reply_parameters(item)
    try:
        await send(reply_parameters)
    except TelegramBadRequest as exc:
        if reply_parameters is None or not _reply_target_is_missing(exc):
            raise
        await send(None)


async def send_item_result(
    bot: Bot,
    session_factory: async_sessionmaker,
    item: Item,
) -> None:
    """Доставка готового результата пользователю (auxiliary-операция).

    Ошибки доставки логируются вызывающим кодом и не переводят READY Item в FAILED.
    """
    await _send_item_notification(bot, session_factory, item)


async def send_item_failure(
    bot: Bot,
    session_factory: async_sessionmaker,
    item: Item,
) -> None:
    """Expose a failed Item's Retry action without pretending analysis succeeded."""
    await _send_item_notification(bot, session_factory, item)
