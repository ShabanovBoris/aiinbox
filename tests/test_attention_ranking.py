from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select

from app.bot.formatting import format_attention_item, format_attention_reason
from app.bot.handlers import on_attention
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.attention_ranking import (
    AttentionRank,
    AttentionRankingService,
    age_bonus,
    calculate_attention_rank,
    due_bonus,
    final_attention_score,
    interest_adjustment,
    neglect_bonus,
    recent_show_penalty,
    round_half_away_from_zero,
    stale_important_bonus,
)
from app.services.behaviour_ranking import BehaviourRank
from app.services.feedback import correct_item_category, correct_item_type
from app.services.retrieval import TodayService
from app.storage.models import Event, Item, ItemSource, User

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SERVICE = AttentionRankingService()


def _neutral_behaviour(personal_rank: int = 50) -> BehaviourRank:
    """Make an immutable PM-06 projection for isolated PM-07 formula tests."""
    return BehaviourRank(
        category_affinity=0.0,
        type_affinity=0.0,
        category_confidence=0.0,
        type_confidence=0.0,
        combined_affinity=0.0,
        confidence=0.0,
        adjustment_points=0,
        personal_rank=personal_rank,
        informative_event_count=0,
        category_informative_event_count=0,
        type_informative_event_count=0,
    )


def _formula_item(
    *,
    priority_score: int = 50,
    interest_level: int = 2,
    created_at: datetime = _NOW,
    due_at: datetime | None = None,
) -> Item:
    """Build a persisted-shape Item for deterministic scoring helper tests."""
    return Item(
        id=1,
        user_id=1,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
        priority_score=priority_score,
        interest_level=interest_level,
        created_at=created_at,
        suggested_due_at=due_at,
    )


async def _create_user(session_factory, telegram_user_id: int = 42) -> int:
    async with session_factory() as session:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id)
        session.add(user)
        await session.commit()
        return user.id


async def _create_item(
    session_factory,
    user_id: int,
    *,
    title: str = "Candidate",
    priority_score: int = 50,
    interest_level: int = 2,
    item_type: ItemType = ItemType.ACTION,
    state: ItemState = ItemState.ACTIVE,
    processing_status: ProcessingStatus = ProcessingStatus.READY,
    created_at: datetime = _NOW,
    due_at: datetime | None = None,
    source_type: SourceType = SourceType.TEXT,
) -> int:
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            telegram_message_id=None,
            source_index=0,
            processing_status=processing_status,
            state=state,
            source_type=source_type,
            processing_stage="READY",
            user_note="",
            title=title,
            summary=f"Summary for {title}",
            category="AI",
            item_type=item_type,
            priority_score=priority_score,
            interest_level=interest_level,
            suggested_due_at=due_at,
            created_at=created_at.replace(tzinfo=None) if created_at.tzinfo else created_at,
        )
        session.add(item)
        await session.commit()
        return item.id


def _message(user_id: int, text: str = "/attention") -> Message:
    return Message(
        message_id=900,
        date=_NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


@pytest.mark.parametrize(("level", "expected"), [(1, -8), (2, 0), (3, 8)])
def test_interest_adjustment_is_bounded_and_separate(level, expected):
    assert interest_adjustment(level) == expected


@pytest.mark.parametrize("level", [0, 4, -1])
def test_interest_adjustment_rejects_invalid_values(level):
    with pytest.raises(ValueError, match="interest_level"):
        interest_adjustment(level)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(days=6, hours=23, minutes=59), 0.0),
        (timedelta(days=7), 0.0),
        (timedelta(days=30), 4.0),
        (timedelta(days=90), 10.0),
        (timedelta(days=91), 12.0),
        (-timedelta(days=2), 0.0),
    ],
)
def test_age_bonus_exact_boundaries_and_future_age(age, expected):
    assert age_bonus(age) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(days=18, hours=12), 2.0), (timedelta(days=60), 7.0)],
)
def test_age_bonus_interpolates_inside_each_band(age, expected):
    assert age_bonus(age) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("since_shown", "age", "expected"),
    [
        (timedelta(days=6, hours=23, minutes=59), timedelta(days=60), 0),
        (timedelta(days=7), timedelta(days=60), 3),
        (timedelta(days=13, hours=23, minutes=59), timedelta(days=60), 3),
        (timedelta(days=14), timedelta(days=60), 6),
        (timedelta(days=30), timedelta(days=60), 6),
        (timedelta(days=30, seconds=1), timedelta(days=60), 10),
        (None, timedelta(days=13, hours=23, minutes=59), 0),
        (None, timedelta(days=14), 8),
    ],
)
def test_neglect_bonus_buckets_and_never_shown(since_shown, age, expected):
    assert neglect_bonus(since_shown, age) == expected


