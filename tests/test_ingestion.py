import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.enums import ItemState, ProcessingStatus, SourceType
from app.services.ingestion import ingest_text
from app.storage.models import Item, User


async def test_authorized_text_creates_queued_item(session_factory):
    item = await ingest_text(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=7,
        text="Изучить AI agents",
    )
    assert item.id is not None
    assert item.processing_status is ProcessingStatus.QUEUED
    assert item.source_type is SourceType.TEXT
    assert item.state is ItemState.ACTIVE
    assert item.processing_stage == "INGESTED"
    assert item.user_note == "Изучить AI agents"
    assert item.error_code is None


async def test_duplicate_update_returns_same_item(session_factory):
    first = await ingest_text(
        session_factory, telegram_user_id=42, chat_id=42, message_id=7, text="Изучить AI agents"
    )
    second = await ingest_text(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=7,
        text="Изучить AI agents",
    )
    assert second.id == first.id
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 1


async def test_same_message_with_different_source_index_creates_items(session_factory):
    first = await ingest_text(
        session_factory, telegram_user_id=42, chat_id=42, message_id=7, text="a", source_index=0
    )
    second = await ingest_text(
        session_factory, telegram_user_id=42, chat_id=42, message_id=7, text="b", source_index=1
    )
    assert first.id != second.id


async def test_unique_constraint_enforced_at_db_level(session_factory):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        base = dict(
            user_id=user.id,
            telegram_message_id=1,
            source_index=0,
            source_type=SourceType.TEXT,
            user_note="a",
        )
        session.add(Item(**base))
        await session.commit()
        session.add(Item(**base))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_user_created_once_for_repeated_updates(session_factory):
    await ingest_text(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="a")
    await ingest_text(session_factory, telegram_user_id=42, chat_id=42, message_id=2, text="b")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_parallel_first_messages_create_one_user_and_all_items(session_factory):
    # Регрессия гонки get_or_create_user: два одновременных первых сообщения
    # нового пользователя не должны терять Item — итог: 1 User + 2 Items.
    results = await asyncio.gather(
        ingest_text(session_factory, telegram_user_id=777, chat_id=777, message_id=1, text="first"),
        ingest_text(
            session_factory, telegram_user_id=777, chat_id=777, message_id=2, text="second"
        ),
    )
    assert len({item.id for item in results}) == 2
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
