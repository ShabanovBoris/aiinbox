import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import format_attention_item, format_ready_item
from app.bot.keyboards import (
    feedback_category_keyboard,
    feedback_menu_keyboard,
    feedback_type_keyboard,
    item_keyboard,
)
from app.bot.provenance import normalize_forward_origin, text_with_entity_urls
from app.config import Settings
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.extractors.document import document_format_hint, safe_document_file_name
from app.extractors.instagram import is_instagram_reel_url
from app.services.actions import apply_item_action, record_item_events, set_item_interest
from app.services.attention_ranking import AttentionRankingService
from app.services.delivery import enqueue_item_video_delivery
from app.services.feedback import (
    correct_item_category_by_token,
    correct_item_type,
    record_item_feedback,
)
from app.services.ingestion import ingest_media, ingest_message
from app.services.notifications import (
    format_settings,
    get_notification_settings,
    update_notification_settings,
)
from app.services.profile import enqueue_profile_update
from app.services.retrieval import (
    TodayService,
    list_categories,
    list_category_items,
    list_inbox,
    search_items,
)
from app.services.url_parsing import find_urls
from app.storage.models import Item, ItemSource, User

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Personal AI Inbox — отправь или перешли текст, URL, voice/audio/video, документ, "
    "YouTube-ссылку или Instagram Reel.\n\n"
    "Команды:\n"
    "/today — приоритетные Items на сегодня\n"
    "/attention [1-5] — что сейчас заслуживает внимания\n"
    "/inbox — последние Items\n"
    "/search <текст> — поиск по сохранённому содержимому\n"
    "/category [имя] — категории и Items категории\n"
    "/profile — текущий профиль\n"
    "/profile_update <инструкция> — обновить профиль\n"
    "/settings — настройки digest и quiet hours\n"
    "/help — эта справка"
)


async def on_start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer("Personal AI Inbox готов. Просто отправь текст или ссылку.")


async def on_help(message: Message, settings: Settings) -> None:
    """Expose the stable Telegram command surface without business logic."""
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(HELP_TEXT)