@pytest.mark.parametrize(
    ("since_shown", "expected"),
    [
        (None, 0),
        (timedelta(hours=23, minutes=59), -25),
        (timedelta(days=1), -15),
        (timedelta(days=3), -8),
        (timedelta(days=7), -3),
        (timedelta(days=14), 0),
    ],
)
def test_recent_show_penalty_has_exact_non_overlapping_boundaries(since_shown, expected):
    assert recent_show_penalty(since_shown) == expected


@pytest.mark.parametrize(
    ("due_at", "expected"),
    [
        (_NOW - timedelta(seconds=1), 10),
        (_NOW, 7),
        (_NOW + timedelta(days=3), 7),
        (_NOW + timedelta(days=3, seconds=1), 4),
        (_NOW + timedelta(days=7), 4),
        (_NOW + timedelta(days=7, seconds=1), 0),
        (None, 0),
    ],
)
def test_due_bonus_exact_boundaries(due_at, expected):
    assert due_bonus(due_at, _NOW) == expected


def test_due_bonus_normalizes_sqlite_naive_timestamp_as_utc():
    assert due_bonus(datetime(2026, 1, 1, 0, 0), _NOW) == 7


@pytest.mark.parametrize(
    ("priority", "age", "since_shown", "expected"),
    [
        (74, timedelta(days=60), None, 0),
        (75, timedelta(days=29, hours=23), None, 0),
        (75, timedelta(days=30), None, 8),
        (75, timedelta(days=60), timedelta(days=13, hours=23), 0),
        (75, timedelta(days=60), timedelta(days=14), 8),
    ],
)
def test_stale_important_bonus_uses_semantic_priority_and_exposure_gap(
    priority, age, since_shown, expected
):
    assert stale_important_bonus(priority, age, since_shown) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(10.5, 11), (9.5, 10), (100.5, 100), (-10.5, 0), (0.5, 1)],
)
def test_final_score_rounds_half_away_then_clamps(raw, expected):
    assert final_attention_score(raw) == expected


def test_rounding_is_symmetric_for_positive_and_negative_ties():
    assert round_half_away_from_zero(2.5) == 3
    assert round_half_away_from_zero(-2.5) == -3


def test_future_age_is_zero_and_recent_exposure_suppresses_old_item():
    future_item = _formula_item(priority_score=80, created_at=_NOW + timedelta(days=2))
    future_rank = calculate_attention_rank(
        future_item, _neutral_behaviour(80), _NOW - timedelta(hours=2), _NOW
    )
    old_item = _formula_item(priority_score=80, created_at=_NOW - timedelta(days=100))
    old_rank = calculate_attention_rank(
        old_item, _neutral_behaviour(80), _NOW - timedelta(hours=2), _NOW
    )

    assert future_rank.age_days == 0.0
    assert future_rank.age_bonus == 0.0
    assert old_rank.age_bonus == 12
    assert old_rank.recent_show_penalty == -25
    assert old_rank.stale_important_bonus == 0
    assert old_rank.score == 67


