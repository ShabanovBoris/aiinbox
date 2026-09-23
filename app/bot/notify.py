import logging

from aiogram import Bot
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import format_instagram_failure_reason, format_ready_item
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
        format_ready_item(item, sources),
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
    failed_sources = [source for source in sources if source.extraction_status == "FAILED"]
    instagram_failure = next(
        (source for source in failed_sources if source.source_type is SourceType.INSTAGRAM), None
    )
    error_code = instagram_failure.error_code if instagram_failure else item.error_code
    retryable = (
        item.processing_stage != "EXTRACTING"
        or not failed_sources
        or any(not source.failure_is_permanent for source in failed_sources)
    )
    if instagram_failure and error_code == "AUTH_REQUIRED":
        message = (
            "Не удалось получить Reel: "
            f"{format_instagram_failure_reason(error_code)}. Ссылка сохранена; "
            "после настройки INSTAGRAM_COOKIES_FILE нажмите Retry."
        )
    elif instagram_failure and error_code == "RATE_LIMITED":
        message = (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Ссылка сохранена; попробуйте Retry позже."
        )
    elif instagram_failure and error_code == "UNSUPPORTED_SOURCE":
        message = (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Отправьте ссылку на конкретный Reel."
        )
    else:
        message = f"Не удалось обработать Item ({item.error_code or 'ошибка'})."
        if retryable:
            message = (
                "Не удалось обработать Item. Можно повторить попытку "
                f"({item.error_code or 'ошибка'})."
            )
    await bot.send_message(
        chat_id,
        message,
        reply_markup=item_keyboard(item, sources),
        reply_parameters=_video_source_reply_parameters(item, sources),
    )