async def on_settings(
    message: Message, settings: Settings, session_factory, arguments: str = ""
) -> None:
    """Show or minimally update notification settings without a settings app."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    from app.services.ingestion import get_or_create_user

    if await get_notification_settings(session_factory, user_id) is None:
        async with session_factory() as session:
            await get_or_create_user(
                session,
                telegram_user_id=user_id,
                chat_id=message.chat.id,
                timezone=settings.default_timezone,
            )
            await session.commit()
    parts = arguments.split(maxsplit=2)
    try:
        if parts and parts[0] == "timezone" and len(parts) == 2:
            result = await update_notification_settings(session_factory, user_id, timezone=parts[1])
        elif parts and parts[0] == "time" and len(parts) == 2:
            result = await update_notification_settings(
                session_factory, user_id, daily_digest_time=parts[1]
            )
        elif parts and parts[0] == "quiet" and len(parts) == 2:
            quiet_parts = parts[1].split("-", maxsplit=1)
            if len(quiet_parts) != 2:
                raise ValueError("Тихие часы: /settings quiet 22:30-08:00")
            result = await update_notification_settings(
                session_factory,
                user_id,
                quiet_hours_start=quiet_parts[0],
                quiet_hours_end=quiet_parts[1],
            )
        elif parts:
            await message.answer("Использование: /settings [timezone|time|quiet] <значение>")
            return
        else:
            result = await get_notification_settings(session_factory, user_id)
        if result is None:
            return
    except ValueError as exc:
        await message.answer(str(exc))
        return
    user, notification_settings = result
    from app.bot.keyboards import settings_keyboard

    await message.answer(
        format_settings(user, notification_settings),
        reply_markup=settings_keyboard(notification_settings["daily_digest_enabled"]),
    )


async def on_text(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        # Allowlist: неавторизованным молча не отвечаем и ничего не обрабатываем.
        log.warning("unauthorized telegram user ignored user_id=%s", user_id)
        return
    text = text_with_entity_urls(message.text, message.entities)
    if not text:
        return
    # Тяжёлая обработка запрещена в handler: только валидация, Item QUEUED и ответ.
    # Сначала persistence, потом ACK: при ошибке БД пользователь не получает
    # ложное подтверждение сохранения.
    result = await ingest_message(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        source_metadata=normalize_forward_origin(message.forward_origin),
        default_timezone=settings.default_timezone,
    )
    ack_lines = []
    if result.items:
        ack_lines.append(
            "Принял Reel. Разбираю…"
            if any(is_instagram_reel_url(url) for url in find_urls(text))
            else "Принял. Разбираю…"
        )
    if result.duplicates:
        ack_lines.append("Часть ссылок уже сохранена — дубли пропустил.")
    if ack_lines:
        await message.answer("\n".join(ack_lines))


async def on_voice_audio(
    message: Message,
    settings: Settings,
    session_factory: async_sessionmaker,
    media,  # MessageVoice | MessageAudio
    is_audio: bool,
    max_audio_bytes: int,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        log.warning("unauthorized telegram user ignored user_id=%s", user_id)
        return
    file_size = media.file_size or 0
    source_type = SourceType.AUDIO if is_audio else SourceType.VOICE
    # persist → ACK (порядок Phase 1). Oversized сохраняется атомарно при создании
    # как FAILED/TOO_LARGE (PRODUCT_SPEC §66) — без claimable промежуточного
    # состояния, чтобы воркер не начал скачивание.
    oversized = (file_size, max_audio_bytes) if file_size > max_audio_bytes else None
    source_metadata = normalize_forward_origin(message.forward_origin)
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=media.file_id,
        duration_seconds=media.duration,
        source_type=source_type,
        too_large=oversized,
        source_metadata=source_metadata,
        source_text=source_text,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if item.processing_status is ProcessingStatus.FAILED and item.error_code == "TOO_LARGE":
        limit_mb = max_audio_bytes / 1_000_000
        await message.answer(
            f"Файл слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{limit_mb:.0f} МБ). Метаданные сохранил — файл не скачан."
        )
        return
    if oversized is not None:
        limit_mb = max_audio_bytes / 1_000_000
        await message.answer(
            f"Файл слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{limit_mb:.0f} МБ), но текст и ссылки из сообщения разберу."
        )
        return
    label = "аудио" if is_audio else "голосовое"
    await message.answer(f"Принял {label}. Разбираю…")


async def on_video(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Persist Telegram video, caption and caption URLs as one composite Item."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    media = message.video
    if media is None and _is_video_document(message):
        media = message.document
    if media is None:
        return
    file_size = media.file_size or 0
    oversized = (
        (file_size, settings.max_video_bytes) if file_size > settings.max_video_bytes else None
    )
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=media.file_id,
        duration_seconds=getattr(media, "duration", None),
        source_type=SourceType.VIDEO,
        too_large=oversized,
        source_metadata=normalize_forward_origin(message.forward_origin),
        source_text=source_text,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if item.processing_status is ProcessingStatus.FAILED and item.error_code == "TOO_LARGE":
        await message.answer(
            f"Видео слишком большое ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{settings.max_video_bytes / 1_000_000:.0f} МБ)."
        )
        return
    if oversized is not None:
        await message.answer("Видео превышает лимит, но текст и ссылки из сообщения разберу.")
        return
    await message.answer("Принял видео. Разбираю текст, ссылки и видео…")


def _is_video_document(message: Message) -> bool:
    """Normalize Telegram's alternate transport for videos into the VIDEO source path.

    Telegram may expose a forwarded/uploaded video as Document instead of Video;
    MIME and common video suffixes are transport signals, while downstream
    extraction remains identical and still produces one media ItemSource.
    """
    document = message.document
    if document is None:
        return False
    mime_type = (document.mime_type or "").lower()
    file_name = (document.file_name or "").lower()
    return mime_type.startswith("video/") or file_name.endswith(
        (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi")
    )


async def on_forwarded_photo(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Analyze forwarded photo caption/links even while image understanding is unavailable."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    text = text_with_entity_urls(message.caption, message.caption_entities)
    if not text:
        await message.answer("Пересланные фото без поддерживаемой ссылки пока не поддерживаются.")
        return
    result = await ingest_message(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        source_metadata=normalize_forward_origin(message.forward_origin),
        default_timezone=settings.default_timezone,
    )
    ack_lines = []
    if result.items:
        ack_lines.append("Принял пересланное сообщение. Текст и ссылки разбираю…")
    if result.duplicates:
        ack_lines.append("Часть ссылок уже сохранена — дубли пропустил.")
    if ack_lines:
        await message.answer("\n".join(ack_lines))


# ❌ Удалена forwarded-only ветка отказа: документы обоих видов проходят общую проверку и pipeline.
async def on_unsupported_document(message: Message, settings: Settings) -> None:
    """Give the same concise format error for unsupported direct and forwarded files."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    await message.answer("Этот формат документа пока не поддерживается.")


