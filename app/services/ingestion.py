import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.extractors.youtube import is_youtube_url
from app.services.url_parsing import normalize_url, parse_message
from app.storage.models import Content, Event, Item, ItemSource, User

log = logging.getLogger(__name__)


async def get_or_create_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    chat_id: int | None,
    timezone: str = "UTC",
) -> User:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is not None:
        return user
    user = User(telegram_user_id=telegram_user_id, telegram_chat_id=chat_id, timezone=timezone)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Гонка первых сообщений нового пользователя: параллельный запрос уже
        # создал User (уникальность telegram_user_id). Откатываем нашу вставку
        # и переиспользуем существующую запись — Item при этом не теряется.
        await session.rollback()
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            raise
    return user


class IngestResult:
    def __init__(self, items: list[Item], duplicates: list[str]):
        self.items = items
        self.duplicates = duplicates


async def ingest_message(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
    text: str,
    source_metadata: dict | None = None,
    default_timezone: str = "UTC",
) -> IngestResult:
    """Persist one Telegram message as one Item with independently extractable URLs.

    Text/caption stays Item-level context; every distinct URL becomes ItemSource.
    Forwarding changes provenance/intent semantics only, never Item cardinality.
    """
    note, raw_urls = parse_message(text)
    is_forwarded = bool(source_metadata and source_metadata.get("forwarded") is True)
    user_note = "" if is_forwarded else note
    urls = _normalized_urls(raw_urls)
    primary_type = urls[0][0] if len(urls) == 1 else SourceType.TEXT
    primary_url = urls[0][1] if len(urls) == 1 else None
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=default_timezone,
        )
        user_id = user.id
        item = Item(
            user_id=user_id,
            telegram_message_id=message_id,
            source_index=0,
            processing_status=ProcessingStatus.QUEUED,
            source_type=primary_type,
            source_url=primary_url,
            source_metadata_json=dict(source_metadata) if source_metadata else None,
            user_note=user_note,
        )
        session.add(item)
        try:
            await session.flush()
            for source_index, (source_type, url) in enumerate(urls):
                session.add(
                    ItemSource(
                        item_id=item.id,
                        source_index=source_index,
                        source_type=source_type,
                        source_url=url,
                    )
                )
            await _add_created_events(session, [item])
            _add_source_contents(session, [item], text)
            await session.commit()
        except IntegrityError:
            # Duplicate Telegram update: DB identity wins even under concurrent delivery.
            await session.rollback()
            existing = await session.scalar(
                select(Item).where(
                    Item.user_id == user_id,
                    Item.telegram_message_id == message_id,
                    Item.source_index == 0,
                )
            )
            if existing is None:
                raise
            return IngestResult([existing], [])
        log.info(
            "item queued id=%s user_id=%s source_type=%s sources=%s",
            item.id,
            user_id,
            item.source_type.value,
            len(urls),
        )
        return IngestResult([item], [])


def _normalized_urls(raw_urls: list[str]) -> list[tuple[SourceType, str]]:
    """Build stable per-message URL source identities while removing local repeats."""
    result: list[tuple[SourceType, str]] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        normalized = normalize_url(raw_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        source_type = SourceType.YOUTUBE if is_youtube_url(normalized) else SourceType.WEB
        result.append((source_type, normalized))
    return result


def _make_media_item(
    user_id: int,
    message_id: int,
    file_id: str,
    duration_seconds: int | None,
    source_type: SourceType,
    source_metadata: dict | None = None,
    source_url: str | None = None,
    user_note: str = "",
) -> Item:
    return Item(
        user_id=user_id,
        telegram_message_id=message_id,
        source_index=0,
        processing_status=ProcessingStatus.QUEUED,
        source_type=source_type,
        source_url=source_url,
        source_file_id=file_id,
        content_duration_seconds=duration_seconds,
        source_metadata_json=dict(source_metadata) if source_metadata else None,
        user_note=user_note,
    )


# ❌ Удалены отдельные фабрики TEXT/WEB Item: после перехода на message-as-Item
# URL создаётся как ItemSource, а сам Item собирается один раз на Telegram message.


async def _add_created_events(session: AsyncSession, items: list[Item]) -> None:
    """Persist one CREATED event per newly materialized Item before its commit."""
    if not items:
        return
    await session.flush()
    session.add_all(
        [Event(user_id=item.user_id, item_id=item.id, event_type="CREATED") for item in items]
    )


def _add_source_contents(session: AsyncSession, items: list[Item], source_text: str | None) -> None:
    """Persist the original Telegram text/caption beside Item.

    USER_TEXT is the durable message-level source text used for fallback analysis,
    search and forwarded source_context; user_note remains the intent projection.
    """
    if not source_text or not source_text.strip():
        return
    session.add_all(
        [Content(item_id=item.id, kind=ContentKind.USER_TEXT, text=source_text) for item in items]
    )


async def ingest_voice(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
    file_id: str,
    duration_seconds: int | None,
    source_type: SourceType,
    too_large: tuple[int, int] | None = None,
    source_metadata: dict | None = None,
    source_text: str | None = None,
    default_timezone: str = "UTC",
) -> IngestResult:
    """Persist media + caption URLs as one Item whose sources extract independently.

    A known oversized media source fails locally. If caption text or another URL
    remains analyzable, the Item still enters the queue and can finish PARTIAL.
    """
    text = source_text or ""
    note, raw_urls = parse_message(text) if text else ("", [])
    is_forwarded = bool(source_metadata and source_metadata.get("forwarded") is True)
    user_note = "" if is_forwarded else note
    urls = _normalized_urls(raw_urls)
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=default_timezone,
        )
        # user.id до транзакции: rollback истекает объекты (см. ingest_message).
        user_id = user.id
        item = _make_media_item(
            user_id,
            message_id,
            file_id,
            duration_seconds,
            source_type,
            source_metadata,
            source_url=urls[0][1] if len(urls) == 1 else None,
            user_note=user_note,
        )
        media_status = "PENDING"
        media_error_code = None
        media_error_message = None
        if too_large is not None:
            actual, limit = too_large
            media_status = "FAILED"
            media_error_code = "TOO_LARGE"
            media_error_message = f"file too large: {actual} > {limit} bytes"
            if not text.strip() and not urls:
                item.processing_status = ProcessingStatus.FAILED
                item.error_code = media_error_code
                item.error_message = media_error_message
        session.add(item)
        try:
            await session.flush()
            session.add(
                ItemSource(
                    item_id=item.id,
                    source_index=0,
                    source_type=source_type,
                    source_file_id=file_id,
                    content_duration_seconds=duration_seconds,
                    extraction_status=media_status,
                    error_code=media_error_code,
                    error_message=media_error_message,
                )
            )
            for offset, (url_type, url) in enumerate(urls, start=1):
                session.add(
                    ItemSource(
                        item_id=item.id,
                        source_index=offset,
                        source_type=url_type,
                        source_url=url,
                    )
                )
            await _add_created_events(session, [item])
            _add_source_contents(session, [item], text)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(Item).where(
                    Item.user_id == user_id,
                    Item.telegram_message_id == message_id,
                    Item.source_index == 0,
                )
            )
            if existing is None:
                raise
            return IngestResult([existing], [])
        log.info(
            "item queued id=%s user_id=%s source_type=%s",
            item.id,
            user.id,
            item.source_type.value,
        )
        return IngestResult([item], [])


# ❌ Удален URL-per-Item race resolver: URL больше не является идентичностью Item;
# один Telegram message атомарно создаёт один Item и дочерние ItemSource.