async def test_attention_closes_database_session_before_telegram_send(
    settings, session_factory, engine, monkeypatch
):
    user_id = await _create_user(session_factory)
    await _create_item(session_factory, user_id)
    checked_out: set[int] = set()

    def checkout(connection, _record, _proxy):
        checked_out.add(id(connection))

    def checkin(connection, _record):
        checked_out.discard(id(connection))

    async def answer(self, text, **kwargs):
        assert not checked_out

    monkeypatch.setattr(Message, "answer", answer)
    sqlalchemy_event.listen(engine.sync_engine.pool, "checkout", checkout)
    sqlalchemy_event.listen(engine.sync_engine.pool, "checkin", checkin)
    try:
        await on_attention(_message(42), settings, session_factory)
    finally:
        sqlalchemy_event.remove(engine.sync_engine.pool, "checkout", checkout)
        sqlalchemy_event.remove(engine.sync_engine.pool, "checkin", checkin)


async def test_attention_score_changes_with_time_without_db_mutation(session_factory):
    user_id = await _create_user(session_factory)
    item_id = await _create_item(
        session_factory, user_id, priority_score=80, created_at=datetime(2026, 1, 1)
    )

    async with session_factory() as session:
        jan_rank = (await _SERVICE.list_ranked(session, user_id, now=_NOW))[0][1]
    async with session_factory() as session:
        feb_rank = (
            await _SERVICE.list_ranked(session, user_id, now=datetime(2026, 2, 20, tzinfo=UTC))
        )[0][1]
    async with session_factory() as session:
        item = await session.get(Item, item_id)

    assert feb_rank.score > jan_rank.score
    assert feb_rank.age_bonus != jan_rank.age_bonus
    assert feb_rank.neglect_bonus != jan_rank.neglect_bonus
    assert item.priority_score == 80
    assert item.interest_level == 2


async def test_attention_includes_only_ready_active_actionable_types(session_factory):
    user_id = await _create_user(session_factory)
    included = [
        await _create_item(session_factory, user_id, item_type=item_type, title=item_type.value)
        for item_type in (ItemType.ACTION, ItemType.LEARN, ItemType.READ, ItemType.WATCH)
    ]
    excluded = [
        await _create_item(session_factory, user_id, item_type=item_type, title=item_type.value)
        for item_type in (ItemType.REFERENCE, ItemType.IDEA, ItemType.SOMEDAY)
    ]
    excluded += [
        await _create_item(session_factory, user_id, state=ItemState.SNOOZED, title="snoozed"),
        await _create_item(session_factory, user_id, state=ItemState.DONE, title="done"),
        await _create_item(session_factory, user_id, state=ItemState.ARCHIVED, title="archived"),
        await _create_item(
            session_factory,
            user_id,
            processing_status=ProcessingStatus.QUEUED,
            title="queued",
        ),
        await _create_item(
            session_factory,
            user_id,
            processing_status=ProcessingStatus.PROCESSING,
            title="processing",
        ),
        await _create_item(
            session_factory,
            user_id,
            processing_status=ProcessingStatus.FAILED,
            title="failed",
        ),
    ]

    async with session_factory() as session:
        ranked = await _SERVICE.list_ranked(session, user_id, limit=99, now=_NOW)

    ranked_ids = {item.id for item, _ in ranked}
    assert ranked_ids == set(included)
    assert not ranked_ids.intersection(excluded)


async def test_attention_limit_defaults_to_three_and_caps_at_five(session_factory):
    user_id = await _create_user(session_factory)
    for score in range(50, 56):
        await _create_item(session_factory, user_id, priority_score=score)

    async with session_factory() as session:
        default = await _SERVICE.list_ranked(session, user_id, now=_NOW)
        one = await _SERVICE.list_ranked(session, user_id, limit=1, now=_NOW)
        five = await _SERVICE.list_ranked(session, user_id, limit=5, now=_NOW)
        capped = await _SERVICE.list_ranked(session, user_id, limit=99, now=_NOW)
        zero = await _SERVICE.list_ranked(session, user_id, limit=0, now=_NOW)

    assert len(default) == 3
    assert len(one) == 1
    assert len(five) == len(capped) == 5
    assert zero == []


