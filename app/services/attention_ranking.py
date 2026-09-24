"""Dynamic PM-07 ranking over canonical Item facts and durable exposure Events."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, ProcessingStatus
from app.services.behaviour_ranking import BehaviourAffinityService, BehaviourRank
from app.storage.models import Event, Item

_DAY = timedelta(days=1)
_EXPOSURE_EVENT_TYPES = ("TODAY_SHOWN", "ATTENTION_SHOWN")
_DEFAULT_LIMIT = 3
_MAX_LIMIT = 5


@dataclass(frozen=True)
class AttentionRank:
    """Immutable score projection retaining PM-06 and every PM-07 component."""

    item_id: int
    score: int
    priority_score: int
    behaviour_rank: BehaviourRank
    interest_adjustment: int
    age_bonus: float
    neglect_bonus: int
    due_bonus: int
    stale_important_bonus: int
    recent_show_penalty: int
    last_shown_at: datetime | None
    age_days: float
    days_since_shown: float | None

    @property
    def personal_rank(self) -> int:
        """Expose the canonical PM-06 base without recalculating its formula."""
        return self.behaviour_rank.personal_rank


def interest_adjustment(level: int) -> int:
    """Keep manual interest a small PM-07 signal separate from PM-06 affinity."""
    adjustments = {1: -8, 2: 0, 3: 8}
    try:
        return adjustments[level]
    except KeyError as exc:
        raise ValueError("interest_level must be 1, 2, or 3") from exc


def age_bonus(age: timedelta) -> float:
    """Interpolate the policy's age bands while treating future Items as age zero."""
    age_days = max(0.0, age.total_seconds() / _DAY.total_seconds())
    if age_days < 7:
        return 0.0
    if age_days <= 30:
        return (age_days - 7) * 4 / 23
    if age_days <= 90:
        return 4 + (age_days - 30) * 6 / 60
    return 12.0


def neglect_bonus(since_last_shown: timedelta | None, age: timedelta) -> int:
    """Reward older unshown Items and progressively neglected resurfaced ones."""
    if since_last_shown is None:
        return 8 if age >= timedelta(days=14) else 0
    elapsed = max(timedelta(0), since_last_shown)
    if elapsed < timedelta(days=7):
        return 0
    if elapsed < timedelta(days=14):
        return 3
    if elapsed <= timedelta(days=30):
        return 6
    return 10


def recent_show_penalty(since_last_shown: timedelta | None) -> int:
    """Suppress repeated previews using mutually exclusive elapsed-time buckets."""
    if since_last_shown is None:
        return 0
    elapsed = max(timedelta(0), since_last_shown)
    if elapsed < _DAY:
        return -25
    if elapsed < timedelta(days=3):
        return -15
    if elapsed < timedelta(days=7):
        return -8
    if elapsed < timedelta(days=14):
        return -3
    return 0


def due_bonus(due_at: datetime | None, now: datetime) -> int:
    """Apply pressure only to the persisted suggested due date."""
    if due_at is None:
        return 0
    due_utc = _as_utc(due_at)
    now_utc = _as_utc(now)
    if due_utc < now_utc:
        return 10
    remaining = due_utc - now_utc
    if remaining <= timedelta(days=3):
        return 7
    if remaining <= timedelta(days=7):
        return 4
    return 0


def stale_important_bonus(
    priority_score: int,
    age: timedelta,
    since_last_shown: timedelta | None,
) -> int:
    """Restore stale semantic priorities only after the full 14-day exposure gap."""
    not_recently_shown = since_last_shown is None or max(
        timedelta(0), since_last_shown
    ) >= timedelta(days=14)
    return 8 if priority_score >= 75 and age >= timedelta(days=30) and not_recently_shown else 0


def round_half_away_from_zero(value: float) -> int:
    """Match PM-06's symmetric tie handling instead of Python's bankers rounding."""
    return floor(value + 0.5) if value >= 0 else ceil(value - 0.5)


def final_attention_score(raw_score: float) -> int:
    """Round deterministically, then bound the derived presentation score to 0..100."""
    return max(0, min(100, round_half_away_from_zero(raw_score)))