async def on_document(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Persist supported documents as one file ItemSource; workers do all parsing."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    document = message.document
    if document is None:
        return
    file_name = safe_document_file_name(document.file_name)
    document_format = document_format_hint(file_name, document.mime_type)
    file_size = document.file_size or 0
    oversized = (
        (file_size, settings.max_document_bytes)
        if file_size > settings.max_document_bytes
        else None
    )
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    source_details = {
        key: value
        for key, value in {
            "file_name": file_name,
            "mime_type": (document.mime_type or "").split(";", 1)[0].strip().lower() or None,
            "document_format": document_format,
        }.items()
        if value is not None
    }
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=document.file_id,
        duration_seconds=None,
        source_type=SourceType.DOCUMENT,
        too_large=oversized,
        prechecked_failure=(
            ("UNSUPPORTED_SOURCE", "document format is unsupported")
            if document_format is None
            else None
        ),
        source_metadata=normalize_forward_origin(message.forward_origin),
        source_text=source_text,
        source_details=source_details,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if oversized is not None:
        if item.processing_status is ProcessingStatus.FAILED:
            answer = (
                f"Документ слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
                f"{settings.max_document_bytes / 1_000_000:.0f} МБ). Файл не скачан."
            )
        else:
            answer = (
                f"Документ превышает лимит {settings.max_document_bytes / 1_000_000:.0f} МБ; "
                "разберу подпись и ссылки из сообщения."
            )
        await message.answer(answer)
        return
    if document_format is None:
        await on_unsupported_document(message, settings)
        return
    label = file_name or "без имени"
    await message.answer(f"Принял документ {label}. Разбираю…")


def make_router(
    settings: Settings, session_factory: async_sessionmaker, max_audio_bytes: int = 20_000_000
) -> Router:
    router = Router()

    # Specific media handlers must precede the forwarded-text catch-all. Aiogram's
    # MagicFilter field lookup is permissive enough that relying on F.text alone for
    # dispatch priority can let a media update reach the wrong callback.
    @router.message(F.voice)
    async def voice(message: Message) -> None:
        await on_voice_audio(
            message, settings, session_factory, message.voice, False, max_audio_bytes
        )

    @router.message(F.audio)
    async def audio(message: Message) -> None:
        await on_voice_audio(
            message, settings, session_factory, message.audio, True, max_audio_bytes
        )

    @router.message(F.video)
    async def video(message: Message) -> None:
        await on_video(message, settings, session_factory)

    @router.message(F.forward_origin, F.photo)
    async def forwarded_photo(message: Message) -> None:
        await on_forwarded_photo(message, settings, session_factory)

    @router.message(F.document)
    async def document(message: Message) -> None:
        # Telegram sometimes serializes ordinary/forwarded video as Document.
        # Normalize it here so message transport does not change Item semantics.
        if _is_video_document(message):
            await on_video(message, settings, session_factory)
        else:
            await on_document(message, settings, session_factory)

    # Forwarded slash-prefixed text is captured content, not a command for this bot.
    # This edge rule must run before Command filters to preserve author semantics.
    @router.message(F.forward_origin, F.text)
    async def forwarded_text(message: Message) -> None:
        await on_text(message, settings, session_factory)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await on_start(message, settings)

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await on_help(message, settings)

    # Не-командный текст — источники TEXT/WEB; медиа-источники добавляются
    # в своих фазах и идут через тот же pipeline.
    @router.message(~F.forward_origin, F.text, ~F.text.startswith("/"))
    async def text(message: Message) -> None:
        await on_text(message, settings, session_factory)

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        await on_profile(message, settings, session_factory)

    @router.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        arguments = (message.text or "").removeprefix("/settings").strip()
        await on_settings(message, settings, session_factory, arguments)

    @router.message(Command("profile_update"))
    async def profile_update(message: Message) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        instruction = (message.text or "").removeprefix("/profile_update").strip()
        await on_profile_update(message, settings, session_factory, instruction)

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        await on_today(message, settings, session_factory)

    @router.message(Command("attention"))
    async def attention(message: Message) -> None:
        """Translate the Telegram command into the PM-07 preview application call."""
        parts = (message.text or "").split(maxsplit=1)
        arguments = parts[1] if len(parts) == 2 else ""
        await on_attention(message, settings, session_factory, arguments)

    @router.message(Command("inbox"))
    async def inbox(message: Message) -> None:
        await on_inbox(message, settings, session_factory)

    @router.message(Command("category"))
    async def category(message: Message) -> None:
        value = (message.text or "").removeprefix("/category").strip()
        await on_category(message, settings, session_factory, value)

    @router.message(Command("search"))
    async def search(message: Message) -> None:
        value = (message.text or "").removeprefix("/search").strip()
        await on_search(message, settings, session_factory, value)

    @router.callback_query(F.data.startswith("item:"))
    async def item_action(callback: CallbackQuery) -> None:
        await on_item_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("feedback:"))
    async def item_feedback(callback: CallbackQuery) -> None:
        await on_feedback_callback(callback, settings, session_factory)

    @router.callback_query(F.data == "settings:digest")
    async def settings_digest(callback: CallbackQuery) -> None:
        await on_settings_callback(callback, settings, session_factory)

    return router