async def test_no_behaviour_history_uses_priority_as_personal_rank(session_factory):
    user_id = await _create_user(session_factory)
    item_id = await _create_item(session_factory, user_id, priority_score=73)

    async with session_factory() as session:
        ((item, rank),) = await _SERVICE.list_ranked(session, user_id, now=_NOW)

    assert item.id == item_id
    assert rank.personal_rank == 73
    assert rank.behaviour_rank.adjustment_points == 0
    assert rank.score == 73


async def test_rank_item_matches_batch_candidate_projection(session_factory):
    """The PM-08 point revalidation path uses the same PM-07 projection as batch ranking."""
    user_id = await _create_user(session_factory)
    item_id = await _create_item(session_factory, user_id, priority_score=74)
    async with session_factory() as session:
        session.add_all(
            [
                Event(
                    user_id=user_id,
                    item_id=item_id,
                    event_type="USEFUL",
                    created_at=_NOW - timedelta(days=20),
                ),
                Event(
                    user_id=user_id,
                    item_id=item_id,
                    event_type="ATTENTION_SHOWN",
                    created_at=_NOW - timedelta(days=3),
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        batch = await _SERVICE.list_candidates(session, user_id, now=_NOW)
        single = await _SERVICE.rank_item(session, user_id, item_id, now=_NOW)

    assert single is not None
    assert single == batch[0]


async def test_pm06_canonical_corrections_feed_attention_without_double_counting(
    session_factory,
):
    user_id = await _create_user(session_factory, telegram_user_id=4200)
    candidate_id = await _create_item(
        session_factory,
        user_id,
        title="Corrected candidate",
        priority_score=61,
        item_type=ItemType.REFERENCE,
    )
    history_id = await _create_item(
        session_factory,
        user_id,
        title="Useful AI item",
        item_type=ItemType.LEARN,
        state=ItemState.DONE,
    )
    async with session_factory() as session:
        session.add(
            Event(
                user_id=user_id,
                item_id=history_id,
                event_type="USEFUL",
                created_at=_NOW.replace(tzinfo=None),
            )
        )
        await session.commit()

    await correct_item_category(
        session_factory,
        4200,
        candidate_id,
        "AI",
        idempotency_key="pm07-correct-category",
    )
    await correct_item_type(
        session_factory,
        4200,
        candidate_id,
        ItemType.LEARN,
        idempotency_key="pm07-correct-type",
    )

    async with session_factory() as session:
        ((item, rank),) = await _SERVICE.list_ranked(session, user_id, now=_NOW)

    assert item.id == candidate_id
    assert rank.behaviour_rank.category_affinity > 0
    assert rank.behaviour_rank.type_affinity > 0
    assert rank.behaviour_rank.adjustment_points > 0
    assert rank.personal_rank == 61 + rank.behaviour_rank.adjustment_points
    component_sum = (
        rank.personal_rank
        + rank.interest_adjustment
        + rank.age_bonus
        + rank.neglect_bonus
        + rank.due_bonus
        + rank.stale_important_bonus
        + rank.recent_show_penalty
    )
    assert rank.score == final_attention_score(component_sum)
    assert rank.score != final_attention_score(
        component_sum + rank.behaviour_rank.adjustment_points
    )


async def test_exposure_and_behaviour_history_are_user_scoped(session_factory):
    user_a = await _create_user(session_factory, telegram_user_id=42)
    user_b = await _create_user(session_factory, telegram_user_id=1000)
    item_today = await _create_item(session_factory, user_a, created_at=_NOW)
    item_attention = await _create_item(session_factory, user_a, created_at=_NOW)
    candidate_b = await _create_item(session_factory, user_b, created_at=_NOW)
    history_a = await _create_item(session_factory, user_a, created_at=_NOW)
    shown_at = _NOW - timedelta(hours=2)
    async with session_factory() as session:
        session.add_all(
            [
                Event(
                    user_id=user_a,
                    item_id=item_today,
                    event_type="TODAY_SHOWN",
                    created_at=shown_at.replace(tzinfo=None),
                ),
                Event(
                    user_id=user_a,
                    item_id=item_attention,
                    event_type="ATTENTION_SHOWN",
                    created_at=shown_at.replace(tzinfo=None),
                ),
                Event(
                    user_id=user_a,
                    item_id=history_a,
                    event_type="USEFUL",
                    created_at=_NOW.replace(tzinfo=None),
                ),
                Event(
                    user_id=user_a,
                    item_id=candidate_b,
                    event_type="TODAY_SHOWN",
                    created_at=shown_at.replace(tzinfo=None),
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        ranks_a = await _SERVICE.list_ranked(session, user_a, limit=5, now=_NOW)
    async with session_factory() as session:
        ranks_b = await _SERVICE.list_ranked(session, user_b, now=_NOW)

    by_id_a = {item.id: rank for item, rank in ranks_a}
    assert by_id_a[item_today].last_shown_at == shown_at
    assert by_id_a[item_attention].last_shown_at == shown_at
    assert by_id_a[item_today].recent_show_penalty == -25
    assert by_id_a[item_attention].recent_show_penalty == -25
    assert by_id_a[item_today].personal_rank > by_id_a[item_today].priority_score
    rank_b = ranks_b[0][1]
    assert rank_b.last_shown_at is None
    assert rank_b.recent_show_penalty == 0
    assert rank_b.personal_rank == rank_b.priority_score


async def test_attention_order_has_priority_created_at_and_id_tie_breaks(session_factory):
    user_id = await _create_user(session_factory)
    high_attention = await _create_item(
        session_factory, user_id, priority_score=79, interest_level=3
    )
    higher_priority = await _create_item(
        session_factory, user_id, priority_score=75, created_at=_NOW
    )
    lower_priority = await _create_item(
        session_factory, user_id, priority_score=67, interest_level=3, created_at=_NOW
    )
    newer = await _create_item(session_factory, user_id, priority_score=60, created_at=_NOW)
    older = await _create_item(
        session_factory, user_id, priority_score=60, created_at=_NOW - timedelta(days=2)
    )

    async with session_factory() as session:
        ranked = await _SERVICE.list_ranked(session, user_id, limit=99, now=_NOW)

    assert [item.id for item, _ in ranked] == [
        high_attention,
        higher_priority,
        lower_priority,
        older,
        newer,
    ]
    assert ranked[0][1].score > ranked[1][1].score
    assert ranked[1][1].score == ranked[2][1].score
    assert ranked[1][1].priority_score > ranked[2][1].priority_score
    assert ranked[3][1].score == ranked[4][1].score
    assert older > newer


async def test_attention_uses_id_for_exact_final_tie(session_factory):
    user_id = await _create_user(session_factory)
    first_id = await _create_item(session_factory, user_id, priority_score=60, created_at=_NOW)
    second_id = await _create_item(session_factory, user_id, priority_score=60, created_at=_NOW)

    async with session_factory() as session:
        ranked = await _SERVICE.list_ranked(session, user_id, now=_NOW)

    assert [item.id for item, _ in ranked] == [first_id, second_id]


async def test_old_neglected_item_can_outrank_newer_priority_and_today_is_unchanged(
    session_factory,
):
    user_id = await _create_user(session_factory)
    newer = await _create_item(
        session_factory, user_id, title="New high priority", priority_score=90, created_at=_NOW
    )
    older = await _create_item(
        session_factory,
        user_id,
        title="Old neglected",
        priority_score=80,
        interest_level=3,
        created_at=_NOW - timedelta(days=60),
    )

    async with session_factory() as session:
        today = await TodayService().list_items(session, user_id)
        attention = await _SERVICE.list_ranked(session, user_id, now=_NOW)

    assert [item.id for item in today] == [newer, older]
    assert attention[0][0].id == older
    assert attention[0][1].score == 100


async def test_attention_uses_constant_number_of_candidate_and_event_queries(
    session_factory, engine
):
    user_id = await _create_user(session_factory)
    for index in range(20):
        await _create_item(session_factory, user_id, title=f"Item {index}")
    selects: list[str] = []

    def count_selects(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select"):
            selects.append(statement.lower())

    async with session_factory() as session:
        sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
        try:
            ranked = await _SERVICE.list_ranked(session, user_id, now=_NOW)
        finally:
            sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", count_selects)

    assert len(ranked) == 3
    assert len(selects) == 6
    assert sum("from events" in statement for statement in selects) == 4
    exposure_query = next(
        statement for statement in selects if "max(events.created_at)" in statement
    )
    assert "events.user_id =" in exposure_query
    assert "events.item_id in" in exposure_query


async def test_repeated_attention_suppresses_successfully_shown_items(
    settings, session_factory, monkeypatch
):
    user_id = await _create_user(session_factory)
    item_ids = [
        await _create_item(
            session_factory,
            user_id,
            title=f"Card {index}",
            created_at=_NOW - timedelta(days=10),
        )
        for index in range(6)
    ]
    sent = []

    async def answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    await on_attention(_message(42), settings, session_factory)
    first_shown = {event.item_id for event in await _get_attention_events(session_factory)}
    await on_attention(_message(42), settings, session_factory)
    events = await _get_attention_events(session_factory)
    cards = [text for text in sent if "/3 —" in text]
    second_shown = {event.item_id for event in events} - first_shown

    assert len(cards) == 6
    assert len(first_shown) == len(second_shown) == 3
    assert first_shown.isdisjoint(second_shown)
    assert {event.item_id for event in events} <= set(item_ids)


async def test_failed_telegram_card_does_not_record_its_exposure(
    settings, session_factory, monkeypatch
):
    user_id = await _create_user(session_factory)
    item_ids = [
        await _create_item(session_factory, user_id, title=f"Card {index}") for index in range(3)
    ]

    async def answer(self, text, **kwargs):
        if text.startswith("2/3 —"):
            raise RuntimeError("Telegram send failed")

    monkeypatch.setattr(Message, "answer", answer)
    with pytest.raises(RuntimeError, match="Telegram send failed"):
        await on_attention(_message(42), settings, session_factory)

    events = await _get_attention_events(session_factory)
    assert len(events) == 1
    assert events[0].item_id == item_ids[0]


async def test_attention_preview_keeps_item_source_actions_and_canonical_state(
    settings, session_factory, monkeypatch
):
    user_id = await _create_user(session_factory)
    item_id = await _create_item(
        session_factory,
        user_id,
        title="Video source",
        priority_score=80,
        interest_level=3,
        source_type=SourceType.YOUTUBE,
    )
    async with session_factory() as session:
        session.add(
            ItemSource(
                item_id=item_id,
                source_index=0,
                source_type=SourceType.YOUTUBE,
                source_url="https://youtube.com/watch?v=source",
                extraction_status="READY",
            )
        )
        await session.commit()
    delivered = []

    async def answer(self, text, **kwargs):
        delivered.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", answer)
    await on_attention(_message(42), settings, session_factory)
    card, markup = next((text, markup) for text, markup in delivered if "Video source" in text)
    labels = {button.text for row in markup.inline_keyboard for button in row}
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        events = (await session.scalars(select(Event).where(Event.item_id == item_id))).all()

    assert "Внимание:" in card and "Приоритет: 80/100" in card
    assert "Интерес: 3/3" in card and "Возраст:" in card and "Почему сейчас:" in card
    assert {"✅ Done", "⏰ Later", "🗄 Archive", "📹 Отправить YouTube", "🔗 Открыть"} <= labels
    assert item.priority_score == 80
    assert item.interest_level == 3
    assert item.state is ItemState.ACTIVE
    assert [event.event_type for event in events] == ["ATTENTION_SHOWN"]


async def test_empty_attention_result_has_no_exposure_event(settings, session_factory, monkeypatch):
    sent = []

    async def answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    await on_attention(_message(42), settings, session_factory)

    assert sent == ["Сейчас нет подходящих Items."]
    assert await _get_attention_events(session_factory) == []


@pytest.mark.parametrize("arguments", ["0", "6", "abc", "2 3", "²"])
async def test_attention_rejects_invalid_limit(settings, session_factory, monkeypatch, arguments):
    sent = []

    async def answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    await on_attention(_message(42), settings, session_factory, arguments)

    assert sent == ["Использование: /attention [1-5]"]
    async with session_factory() as session:
        assert await session.scalar(select(User)) is None


async def _get_attention_events(session_factory) -> list[Event]:
    """Read PM-07's durable exposure history for command integration tests."""
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(Event).where(Event.event_type == "ATTENTION_SHOWN").order_by(Event.id)
                )
            ).all()
        )


def test_attention_reason_is_deterministic_and_grounded_in_score_components():
    rank = AttentionRank(
        item_id=1,
        score=91,
        priority_score=82,
        behaviour_rank=_neutral_behaviour(82),
        interest_adjustment=8,
        age_bonus=4.0,
        neglect_bonus=6,
        due_bonus=0,
        stale_important_bonus=0,
        recent_show_penalty=0,
        last_shown_at=_NOW - timedelta(days=19),
        age_days=47.0,
        days_since_shown=19.0,
    )
    item = _formula_item(priority_score=82, interest_level=3)
    reason = format_attention_reason(rank)
    formatted = format_attention_item(1, 1, item, rank)

    assert reason == format_attention_reason(rank)
    assert reason == "Не показывался 19 дн.; Высокий приоритет; Высокий интерес"
    assert "Без названия" in formatted
    assert "Внимание: 91/100" in formatted
    assert "Приоритет: 82/100" in formatted
    assert "Интерес: 3/3" in formatted
    assert "Возраст: 47 дн." in formatted
    assert reason in formatted


# The fallback must identify a calculated score without claiming it is high.
def test_attention_reason_fallback_describes_the_actual_calculated_score():
    rank = AttentionRank(
        item_id=1,
        score=20,
        priority_score=20,
        behaviour_rank=_neutral_behaviour(20),
        interest_adjustment=0,
        age_bonus=0.0,
        neglect_bonus=0,
        due_bonus=0,
        stale_important_bonus=0,
        recent_show_penalty=0,
        last_shown_at=None,
        age_days=1.0,
        days_since_shown=None,
    )

    assert format_attention_reason(rank) == "По рассчитанному рейтингу"


async def test_attention_does_not_change_today_or_semantic_item_fields(
    settings, session_factory, monkeypatch
):
    user_id = await _create_user(session_factory)
    now = datetime.now(UTC)
    new_item = await _create_item(
        session_factory, user_id, title="New high priority", priority_score=90, created_at=now
    )
    old_item = await _create_item(
        session_factory,
        user_id,
        title="Old useful item",
        priority_score=80,
        interest_level=3,
        created_at=now - timedelta(days=60),
    )
    sent = []

    async def answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    await on_attention(_message(42), settings, session_factory)
    async with session_factory() as session:
        today = await TodayService().list_items(session, user_id)
        stored_new = await session.get(Item, new_item)
        stored_old = await session.get(Item, old_item)
        events = (
            await session.scalars(select(Event).where(Event.item_id.in_([new_item, old_item])))
        ).all()

    assert [item.id for item in today] == [new_item, old_item]
    assert next(i for i, text in enumerate(sent) if "Old useful item" in text) < next(
        i for i, text in enumerate(sent) if "New high priority" in text
    )
    assert stored_new.priority_score == 90 and stored_old.priority_score == 80
    assert stored_new.interest_level == 2 and stored_old.interest_level == 3
    assert {event.event_type for event in events} == {"ATTENTION_SHOWN"}


async def test_attention_loads_item_sources_in_one_batch_query(
    settings, session_factory, engine, monkeypatch
):
    user_id = await _create_user(session_factory)
    for index in range(5):
        await _create_item(session_factory, user_id, title=f"Source candidate {index}")
    source_selects = []

    def count_source_selects(_connection, _cursor, statement, _parameters, _context, _many):
        lowered = statement.lower()
        if lowered.lstrip().startswith("select") and "from item_sources" in lowered:
            source_selects.append(lowered)

    async def answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer)
    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", count_source_selects)
    try:
        await on_attention(_message(42), settings, session_factory)
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", count_source_selects)

    assert len(source_selects) == 1
