import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.services.ingestion import ingest_message
from app.services.url_parsing import normalize_url, parse_message
from app.storage.models import Content, Event, Item, ItemSource, User


async def ingest(session_factory, message_id: int = 1, text: str = "Изучить AI agents"):
    return await ingest_message(
        session_factory, telegram_user_id=42, chat_id=42, message_id=message_id, text=text
    )


async def test_text_message_creates_queued_item(session_factory):
    result = await ingest(session_factory)
    item = result.items[0]
    assert item.id is not None
    assert item.processing_status is ProcessingStatus.QUEUED
    assert item.source_type is SourceType.TEXT
    assert item.source_url is None
    assert item.user_note == "Изучить AI agents"
    assert item.error_code is None


async def test_duplicate_update_returns_same_item(session_factory):
    first = await ingest(session_factory)
    second = await ingest(session_factory)
    assert second.items[0].id == first.items[0].id
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.event_type == "CREATED")
            )
            == 1
        )


async def test_url_message_creates_web_item_with_note(session_factory):
    result = await ingest(
        session_factory,
        text="Полезная статья про архитектуру https://Example.com/a?utm_source=x&id=7",
    )
    item = result.items[0]
    assert item.source_type is SourceType.WEB
    assert item.source_url == "https://example.com/a?id=7"
    assert item.user_note == "Полезная статья про архитектуру"
    async with session_factory() as session:
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert source is not None
        assert source.source_type is SourceType.WEB
        assert source.source_url == "https://example.com/a?id=7"
        source_text = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.USER_TEXT,
            )
        )
        assert source_text == (
            "Полезная статья про архитектуру https://Example.com/a?utm_source=x&id=7"
        )


async def test_multiple_urls_create_one_item_with_child_sources(session_factory):
    result = await ingest(
        session_factory,
        text="Две статьи https://example.com/one и https://example.com/two",
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_type is SourceType.TEXT
    assert item.source_url is None
    assert item.user_note == "Две статьи и"
    async with session_factory() as session:
        sources = (
            await session.scalars(
                select(ItemSource)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        ).all()
        assert [(source.source_index, source.source_url) for source in sources] == [
            (0, "https://example.com/one"),
            (1, "https://example.com/two"),
        ]


async def test_same_url_in_different_messages_keeps_both_message_items(session_factory):
    first = await ingest(session_factory, message_id=1, text="https://example.com/a")
    second = await ingest(session_factory, message_id=2, text="https://example.com/a")
    assert first.items[0].id != second.items[0].id
    assert second.duplicates == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 2
        assert await session.scalar(select(func.count()).select_from(ItemSource)) == 2
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.event_type == "CREATED")
            )
            == 2
        )


async def test_repeated_url_inside_single_message_deduplicated(session_factory):
    result = await ingest(
        session_factory,
        message_id=1,
        text="https://example.com/a и ещё раз https://example.com/a",
    )
    assert len(result.items) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ItemSource)
                .where(ItemSource.item_id == result.items[0].id)
            )
            == 1
        )


async def test_item_source_index_unique_per_item_at_db_level(session_factory):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=1,
            source_index=0,
            processing_status=ProcessingStatus.QUEUED,
            source_type=SourceType.TEXT,
            user_note="",
        )
        session.add(item)
        await session.flush()
        session.add(
            ItemSource(
                item_id=item.id,
                source_index=0,
                source_type=SourceType.WEB,
                source_url="https://example.com/one",
            )
        )
        await session.commit()
        session.add(
            ItemSource(
                item_id=item.id,
                source_index=0,
                source_type=SourceType.WEB,
                source_url="https://example.com/two",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_user_created_once_for_repeated_updates(session_factory):
    await ingest(session_factory, message_id=1, text="a")
    await ingest(session_factory, message_id=2, text="b")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_parallel_first_messages_create_one_user_and_all_items(session_factory):
    # Регрессия гонки get_or_create_user: два одновременных первых сообщения
    # нового пользователя не должны терять Item — итог: 1 User + 2 Items.
    results = await asyncio.gather(
        ingest_message(
            session_factory, telegram_user_id=777, chat_id=777, message_id=1, text="first"
        ),
        ingest_message(
            session_factory, telegram_user_id=777, chat_id=777, message_id=2, text="second"
        ),
    )
    all_items = [item for result in results for item in result.items]
    assert len(all_items) == 2
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1
        assert await session.scalar(select(func.count()).select_from(Item)) == 2


async def test_item_with_unknown_user_rejected_by_db(session_factory):
    # FK должен enforcement'иться БД (pragma foreign_keys=ON), а не только логикой.
    async with session_factory() as session:
        session.add(
            Item(
                user_id=999_999,
                telegram_message_id=1,
                source_index=0,
                source_type=SourceType.TEXT,
                user_note="orphan",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


def test_normalize_url_strips_tracking_and_fragment():
    normalized = normalize_url("HTTPS://Example.COM/path/?utm_source=tg&id=7&fbclid=abc#section")
    assert normalized == "https://example.com/path/?id=7"  # trailing slash сохранён


def test_normalize_url_keeps_meaningful_query():
    assert (
        normalize_url("https://example.com/search?q=compose+recomposition")
        == "https://example.com/search?q=compose+recomposition"
    )


def test_normalize_url_removes_all_utm_parameters_but_keeps_instagram_identity_query():
    assert (
        normalize_url("https://instagram.com/reel/ABC/?utm_id=campaign&fbclid=tracking&igsh=share")
        == "https://instagram.com/reel/ABC/?igsh=share"
    )


def test_parse_message_extracts_note_and_urls():
    note, urls = parse_message("Надо изучить, интересная архитектура https://example.com/a.")
    assert urls == ["https://example.com/a"]
    assert note == "Надо изучить, интересная архитектура"


def test_parse_message_without_urls():
    note, urls = parse_message("Просто мысль без ссылок")
    assert urls == []
    assert note == "Просто мысль без ссылок"


async def test_concurrent_overlapping_url_dedup_converges(session_factory):
    # URL identity is message-local: overlapping links do not merge two distinct posts.
    results = await asyncio.gather(
        ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text="https://example.com/a и https://example.com/b",
        ),
        ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=2,
            text="https://example.com/b и https://example.com/c",
        ),
    )
    assert sum(len(result.items) for result in results) == 2
    async with session_factory() as session:
        urls = list(await session.scalars(select(ItemSource.source_url).order_by(ItemSource.id)))
        assert sorted(urls) == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/b",
            "https://example.com/c",
        ]
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.event_type == "CREATED")
            )
            == 2
        )


async def test_concurrent_youtube_url_resolve_preserves_source_type(session_factory):
    # Child source routing remains semantic even when the Item itself is composite TEXT.
    results = await asyncio.gather(
        ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=10,
            text="https://www.youtube.com/watch?v=shared https://www.youtube.com/watch?v=first",
        ),
        ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=11,
            text="https://www.youtube.com/watch?v=shared https://www.youtube.com/watch?v=second",
        ),
    )
    items = [item for result in results for item in result.items]
    assert len(items) == 2
    assert all(item.source_type is SourceType.TEXT for item in items)
    async with session_factory() as session:
        sources = (await session.scalars(select(ItemSource))).all()
        assert len(sources) == 4
        assert all(source.source_type is SourceType.YOUTUBE for source in sources)