async def on_item_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Translate an inline callback into one scoped application action."""
    user = callback.from_user
    if not settings.is_allowed(user.id) or not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) < 3 or parts[0] != "item":
        await callback.answer("Неизвестное действие")
        return
    action, raw_item_id = parts[1], parts[2]
    try:
        item_id = int(raw_item_id)
    except ValueError:
        await callback.answer("Некорректный Item")
        return
    if action == "video":
        if len(parts) != 4:
            await callback.answer("Некорректный источник видео")
            return
        try:
            source_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный источник видео")
            return
        result = await enqueue_item_video_delivery(
            session_factory,
            user.id,
            item_id,
            source_id,
        )
        if result is None:
            await callback.answer("Это видео сейчас недоступно")
        elif result == "IN_PROGRESS":
            await callback.answer("Видео уже готовится")
        else:
            await callback.answer("Поставил видео в очередь")
        return
    if action == "interest":
        if len(parts) != 4:
            await callback.answer("Некорректный уровень интереса")
            return
        try:
            level = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный уровень интереса")
            return
        if level not in {1, 2, 3}:
            await callback.answer("Некорректный уровень интереса")
            return
        result = await set_item_interest(session_factory, user.id, item_id, level)
        if result is None:
            await callback.answer("Item не найден")
            return
        item, changed = result
        if changed and callback.message:
            async with session_factory() as session:
                sources = list(
                    (
                        await session.scalars(
                            select(ItemSource)
                            .where(ItemSource.item_id == item.id)
                            .order_by(ItemSource.source_index, ItemSource.id)
                        )
                    ).all()
                )
            await callback.message.edit_text(
                format_ready_item(item), reply_markup=item_keyboard(item, sources)
            )
        await callback.answer()
        return
    if action == "later":
        from app.bot.keyboards import snooze_keyboard

        if callback.message:
            await callback.message.edit_text(
                "Когда напомнить?", reply_markup=snooze_keyboard(item_id)
            )
        await callback.answer()
        return
    if action == "snooze" and len(parts) == 4:
        durations = {
            "tomorrow": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
        }
        duration = durations.get(parts[3])
        if duration is None:
            await callback.answer("Некорректный срок")
            return
        action_name = "snooze"
        snoozed_until = datetime.now(UTC).replace(tzinfo=None) + duration
    elif action == "cancel":
        action_name, snoozed_until = "cancel_snooze", None
    elif action in {"done", "archive", "retry"}:
        action_name, snoozed_until = action, None
    else:
        await callback.answer("Неизвестное действие")
        return

    item = await apply_item_action(
        session_factory, user.id, item_id, action_name, snoozed_until=snoozed_until
    )
    if item is None:
        await callback.answer("Item не найден")
        return
    # ❌ Удален label lookup только по requested action: при concurrent CAS он
    # мог подтверждать проигравшее действие вместо persisted результата.
    if callback.message:
        await callback.message.edit_text(_item_action_label(item, action_name), reply_markup=None)
    await callback.answer()


async def _load_ready_feedback_projection(
    session_factory: async_sessionmaker, telegram_user_id: int, item_id: int
) -> tuple[Item, list[ItemSource]] | None:
    """Build a source-aware keyboard projection after checking Item ownership."""
    async with session_factory() as session:
        item = await session.scalar(
            select(Item)
            .join(User, User.id == Item.user_id)
            .where(
                User.telegram_user_id == telegram_user_id,
                Item.id == item_id,
                Item.processing_status == ProcessingStatus.READY,
            )
        )
        if item is None:
            return None
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item.id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
        return item, sources


async def _edit_feedback_keyboard_if_changed(message, reply_markup) -> None:
    """Avoid Telegram's message-is-not-modified error on duplicate callbacks."""
    if (
        message is None
        or not hasattr(message, "edit_reply_markup")
        or getattr(message, "reply_markup", None) == reply_markup
    ):
        return
    await message.edit_reply_markup(reply_markup=reply_markup)


