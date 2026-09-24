from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.services.behaviour_ranking import BehaviourAffinityService, BehaviourRank
from app.services.feedback import correct_item_category, correct_item_type, record_item_feedback
from app.storage.models import Event, Item, User

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SERVICE = BehaviourAffinityService()


def _database_time(value: datetime) -> datetime:
    """Match the application's naive-UTC SQLite DateTime convention in fixtures."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


async def _create_user(session_factory, telegram_user_id: int = 42) -> int:
    """Create the owner required to exercise the real user-scoped Event schema."""
    async with session_factory() as session:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id)
        session.add(user)
        await session.commit()
        return user.id


async def _create_items(
    session_factory,
    user_id: int,
    count: int,
    *,
    category: str | None,
    item_type: ItemType | None,
    priority_score: int = 50,
    interest_level: int = 2,
    state: ItemState = ItemState.ACTIVE,
    processing_status: ProcessingStatus = ProcessingStatus.READY,
) -> list[int]:
    """Persist compact candidates/history rows without changing production paths."""
    async with session_factory() as session:
        items = [
            Item(
                user_id=user_id,
                telegram_message_id=None,
                source_index=index,
                processing_status=processing_status,
                state=state,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="",
                category=category,
                item_type=item_type,
                priority_score=priority_score,
                interest_level=interest_level,
            )
            for index in range(count)
        ]
        session.add_all(items)
        await session.flush()
        item_ids = [item.id for item in items]
        await session.commit()
        return item_ids


async def _add_events(
    session_factory,
    events: list[tuple[int, str, datetime]],
    *,
    user_id_override: int | None = None,
) -> None:
    """Insert synthetic chronology while keeping each ordinary Event owner correct."""
    if not events:
        return
    async with session_factory() as session:
        item_ids = {item_id for item_id, _, _ in events}
        items = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
        owners = {item.id: item.user_id for item in items}
        session.add_all(
            [
                Event(
                    user_id=user_id_override or owners[item_id],
                    item_id=item_id,
                    event_type=event_type,
                    created_at=_database_time(created_at),
                )
                for item_id, event_type, created_at in events
            ]
        )
        await session.commit()


async def _rank_items(
    session_factory, user_id: int, item_ids: list[int]
) -> dict[int, BehaviourRank]:
    """Load candidates and exercise the production batch ranking boundary."""
    async with session_factory() as session:
        items = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
        return await _SERVICE.rank_items(session, user_id, items, now=_NOW)


async def _rank_item(session_factory, user_id: int, item_id: int) -> BehaviourRank:
    """Exercise the single-item convenience API with the same deterministic clock."""
    return (await _rank_items(session_factory, user_id, [item_id]))[item_id]


async def test_no_history_returns_neutral_affinity_and_preserves_priority(session_factory):
    user_id = await _create_user(session_factory)
    (candidate_id,) = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.LEARN, priority_score=73
    )

    result = await _rank_item(session_factory, user_id, candidate_id)

    assert result.category_affinity == 0.0
    assert result.type_affinity == 0.0
    assert result.combined_affinity == 0.0
    assert result.confidence == 0.0
    assert result.adjustment_points == 0
    assert result.personal_rank == 73
    assert result.informative_event_count == 0


@pytest.mark.parametrize(("event_type", "sign"), [("USEFUL", 1), ("NOT_INTERESTING", -1)])
async def test_one_explicit_event_is_strongly_smoothed(session_factory, event_type, sign):
    user_id = await _create_user(session_factory)
    candidate_id, history_id = await _create_items(
        session_factory,
        user_id,
        2,
        category="AI",
        item_type=None,
        priority_score=50,
    )
    await _add_events(session_factory, [(history_id, event_type, _NOW)])

    result = await _rank_item(session_factory, user_id, candidate_id)

    assert result.category_affinity == pytest.approx(sign / 9)
    assert -1.0 <= result.category_affinity <= 1.0
    assert result.category_confidence == pytest.approx(1 / 9)
    assert result.combined_affinity == pytest.approx(sign * 0.7 / 9)
    assert result.adjustment_points == sign
    assert result.personal_rank == 50 + sign
    assert result.informative_event_count == 1


@pytest.mark.parametrize("sign", [1, -1])
async def test_sparse_smoothing_grows_with_distinct_items(session_factory, sign):
    user_id = await _create_user(session_factory)
    candidate_id = await _create_items(session_factory, user_id, 1, category="AI", item_type=None)
    history_ids = await _create_items(session_factory, user_id, 20, category="AI", item_type=None)
    event_type = "USEFUL" if sign > 0 else "NOT_INTERESTING"
    await _add_events(session_factory, [(item_id, event_type, _NOW) for item_id in history_ids])

    result = await _rank_item(session_factory, user_id, candidate_id[0])

    assert result.category_affinity == pytest.approx(sign * 20 / 28)
    assert result.category_confidence == pytest.approx(20 / 28)
    assert result.combined_affinity == pytest.approx(sign * 0.5)
    assert result.adjustment_points == sign * 8
    assert result.informative_event_count == 20


async def test_mixed_signals_follow_weighted_formula(session_factory):
    user_id = await _create_user(session_factory)
    candidate_id, *history_ids = await _create_items(
        session_factory, user_id, 5, category="AI", item_type=None
    )
    signal_types = ["USEFUL", "NOT_INTERESTING", "DONE", "SNOOZED"]
    await _add_events(
        session_factory,
        [(item_id, signal, _NOW) for item_id, signal in zip(history_ids, signal_types)],
    )

    result = await _rank_item(session_factory, user_id, candidate_id)

    raw = (1.0 - 1.0 + 0.35 - 0.15) / (1.0 + 1.0 + 0.35 + 0.15)
    assert result.category_affinity == pytest.approx(raw * (4 / 12))
    assert result.category_informative_event_count == 4


async def test_explicit_feedback_outweighs_weak_lifecycle_inference(session_factory):
    user_id = await _create_user(session_factory)
    categories = [
        "Explicit positive",
        "Lifecycle positive",
        "Explicit negative",
        "Lifecycle negative",
    ]
    candidate_ids = [
        (await _create_items(session_factory, user_id, 1, category=name, item_type=None))[0]
        for name in categories
    ]
    history_ids = [
        (await _create_items(session_factory, user_id, 1, category=name, item_type=None))[0]
        for name in categories
        for _ in range(2)
    ]
    signals = [
        "USEFUL",
        "SNOOZED",
        "DONE",
        "SNOOZED",
        "USEFUL",
        "NOT_INTERESTING",
        "USEFUL",
        "SNOOZED",
    ]
    event_rows = []
    for category_index, category in enumerate(categories):
        start = category_index * 2
        for item_id, signal in zip(history_ids[start : start + 2], signals[start : start + 2]):
            event_rows.append((item_id, signal, _NOW))
    await _add_events(session_factory, event_rows)

    ranks = await _rank_items(session_factory, user_id, candidate_ids)

    explicit_positive = ranks[candidate_ids[0]].category_affinity
    lifecycle_positive = ranks[candidate_ids[1]].category_affinity
    explicit_negative = ranks[candidate_ids[2]].category_affinity
    lifecycle_negative = ranks[candidate_ids[3]].category_affinity
    assert explicit_positive > lifecycle_positive
    assert explicit_negative < lifecycle_negative


async def test_latest_family_signals_use_event_id_as_equal_time_tiebreak(session_factory):
    user_id = await _create_user(session_factory)
    candidate_id, sentiment_id, priority_id = await _create_items(
        session_factory, user_id, 3, category="AI", item_type=None
    )
    await _add_events(
        session_factory,
        [
            (sentiment_id, "USEFUL", _NOW),
            (sentiment_id, "NOT_INTERESTING", _NOW),
            (priority_id, "PRIORITY_HIGHER", _NOW),
            (priority_id, "PRIORITY_LOWER", _NOW),
            (priority_id, "PRIORITY_HIGHER", _NOW),
        ],
    )

    result = await _rank_item(session_factory, user_id, candidate_id)

    assert result.category_informative_event_count == 2
    assert result.category_affinity == pytest.approx(-0.05)


async def test_signal_families_are_independent_terminal_is_collapsed_and_snoozes_capped(
    session_factory,
):
    user_id = await _create_user(session_factory)
    candidate_id, history_id = await _create_items(
        session_factory, user_id, 2, category="AI", item_type=None
    )
    history = [
        (history_id, "USEFUL", _NOW - timedelta(minutes=12)),
        (history_id, "PRIORITY_HIGHER", _NOW - timedelta(minutes=11)),
        (history_id, "PRIORITY_LOWER", _NOW - timedelta(minutes=10)),
        (history_id, "PRIORITY_HIGHER", _NOW - timedelta(minutes=9)),
        (history_id, "DONE", _NOW - timedelta(minutes=8)),
        (history_id, "ARCHIVED", _NOW - timedelta(minutes=7)),
    ]
    history.extend(
        (history_id, "SNOOZED", _NOW - timedelta(days=age)) for age in (200, 190, 180, 120, 60, 0)
    )
    await _add_events(session_factory, history)

    result = await _rank_item(session_factory, user_id, candidate_id)

    expected_raw = (1.0 + 0.60 - 0.10 - 0.15 * (1.0 + 0.75 + 0.50)) / (
        1.0 + 0.60 + 0.10 + 0.15 * (1.0 + 0.75 + 0.50)
    )
    assert result.category_informative_event_count == 6
    assert result.category_affinity == pytest.approx(expected_raw * (6 / 14))


@pytest.mark.parametrize(
    ("negative_age_days", "recency"), [(30, 1.0), (31, 0.75), (90, 0.75), (91, 0.50)]
)
async def test_recency_bucket_boundaries_are_exact(session_factory, negative_age_days, recency):
    user_id = await _create_user(session_factory)
    candidate_id, positive_id, negative_id = await _create_items(
        session_factory, user_id, 3, category="AI", item_type=None
    )
    await _add_events(
        session_factory,
        [
            (positive_id, "USEFUL", _NOW),
            (negative_id, "NOT_INTERESTING", _NOW - timedelta(days=negative_age_days)),
        ],
    )

    result = await _rank_item(session_factory, user_id, candidate_id)

    raw = (1.0 - recency) / (1.0 + recency)
    assert result.category_affinity == pytest.approx(raw * (2 / 10))
    if negative_age_days == 91:
        assert result.category_affinity > 0.0


async def test_category_type_blend_and_overall_confidence_are_explainable(session_factory):
    user_id = await _create_user(session_factory)
    candidate_id = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.LEARN
    )
    category_history = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.ACTION
    )
    type_history = await _create_items(
        session_factory, user_id, 3, category="Other", item_type=ItemType.LEARN
    )
    await _add_events(
        session_factory,
        [(category_history[0], "USEFUL", _NOW)]
        + [(item_id, "NOT_INTERESTING", _NOW) for item_id in type_history],
    )

    result = await _rank_item(session_factory, user_id, candidate_id[0])

    assert result.category_affinity == pytest.approx(1 / 9)
    assert result.type_affinity == pytest.approx(-3 / 11)
    assert result.combined_affinity == pytest.approx((1 / 9) * 0.7 + (-3 / 11) * 0.3)
    assert result.category_confidence == pytest.approx(1 / 9)
    assert result.type_confidence == pytest.approx(3 / 11)
    assert result.confidence == pytest.approx((1 / 9) * 0.7 + (3 / 11) * 0.3)
    assert result.informative_event_count == 4


async def test_missing_dimensions_and_interest_level_do_not_change_affinity(session_factory):
    user_id = await _create_user(session_factory)
    missing_category, low_interest, high_interest, history_id = await _create_items(
        session_factory,
        user_id,
        4,
        category=None,
        item_type=ItemType.LEARN,
        interest_level=1,
    )
    async with session_factory() as session:
        high = await session.get(Item, high_interest)
        high.category = "AI"
        high.interest_level = 3
        session.add(high)
        normal = await session.get(Item, low_interest)
        normal.category = "AI"
        session.add(normal)
        history = await session.get(Item, history_id)
        history.category = "Other"
        history.item_type = ItemType.LEARN
        session.add(history)
        await session.commit()
    await _add_events(session_factory, [(history_id, "USEFUL", _NOW)])

    ranks = await _rank_items(
        session_factory, user_id, [missing_category, low_interest, high_interest]
    )

    assert ranks[missing_category].category_affinity == 0.0
    assert ranks[missing_category].category_confidence == 0.0
    assert ranks[missing_category].type_affinity == pytest.approx(1 / 9)
    assert ranks[low_interest].category_affinity == pytest.approx(
        ranks[high_interest].category_affinity
    )
    assert ranks[low_interest].type_affinity == pytest.approx(ranks[high_interest].type_affinity)


async def test_priority_score_changes_only_the_base_personal_rank(session_factory):
    user_id = await _create_user(session_factory)
    low_priority, high_priority, history_id = await _create_items(
        session_factory,
        user_id,
        3,
        category="AI",
        item_type=ItemType.LEARN,
        priority_score=20,
        interest_level=1,
    )
    async with session_factory() as session:
        high = await session.get(Item, high_priority)
        high.priority_score = 90
        high.interest_level = 3
        session.add(high)
        await session.commit()
    await _add_events(session_factory, [(history_id, "USEFUL", _NOW)])

    ranks = await _rank_items(session_factory, user_id, [low_priority, high_priority])

    assert ranks[low_priority].category_affinity == pytest.approx(
        ranks[high_priority].category_affinity
    )
    assert ranks[low_priority].type_affinity == pytest.approx(ranks[high_priority].type_affinity)
    assert ranks[high_priority].personal_rank - ranks[low_priority].personal_rank == 70


@pytest.mark.parametrize(
    "event_type",
    [
        "CREATED",
        "TODAY_SHOWN",
        "RETRIED",
        "INTEREST_CHANGED",
        "CATEGORY_CORRECTED",
        "TYPE_CORRECTED",
        "SUMMARY_REPORTED_WRONG",
        "ATTENTION_SHOWN",
        "SOME_FUTURE_EVENT",
    ],
)
async def test_zero_weight_and_unknown_events_do_not_add_evidence(session_factory, event_type):
    user_id = await _create_user(session_factory)
    candidate_id, history_id = await _create_items(
        session_factory, user_id, 2, category="AI", item_type=None
    )
    await _add_events(session_factory, [(history_id, event_type, _NOW)])

    result = await _rank_item(session_factory, user_id, candidate_id)

    assert result.category_affinity == 0.0
    assert result.category_confidence == 0.0
    assert result.informative_event_count == 0


async def test_history_is_isolated_by_owner_even_for_mismatched_event_item_pair(session_factory):
    user_a = await _create_user(session_factory, 42)
    user_b = await _create_user(session_factory, 84)
    candidate_a = await _create_items(session_factory, user_a, 1, category="AI", item_type=None)
    candidate_b = await _create_items(session_factory, user_b, 1, category="AI", item_type=None)
    history_a = await _create_items(session_factory, user_a, 1, category="AI", item_type=None)
    await _add_events(session_factory, [(history_a[0], "USEFUL", _NOW)])
    await _add_events(
        session_factory,
        [(candidate_b[0], "USEFUL", _NOW)],
        user_id_override=user_a,
    )

    rank_a = await _rank_item(session_factory, user_a, candidate_a[0])
    rank_b = await _rank_item(session_factory, user_b, candidate_b[0])

    assert rank_a.category_affinity == pytest.approx(1 / 9)
    assert rank_b.category_affinity == 0.0


async def test_batch_query_is_constant_for_candidates_sharing_dimensions(session_factory, engine):
    user_id = await _create_user(session_factory)
    candidate_ids = await _create_items(
        session_factory, user_id, 20, category="AI", item_type=ItemType.LEARN
    )
    history_id = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.LEARN
    )
    await _add_events(session_factory, [(history_id[0], "USEFUL", _NOW)])
    select_queries: list[str] = []
    event_queries: list[str] = []

    def count_event_selects(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select"):
            select_queries.append(statement)
        if "from events" in statement.lower():
            event_queries.append(statement)

    async with session_factory() as session:
        candidates = (await session.scalars(select(Item).where(Item.id.in_(candidate_ids)))).all()
        sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", count_event_selects)
        try:
            ranks = await _SERVICE.rank_items(session, user_id, candidates, now=_NOW)
        finally:
            sqlalchemy_event.remove(
                engine.sync_engine, "before_cursor_execute", count_event_selects
            )

    assert len(ranks) == 20
    assert len(select_queries) == 2
    assert len(event_queries) == 1
    assert len({rank.category_affinity for rank in ranks.values()}) == 1


async def test_half_points_round_away_from_zero_and_rank_clamps(session_factory):
    user_id = await _create_user(session_factory)
    positive_candidate, negative_candidate = await _create_items(
        session_factory,
        user_id,
        2,
        category="Positive",
        item_type=None,
        priority_score=95,
    )
    async with session_factory() as session:
        negative = await session.get(Item, negative_candidate)
        negative.category = "Negative"
        negative.item_type = None
        negative.priority_score = 5
        session.add(negative)
        await session.commit()
    positive_history = await _create_items(
        session_factory, user_id, 13, category="Positive", item_type=None
    )
    negative_history = await _create_items(
        session_factory, user_id, 13, category="Negative", item_type=None
    )
    await _add_events(
        session_factory,
        [(item_id, "USEFUL", _NOW) for item_id in positive_history]
        + [(item_id, "NOT_INTERESTING", _NOW) for item_id in negative_history],
    )

    ranks = await _rank_items(session_factory, user_id, [positive_candidate, negative_candidate])

    assert ranks[positive_candidate].combined_affinity * 15 == pytest.approx(6.5)
    assert ranks[positive_candidate].adjustment_points == 7
    assert ranks[positive_candidate].personal_rank == 100
    assert ranks[negative_candidate].combined_affinity * 15 == pytest.approx(-6.5)
    assert ranks[negative_candidate].adjustment_points == -7
    assert ranks[negative_candidate].personal_rank == 0


async def test_dense_category_and_type_history_caps_adjustment_at_fifteen(session_factory):
    user_id = await _create_user(session_factory)
    positive_candidate, negative_candidate = await _create_items(
        session_factory,
        user_id,
        2,
        category="Positive",
        item_type=ItemType.LEARN,
        priority_score=95,
    )
    async with session_factory() as session:
        negative = await session.get(Item, negative_candidate)
        negative.category = "Negative"
        negative.item_type = ItemType.ACTION
        negative.priority_score = 5
        session.add(negative)
        await session.commit()
    positive_history = await _create_items(
        session_factory, user_id, 240, category="Positive", item_type=ItemType.LEARN
    )
    negative_history = await _create_items(
        session_factory, user_id, 240, category="Negative", item_type=ItemType.ACTION
    )
    await _add_events(
        session_factory,
        [(item_id, "USEFUL", _NOW) for item_id in positive_history]
        + [(item_id, "NOT_INTERESTING", _NOW) for item_id in negative_history],
    )

    ranks = await _rank_items(session_factory, user_id, [positive_candidate, negative_candidate])

    assert ranks[positive_candidate].category_affinity == pytest.approx(240 / 248)
    assert ranks[positive_candidate].type_affinity == pytest.approx(240 / 248)
    assert ranks[positive_candidate].adjustment_points == 15
    assert ranks[positive_candidate].personal_rank == 100
    assert ranks[negative_candidate].adjustment_points == -15
    assert ranks[negative_candidate].personal_rank == 0


async def test_pm05_feedback_corrections_and_lifecycle_events_use_current_dimensions(
    session_factory,
):
    telegram_user_id = 4200
    user_id = await _create_user(session_factory, telegram_user_id)
    candidate_id = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.LEARN, priority_score=70
    )
    corrected_id = await _create_items(
        session_factory,
        user_id,
        1,
        category="Programming",
        item_type=ItemType.REFERENCE,
        priority_score=61,
    )
    done_id = await _create_items(
        session_factory,
        user_id,
        1,
        category="AI",
        item_type=ItemType.ACTION,
        priority_score=55,
    )
    archived_id = await _create_items(
        session_factory,
        user_id,
        1,
        category="AI",
        item_type=ItemType.READ,
        priority_score=45,
        processing_status=ProcessingStatus.FAILED,
    )

    await record_item_feedback(
        session_factory,
        telegram_user_id,
        corrected_id[0],
        "USEFUL",
        idempotency_key="pm06-useful",
    )
    await correct_item_category(
        session_factory,
        telegram_user_id,
        corrected_id[0],
        "AI",
        idempotency_key="pm06-category",
    )
    await correct_item_type(
        session_factory,
        telegram_user_id,
        corrected_id[0],
        ItemType.LEARN,
        idempotency_key="pm06-type",
    )
    await apply_item_action(session_factory, telegram_user_id, done_id[0], "done")
    await apply_item_action(session_factory, telegram_user_id, archived_id[0], "archive")

    result = await _rank_item(session_factory, user_id, candidate_id[0])

    assert result.category_informative_event_count == 3
    assert result.type_informative_event_count == 1
    assert result.category_affinity > 0.0
    assert result.type_affinity == pytest.approx(1 / 9)
    assert result.personal_rank >= 70
    async with session_factory() as session:
        corrected = await session.get(Item, corrected_id[0])
        assert corrected.category == "AI"
        assert corrected.item_type is ItemType.LEARN
        assert corrected.priority_score == 61


async def test_candidate_uses_fresh_canonical_metadata_after_correction(session_factory):
    telegram_user_id = 4200
    user_id = await _create_user(session_factory, telegram_user_id)
    candidate_id = await _create_items(
        session_factory,
        user_id,
        1,
        category="Programming",
        item_type=ItemType.REFERENCE,
    )
    history_id = await _create_items(
        session_factory, user_id, 1, category="AI", item_type=ItemType.LEARN
    )
    await _add_events(session_factory, [(history_id[0], "USEFUL", _NOW)])
    async with session_factory() as session:
        stale_candidate = await session.get(Item, candidate_id[0])
        assert stale_candidate.category == "Programming"
        assert stale_candidate.item_type is ItemType.REFERENCE

    await correct_item_category(
        session_factory,
        telegram_user_id,
        candidate_id[0],
        "AI",
        idempotency_key="pm06-stale-category",
    )
    await correct_item_type(
        session_factory,
        telegram_user_id,
        candidate_id[0],
        ItemType.LEARN,
        idempotency_key="pm06-stale-type",
    )

    async with session_factory() as session:
        result = await _SERVICE.rank_item(session, user_id, stale_candidate, now=_NOW)

    assert result.category_affinity == pytest.approx(1 / 9)
    assert result.type_affinity == pytest.approx(1 / 9)
