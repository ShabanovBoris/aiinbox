from datetime import datetime, timedelta

from sqlalchemy import text

from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.services.analysis import Analyzer
from app.services.processing import ProcessingPipeline
from app.services.retrieval import (
    TodayService,
    list_categories,
    list_category_items,
    list_inbox,
    search_items,
)
from app.storage.models import Content, Item, User
from tests.fakes import FakeLlmProvider


async def make_user(session_factory, telegram_user_id: int = 42) -> int:
    async with session_factory() as session:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id)
        session.add(user)
        await session.commit()
        return user.id


def make_item(
    user_id: int,
    *,
    title: str,
    score: int = 50,
    item_type: ItemType | None = ItemType.ACTION,
    state: ItemState = ItemState.ACTIVE,
    status: ProcessingStatus = ProcessingStatus.READY,
    category: str = "AI",
    created_at: datetime | None = None,
    note: str = "",
) -> Item:
    return Item(
        user_id=user_id,
        telegram_message_id=None,
        source_index=0,
        processing_status=status,
        state=state,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note=note,
        title=title,
        summary=f"Summary for {title}",
        category=category,
        item_type=item_type,
        tags_json=["agents"],
        priority_score=score,
        estimated_action_minutes=25,
        next_action=f"Next step for {title}",
        created_at=created_at,
    )


async def test_today_filters_and_sorts_with_absolute_limit(session_factory):
    user_id = await make_user(session_factory)
    now = datetime.now()
    async with session_factory() as session:
        eligible_high = make_item(user_id, title="high", score=90, created_at=now)
        eligible_old = make_item(
            user_id, title="old", score=90, created_at=now - timedelta(minutes=1)
        )
        excluded_done = make_item(
            user_id, title="done", state=ItemState.DONE, score=100, created_at=now
        )
        excluded_snoozed = make_item(
            user_id, title="snoozed", state=ItemState.SNOOZED, score=100, created_at=now
        )
        excluded_reference = make_item(
            user_id, title="reference", item_type=ItemType.REFERENCE, score=100, created_at=now
        )
        excluded_not_ready = make_item(
            user_id, title="queued", status=ProcessingStatus.QUEUED, score=100, created_at=now
        )
        session.add_all(
            [
                eligible_high,
                eligible_old,
                excluded_done,
                excluded_snoozed,
                excluded_reference,
                excluded_not_ready,
            ]
        )
        await session.commit()
        items = await TodayService().list_items(session, user_id, limit=99)

    assert [item.title for item in items] == ["old", "high"]
    assert len(items) <= 5


async def test_today_default_limit_is_three(session_factory):
    user_id = await make_user(session_factory)
    async with session_factory() as session:
        session.add_all(
            [
                make_item(user_id, title=str(i), score=100 - i, created_at=datetime.now())
                for i in range(6)
            ]
        )
        await session.commit()
        items = await TodayService().list_items(session, user_id)
    assert len(items) == 3


async def test_fts_finds_title_transcript_web_text_and_archived(session_factory):
    user_id = await make_user(session_factory)
    async with session_factory() as session:
        title_item = make_item(user_id, title="Compose recomposition", note="architecture")
        transcript_item = make_item(user_id, title="Voice note", note="saved")
        web_item = make_item(user_id, title="Article", note="saved")
        archived_item = make_item(
            user_id, title="Archived decision", state=ItemState.ARCHIVED, note="keep searchable"
        )
        session.add_all([title_item, transcript_item, web_item, archived_item])
        await session.flush()
        session.add(
            Content(
                item_id=transcript_item.id,
                kind=ContentKind.TRANSCRIPT,
                text="Kotlin coroutines",
            )
        )
        session.add(Content(item_id=web_item.id, kind=ContentKind.WEB_TEXT, text="SSRF protection"))
        await session.commit()

        assert (await search_items(session, user_id, "Compose"))[0].id == title_item.id
        assert (await search_items(session, user_id, "coroutines"))[0].id == transcript_item.id
        assert (await search_items(session, user_id, "SSRF"))[0].id == web_item.id
        assert (await search_items(session, user_id, "decision"))[0].id == archived_item.id


async def test_inbox_and_categories_are_user_scoped(session_factory):
    user_id = await make_user(session_factory)
    other_id = await make_user(session_factory, telegram_user_id=1000)
    async with session_factory() as session:
        session.add(make_item(user_id, title="mine", category="AI", created_at=datetime.now()))
        session.add(
            make_item(user_id, title="other category", category="Piano", created_at=datetime.now())
        )
        session.add(
            make_item(other_id, title="other user", category="AI", created_at=datetime.now())
        )
        await session.commit()
        assert [item.title for item in await list_inbox(session, user_id)] == [
            "other category",
            "mine",
        ]
        assert await list_categories(session, user_id) == [("AI", 1), ("Piano", 1)]
        assert [item.title for item in await list_category_items(session, user_id, "AI")] == [
            "mine"
        ]


async def test_processing_ready_commit_updates_fts_projection(session_factory):
    user_id = await make_user(session_factory)
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            telegram_message_id=1,
            source_index=0,
            processing_status=ProcessingStatus.PROCESSING,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="INGESTED",
            user_note="unique projection phrase",
        )
        session.add(item)
        await session.commit()
        await ProcessingPipeline(Analyzer(FakeLlmProvider()), PriorityEngine()).run(session, item)
        row = await session.execute(
            text("SELECT item_id FROM item_search WHERE item_search MATCH :query"),
            {"query": '"projection"'},
        )
    assert row.scalar_one() == item.id