async def _present_corrected_feedback_item(message, item: Item, sources: list[ItemSource]) -> None:
    """Refresh only stale result text; duplicate/no-op callbacks update markup alone."""
    markup = item_keyboard(item, sources)
    result_text = format_ready_item(item, sources)
    if (
        message is not None
        and hasattr(message, "edit_text")
        and getattr(message, "text", None) != result_text
    ):
        await message.edit_text(result_text, reply_markup=markup)
        return
    await _edit_feedback_keyboard_if_changed(message, markup)


async def on_feedback_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Translate feedback callbacks into scoped service calls and UI projections."""
    if not settings.is_allowed(callback.from_user.id) or not callback.data:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) < 3 or parts[0] != "feedback":
        await callback.answer("Некорректное действие")
        return
    action, raw_item_id = parts[1], parts[2]
    try:
        item_id = int(raw_item_id)
    except ValueError:
        await callback.answer("Некорректный Item")
        return
    if item_id < 1:
        await callback.answer("Некорректный Item")
        return

    if action in {"menu", "category_menu", "type_menu", "back"}:
        if len(parts) != 3:
            await callback.answer("Некорректное действие")
            return
        projection = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if projection is None:
            await callback.answer("Item недоступен")
            return
        item, sources = projection
        if action == "menu":
            markup = feedback_menu_keyboard(item_id)
        elif action == "type_menu":
            markup = feedback_type_keyboard(item_id)
        elif action == "category_menu":
            async with session_factory() as session:
                categories = await list_categories(session, item.user_id)
            # Frequency order keeps the keyboard's bounded choices useful without
            # introducing pagination state or callback payloads containing text.
            categories = sorted(categories, key=lambda row: (-row[1], row[0].casefold()))
            markup = feedback_category_keyboard(
                item_id, [category for category, _count in categories]
            )
        else:
            markup = item_keyboard(item, sources)
        await _edit_feedback_keyboard_if_changed(callback.message, markup)
        await callback.answer()
        return

    idempotency_key = f"telegram-callback:{callback.id}"
    # ❌ Удален handler-level receipt claim в отдельной транзакции: service
    # теперь связывает eligibility/token resolution и receipt в одной записи.
    if action in {
        "useful",
        "not_interesting",
        "priority_higher",
        "priority_lower",
        "summary_wrong",
    }:
        if len(parts) != 3:
            await callback.answer("Некорректное действие")
            return
        event_types = {
            "useful": "USEFUL",
            "not_interesting": "NOT_INTERESTING",
            "priority_higher": "PRIORITY_HIGHER",
            "priority_lower": "PRIORITY_LOWER",
            "summary_wrong": "SUMMARY_REPORTED_WRONG",
        }
        item = await record_item_feedback(
            session_factory,
            callback.from_user.id,
            item_id,
            event_types[action],
            idempotency_key=idempotency_key,
        )
        if item is None:
            await callback.answer("Item недоступен")
            return
        if action in {"useful", "not_interesting"}:
            confirmations = {
                "useful": "Записал 👍",
                "not_interesting": "Записал — буду учитывать",
            }
            await callback.answer(confirmations[action])
            return
        projection = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if projection is not None:
            current_item, sources = projection
            await _edit_feedback_keyboard_if_changed(
                callback.message, item_keyboard(current_item, sources)
            )
        confirmations = {
            "priority_higher": "Записал сигнал о приоритете",
            "priority_lower": "Записал сигнал о приоритете",
            "summary_wrong": "Отметил summary как неверный",
        }
        await callback.answer(confirmations[action])
        return

    if action == "category":
        if (
            len(parts) != 4
            or len(parts[3]) != 20
            or any(character not in "0123456789abcdef" for character in parts[3])
        ):
            await callback.answer("Некорректная категория")
            return
        try:
            result = await correct_item_category_by_token(
                session_factory,
                callback.from_user.id,
                item_id,
                parts[3],
                idempotency_key=idempotency_key,
            )
        except ValueError:
            await callback.answer("Категория не подходит")
            return
        if result is None:
            current = await _load_ready_feedback_projection(
                session_factory, callback.from_user.id, item_id
            )
            await callback.answer(
                "Категория больше недоступна" if current is not None else "Item недоступен"
            )
            return
        _updated_item, changed = result
        current = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if current is not None:
            current_item, sources = current
            await _present_corrected_feedback_item(callback.message, current_item, sources)
        await callback.answer("Категория изменена" if changed else "Категория без изменений")
        return

    if action == "type":
        if len(parts) != 4:
            await callback.answer("Некорректный тип")
            return
        try:
            item_type = ItemType(parts[3])
        except ValueError:
            await callback.answer("Некорректный тип")
            return
        try:
            result = await correct_item_type(
                session_factory,
                callback.from_user.id,
                item_id,
                item_type,
                idempotency_key=idempotency_key,
            )
        except ValueError:
            await callback.answer("Некорректный тип")
            return
        if result is None:
            await callback.answer("Item недоступен")
            return
        _updated_item, changed = result
        current = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if current is not None:
            current_item, sources = current
            await _present_corrected_feedback_item(callback.message, current_item, sources)
        await callback.answer("Тип изменён" if changed else "Тип без изменений")
        return

    await callback.answer("Неизвестное действие")


# Callback text is derived from canonical persisted state, not requested input:
# a losing concurrent CAS must never acknowledge an action that did not happen.
def _item_action_label(item: Item, action_name: str) -> str:
    if action_name == "retry":
        return {
            ProcessingStatus.QUEUED: "Повторно поставил в обработку 🔁",
            ProcessingStatus.PROCESSING: "Уже обрабатывается ⏳",
            ProcessingStatus.READY: "Уже обработано ✅",
            ProcessingStatus.FAILED: "Повтор не запущен",
        }[item.processing_status]
    expected_state = {
        "done": ItemState.DONE,
        "archive": ItemState.ARCHIVED,
        "snooze": ItemState.SNOOZED,
        "cancel_snooze": ItemState.ACTIVE,
    }.get(action_name)
    if item.state is expected_state:
        return {
            "done": "Готово ✅",
            "archive": "В архиве 🗄",
            "snooze": "Отложено ⏰",
            "cancel_snooze": "Отложенное действие отменено",
        }[action_name]
    return {
        ItemState.ACTIVE: "Активно",
        ItemState.SNOOZED: "Отложено ⏰",
        ItemState.DONE: "Готово ✅",
        ItemState.ARCHIVED: "В архиве 🗄",
    }[item.state]


async def on_settings_callback(
    callback: CallbackQuery, settings: Settings, session_factory
) -> None:
    """Toggle only the safe, single-tap setting and redraw the compact UI."""
    if not settings.is_allowed(callback.from_user.id):
        await callback.answer()
        return
    current = await get_notification_settings(session_factory, callback.from_user.id)
    if current is None:
        await callback.answer("Пользователь не найден")
        return
    user, values = current
    updated = await update_notification_settings(
        session_factory,
        callback.from_user.id,
        daily_digest_enabled=not values["daily_digest_enabled"],
    )
    if callback.message and updated is not None:
        from app.bot.keyboards import settings_keyboard

        updated_user, updated_values = updated
        await callback.message.edit_text(
            format_settings(updated_user, updated_values),
            reply_markup=settings_keyboard(updated_values["daily_digest_enabled"]),
        )
    await callback.answer()


async def on_profile(message: Message, settings: Settings, session_factory) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    from app.bot.formatting import format_profile
    from app.services.ingestion import get_or_create_user
    from app.services.profile import get_profile

    async with session_factory() as session:
        # get_or_create + get_profile: lazy seed работает и для первого /profile
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        await session.commit()
        profile = await get_profile(session, user.id)
    await message.answer(format_profile(profile))


async def on_profile_update(
    message: Message,
    settings: Settings,
    session_factory,
    instruction: str,
) -> None:
    if not instruction:
        await message.answer("Использование: /profile_update <что изменить>")
        return
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    # Durable job + быстрый ACK: LLM/merge выполняет фоновый worker (ТЗ §14/§68).
    await enqueue_profile_update(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        instruction=instruction,
        default_timezone=settings.default_timezone,
    )
    log.info("profile update queued user_id=%s", user_id)
    await message.answer("Принял. Обновляю профиль…")


async def _allowed(message: Message, settings: Settings) -> bool:
    return settings.is_allowed(message.from_user.id if message.from_user else None)


async def on_today(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_today
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        user_id = user.id
        items = await TodayService().list_items(session, user.id)
        response = format_today(items)
        await session.commit()
    await message.answer(response)

    # PM-07 treats this history as exposure, so a failed Telegram send must not
    # suppress these Items in a later attention preview.
    if items:
        async with session_factory() as session:
            await record_item_events(session, user_id, [item.id for item in items], "TODAY_SHOWN")
            await session.commit()


async def on_attention(
    message: Message, settings: Settings, session_factory, arguments: str = ""
) -> None:
    """Build the preview before Telegram I/O, then persist exposure after each sent card."""
    if not await _allowed(message, settings):
        return

    parts = arguments.split()
    limit = None
    if parts:
        try:
            if len(parts) != 1:
                raise ValueError
            requested_limit = int(parts[0])
            if not 1 <= requested_limit <= 5:
                raise ValueError
        except ValueError:
            await message.answer("Использование: /attention [1-5]")
            return
        limit = requested_limit

    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        ranked = await AttentionRankingService().list_ranked(session, user.id, limit=limit)
        item_ids = [item.id for item, _ in ranked]
        sources_by_item: dict[int, list[ItemSource]] = {item_id: [] for item_id in item_ids}
        if item_ids:
            sources = (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id.in_(item_ids))
                    .order_by(ItemSource.item_id, ItemSource.source_index, ItemSource.id)
                )
            ).all()
            for source in sources:
                sources_by_item[source.item_id].append(source)
        user_id = user.id
        await session.commit()

    if not ranked:
        await message.answer("Сейчас нет подходящих Items.")
        return

    await message.answer("🎯 Сейчас заслуживает внимания:")
    count = len(ranked)
    for index, (item, rank) in enumerate(ranked, start=1):
        await message.answer(
            format_attention_item(index, count, item, rank),
            reply_markup=item_keyboard(item, sources_by_item[item.id]),
        )
        # Exposure is durable only after Telegram accepted this card; each Item
        # commits independently so a later delivery failure leaves a truthful prefix.
        async with session_factory() as session:
            await record_item_events(session, user_id, [item.id], "ATTENTION_SHOWN")
            await session.commit()


async def on_inbox(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        await session.commit()
        items = await list_inbox(session, user.id)
    await message.answer(format_item_list(items, "Входящие:"))


async def on_category(message: Message, settings: Settings, session_factory, category: str) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_categories, format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        await session.commit()
        if category:
            items = await list_category_items(session, user.id, category)
            response = format_item_list(items, f"Категория: {category}")
        else:
            response = format_categories(await list_categories(session, user.id))
    await message.answer(response)


async def on_search(message: Message, settings: Settings, session_factory, query: str) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    if not query:
        await message.answer("Использование: /search <запрос>")
        return
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        await session.commit()
        items = await search_items(session, user.id, query)
    await message.answer(format_item_list(items, "Результаты поиска:"))
