import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ProcessingStatus, SourceType
from app.services.url_parsing import normalize_url, parse_message
from app.storage.models import Item, User

log = logging.getLogger(__name__)


async def get_or_create_user(
    session: AsyncSession, *, telegram_user_id: int, chat_id: int | None
) -> User:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is not None:
        return user
    user = User(telegram_user_id=telegram_user_id, telegram_chat_id=chat_id)
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
) -> IngestResult:
    """Идемпотентная точка входа сообщения: Telegram update → Item(s) QUEUED.

    Без URL → один TEXT-Item. С URL'ами → Item на каждый нормализованный URL
    (source_index по порядку), общий текст — user_note (ТЗ §13). Повтор URL тем
    же пользователем дедуплицируется по (user_id, source_url) на уровне БД.
    """
    note, raw_urls = parse_message(text)
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_user_id=telegram_user_id, chat_id=chat_id)
        user_id = user.id
        items: list[Item] = []
        duplicates: list[str] = []

        if not raw_urls:
            items.append(_make_text_item(user_id, message_id, text))
        else:
            seen: set[str] = set()
            for index, raw_url in enumerate(raw_urls):
                normalized = normalize_url(raw_url)
                if normalized in seen:
                    continue  # повтор URL внутри одного сообщения
                seen.add(normalized)
                existing = await session.scalar(
                    select(Item).where(Item.user_id == user_id, Item.source_url == normalized)
                )
                if existing is not None:
                    log.info("duplicate url ignored item_id=%s url=%s", existing.id, normalized)
                    duplicates.append(normalized)
                    continue
                items.append(_make_web_item(user_id, message_id, index, normalized, note))

        for item in items:
            session.add(item)
        try:
            await session.commit()
        except IntegrityError:
            # Гонка дедупликации: параллельный запрос сохранил тот же URL.
            # Откат и пере-выборка существующих Item'ов вместо дублей.
            await session.rollback()
            items, duplicates = await _resolve_after_race(
                session, user_id, message_id, note, raw_urls
            )
        for item in items:
            log.info(
                "item queued id=%s user_id=%s source_type=%s",
                item.id,
                user_id,
                item.source_type.value,
            )
        return IngestResult(items, duplicates)


def _make_media_item(
    user_id: int,
    message_id: int,
    file_id: str,
    duration_seconds: int | None,
    source_type: SourceType,
) -> Item:
    return Item(
        user_id=user_id,
        telegram_message_id=message_id,
        source_index=0,
        processing_status=ProcessingStatus.QUEUED,
        source_type=source_type,
        source_file_id=file_id,
        content_duration_seconds=duration_seconds,
        user_note="",
    )


def _make_text_item(user_id: int, message_id: int, text: str) -> Item:
    return Item(
        user_id=user_id,
        telegram_message_id=message_id,
        source_index=0,
        processing_status=ProcessingStatus.QUEUED,
        source_type=SourceType.TEXT,
        user_note=text,
    )


def _make_web_item(user_id: int, message_id: int, source_index: int, url: str, note: str) -> Item:
    return Item(
        user_id=user_id,
        telegram_message_id=message_id,
        source_index=source_index,
        processing_status=ProcessingStatus.QUEUED,
        source_type=SourceType.WEB,
        source_url=url,
        user_note=note,
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
) -> IngestResult:
    """Voice/audio → один Item с file_id; сам файл скачивается позже,
    в extraction-этапе воркера (handler не делает тяжёлой работы).

    too_large=(actual, limit): Item сразу персистится атомарно как FAILED/TOO_LARGE
    с метаданными (PRODUCT_SPEC §66) — без промежуточного claimable QUEUED.
    """
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_user_id=telegram_user_id, chat_id=chat_id)
        # user.id до транзакции: rollback истекает объекты (см. ingest_message).
        user_id = user.id
        item = _make_media_item(user_id, message_id, file_id, duration_seconds, source_type)
        if too_large is not None:
            actual, limit = too_large
            item.processing_status = ProcessingStatus.FAILED
            item.error_code = "TOO_LARGE"
            item.error_message = f"file too large: {actual} > {limit} bytes"
        session.add(item)
        try:
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


async def _resolve_after_race(
    session: AsyncSession, user_id: int, message_id: int, note: str, raw_urls: list[str]
) -> tuple[list[Item], list[str]]:
    """Сходящийся resolve после IntegrityError: повторная вставка текстового Item
    невозможна — уже существующий возвращается как результат."""
    if not raw_urls:
        # Повторный TEXT-update: уникальность (user_id, message_id, source_index)
        existing = await session.scalar(
            select(Item).where(
                Item.user_id == user_id,
                Item.telegram_message_id == message_id,
                Item.source_index == 0,
            )
        )
        return ([existing] if existing is not None else [], [])
    return await _ingest_web_urls(session, user_id, message_id, note, raw_urls)


async def _ingest_web_urls(
    session: AsyncSession,
    user_id: int,
    message_id: int,
    note: str,
    raw_urls: list[str],
) -> tuple[list[Item], list[str]]:
    """Сходящаяся схема дедупликации URL: re-select → insert → при гонке rollback
    и повторный re-select (bounded). Гарантирует: каждый нормализованный URL —
    либо существующий Item (duplicate), либо ровно один новый."""
    items: list[Item] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    pending: list[tuple[int, str]] = []
    for index, raw_url in enumerate(raw_urls):
        normalized = normalize_url(raw_url)
        if normalized in seen:
            continue  # повтор URL внутри одного сообщения
        seen.add(normalized)
        pending.append((index, normalized))

    for _ in range(3):  # bounded re-resolve: гонка не может длиться дольше
        still_missing: list[tuple[int, str]] = []
        for index, normalized in pending:
            existing = await session.scalar(
                select(Item).where(Item.user_id == user_id, Item.source_url == normalized)
            )
            if existing is not None:
                log.info("duplicate url ignored item_id=%s url=%s", existing.id, normalized)
                duplicates.append(normalized)
            else:
                still_missing.append((index, normalized))
        if not still_missing:
            return items, duplicates

        created = [
            _make_web_item(user_id, message_id, index, normalized, note)
            for index, normalized in still_missing
        ]
        for item in created:
            session.add(item)
        try:
            await session.commit()
            items.extend(created)
            return items, duplicates
        except IntegrityError:
            # Параллельный запрос успел сохранить часть URL'ов — пере-резолв.
            await session.rollback()

    # Сходиться не удалось за bounded число раундов — реальная проблема БД.
    raise IntegrityError("url dedup race did not converge", None, Exception())
