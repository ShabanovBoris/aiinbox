from datetime import datetime, timedelta

from sqlalchemy import text

from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.services.analysis import Analyzer
from app.services.processing import ProcessingPipeline
from app.services.retrieval import (
    TodayService,
    list_categories_page,
    list_category_items_page,
    list_inbox_page,
    resolve_category_token,
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
        session.add(
            Content(
                item_id=web_item.id,
                kind=ContentKind.ATTENTION_HOOK,
                text="purple-saturn-hook-word",
            )
        )
        await session.commit()

        assert (await search_items(session, user_id, "Compose"))[0].id == title_item.id
        assert (await search_items(session, user_id, "coroutines"))[0].id == transcript_item.id
        assert (await search_items(session, user_id, "SSRF"))[0].id == web_item.id
        assert await search_items(session, user_id, "purple-saturn-hook-word") == []
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
        inbox_page = await list_inbox_page(session, user_id)
        assert [item.title for item in inbox_page.items] == [
            "other category",
            "mine",
        ]
        categories = await list_categories_page(session, user_id)
        assert categories.categories == (("AI", 1), ("Piano", 1))
        category_items = await list_category_items_page(session, user_id, "AI")
        assert [item.title for item in category_items.items] == ["mine"]
        assert await resolve_category_token(session, user_id, "malformed") is None


async def test_inbox_category_item_and_category_pages_have_no_total_twenty_cap(session_factory):
    user_id = await make_user(session_factory)
    now = datetime(2026, 1, 1)
    async with session_factory() as session:
        session.add_all(
            [
                make_item(
                    user_id,
                    title=f"shared-{index}",
                    score=index,
                    category="Shared",
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(27)
            ]
            + [
                make_item(
                    user_id,
                    title=f"category-{index}",
                    category=f"Category {index:02}",
                    created_at=now + timedelta(days=1, seconds=index),
                )
                for index in range(23)
            ]
        )
        await session.commit()

        inbox_pages = [await list_inbox_page(session, user_id, page=page) for page in range(5)]
        inbox_items = [item for result in inbox_pages for item in result.items]
        assert [len(result.items) for result in inbox_pages] == [10, 10, 10, 10, 10]
        assert [result.has_next for result in inbox_pages] == [True, True, True, True, False]
        assert len({item.id for item in inbox_items}) == 50
        assert [item.title for item in inbox_items] == [
            item.title
            for item in sorted(
                inbox_items, key=lambda item: (item.created_at, item.id), reverse=True
            )
        ]

        category_pages = [
            await list_categories_page(session, user_id, page=page) for page in range(3)
        ]
        all_categories = [name for result in category_pages for name, _ in result.categories]
        assert [len(result.categories) for result in category_pages] == [10, 10, 4]
        assert len(set(all_categories)) == 24

        item_pages = [
            await list_category_items_page(session, user_id, "Shared", page=page)
            for page in range(3)
        ]
        shared_items = [item for result in item_pages for item in result.items]
        assert [len(result.items) for result in item_pages] == [10, 10, 7]
        assert len({item.id for item in shared_items}) == 27
        assert item_pages[-1].has_previous and not item_pages[-1].has_next

        # Stale and oversized callbacks resolve to the last valid page before OFFSET.
        stale = await list_inbox_page(session, user_id, page=10**40)
        assert stale.page == 4
        assert len(stale.items) == 10
        oversized_page = await list_inbox_page(session, user_id, page_size=10**6)
        assert len(oversized_page.items) <= 12
        assert (await list_category_items_page(session, user_id, "Shared", page=-1)).page == 0


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