def _as_utc(value: datetime) -> datetime:
    """Interpret SQLite naive DateTimes as UTC, matching the PM-06 convention."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def calculate_attention_rank(
    item: Item,
    behaviour_rank: BehaviourRank,
    last_shown_at: datetime | None,
    now: datetime,
) -> AttentionRank:
    """Project a persisted candidate into a score without mutating canonical Item data."""
    if item.id is None or item.priority_score is None or item.created_at is None:
        raise ValueError("candidate must be persisted with created_at and priority_score")

    now_utc = _as_utc(now)
    created_at = _as_utc(item.created_at)
    age = max(timedelta(0), now_utc - created_at)
    last_shown = _as_utc(last_shown_at) if last_shown_at is not None else None
    since_last_shown = max(timedelta(0), now_utc - last_shown) if last_shown is not None else None

    interest_points = interest_adjustment(item.interest_level)
    age_points = age_bonus(age)
    neglect_points = neglect_bonus(since_last_shown, age)
    due_points = due_bonus(item.suggested_due_at, now_utc)
    stale_points = stale_important_bonus(item.priority_score, age, since_last_shown)
    recent_penalty = recent_show_penalty(since_last_shown)
    raw_score = (
        behaviour_rank.personal_rank
        + interest_points
        + age_points
        + neglect_points
        + due_points
        + stale_points
        + recent_penalty
    )
    return AttentionRank(
        item_id=item.id,
        score=final_attention_score(raw_score),
        priority_score=item.priority_score,
        behaviour_rank=behaviour_rank,
        interest_adjustment=interest_points,
        age_bonus=age_points,
        neglect_bonus=neglect_points,
        due_bonus=due_points,
        stale_important_bonus=stale_points,
        recent_show_penalty=recent_penalty,
        last_shown_at=last_shown,
        age_days=age.total_seconds() / _DAY.total_seconds(),
        days_since_shown=(
            since_last_shown.total_seconds() / _DAY.total_seconds()
            if since_last_shown is not None
            else None
        ),
    )


class AttentionRankingService:
    """Rank actionable Items on demand; Telegram delivery and exposure writes stay outside."""

    async def list_ranked(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[tuple[Item, AttentionRank]]:
        result_limit = max(0, min(_DEFAULT_LIMIT if limit is None else limit, _MAX_LIMIT))
        if result_limit == 0:
            return []

        return await self.list_candidates(session, user_id, limit=result_limit, now=now)

    async def list_candidates(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[tuple[Item, AttentionRank]]:
        """Return PM-07's ranked candidates independently of Telegram's five-card limit.

        The scheduler needs a wider shortlist so a cooled-down preview leader does
        not hide the next eligible Item; `/attention` keeps using `list_ranked`.
        """
        if limit is not None and limit <= 0:
            return []
        captured_now = _as_utc(now or datetime.now(UTC))

        candidates = list(
            (
                await session.scalars(
                    select(Item).where(
                        Item.user_id == user_id,
                        Item.processing_status == ProcessingStatus.READY,
                        Item.state == ItemState.ACTIVE,
                        Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
                    )
                )
            ).all()
        )
        if not candidates:
            return []

        behaviour_ranks = await BehaviourAffinityService().rank_items(
            session, user_id, candidates, now=captured_now
        )
        candidate_ids = [item.id for item in candidates]
        exposures = await session.execute(
            select(Event.item_id, func.max(Event.created_at))
            .where(
                Event.user_id == user_id,
                Event.item_id.in_(candidate_ids),
                Event.event_type.in_(_EXPOSURE_EVENT_TYPES),
            )
            .group_by(Event.item_id)
        )
        last_shown_by_item = {
            item_id: _as_utc(created_at) for item_id, created_at in exposures.all()
        }

        ranked = [
            (
                item,
                calculate_attention_rank(
                    item,
                    behaviour_ranks[item.id],
                    last_shown_by_item.get(item.id),
                    captured_now,
                ),
            )
            for item in candidates
        ]
        ranked.sort(
            key=lambda pair: (
                -pair[1].score,
                -pair[1].priority_score,
                _as_utc(pair[0].created_at),
                pair[0].id,
            )
        )
        return ranked if limit is None else ranked[:limit]

    async def rank_item(
        self,
        session: AsyncSession,
        user_id: int,
        item_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[Item, AttentionRank] | None:
        """Recompute one actionable Item's live rank for a serialized send check.

        PM-08 uses this after acquiring its short prepare transaction: a single
        current candidate check avoids ranking every Item while SQLite writers
        are paused, while the same PM-06 and PM-07 calculations remain canonical.
        """
        captured_now = _as_utc(now or datetime.now(UTC))
        item = await session.scalar(
            select(Item).where(
                Item.id == item_id,
                Item.user_id == user_id,
                Item.processing_status == ProcessingStatus.READY,
                Item.state == ItemState.ACTIVE,
                Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
            )
        )
        if item is None:
            return None

        behaviour_rank = await BehaviourAffinityService().rank_item(
            session, user_id, item, now=captured_now
        )
        last_shown_at = await session.scalar(
            select(func.max(Event.created_at)).where(
                Event.user_id == user_id,
                Event.item_id == item_id,
                Event.event_type.in_(_EXPOSURE_EVENT_TYPES),
            )
        )
        rank = calculate_attention_rank(item, behaviour_rank, last_shown_at, captured_now)
        return item, rank
