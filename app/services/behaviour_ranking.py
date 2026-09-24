"""On-demand, explainable ranking derived from canonical Items and Events."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil, floor
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ItemType
from app.storage.models import Event, Item

SIGNAL_WEIGHTS: dict[str, float] = {
    "CREATED": 0.0,
    "TODAY_SHOWN": 0.0,
    "DONE": 0.35,
    "SNOOZED": -0.15,
    "ARCHIVED": -0.10,
    "RETRIED": 0.0,
    "INTEREST_CHANGED": 0.0,
    "USEFUL": 1.0,
    "NOT_INTERESTING": -1.0,
    "CATEGORY_CORRECTED": 0.0,
    "TYPE_CORRECTED": 0.0,
    "PRIORITY_HIGHER": 0.60,
    "PRIORITY_LOWER": -0.60,
    "SUMMARY_REPORTED_WRONG": 0.0,
}
SPARSE_HISTORY_K = 8
CATEGORY_WEIGHT = 0.70
TYPE_WEIGHT = 0.30
MAX_BEHAVIOUR_ADJUSTMENT = 15
MIN_AFFINITY = -1.0
MAX_AFFINITY = 1.0
MIN_PERSONAL_RANK = 0
MAX_PERSONAL_RANK = 100
MAX_SNOOZE_EVENTS_PER_ITEM = 3

_INFORMATIVE_EVENT_TYPES = tuple(
    event_type for event_type, weight in SIGNAL_WEIGHTS.items() if weight != 0.0
)
_SENTIMENT_EVENT_TYPES = frozenset({"USEFUL", "NOT_INTERESTING"})
_PRIORITY_EVENT_TYPES = frozenset({"PRIORITY_HIGHER", "PRIORITY_LOWER"})
_TERMINAL_EVENT_TYPES = frozenset({"DONE", "ARCHIVED"})


@dataclass(frozen=True)
class DimensionAffinity:
    """Expose one smoothed dimension so callers can explain the derived score."""

    affinity: float
    confidence: float
    informative_event_count: int


@dataclass(frozen=True)
class BehaviourRank:
    """Immutable projection, with the total counting distinct Events across both dimensions."""

    category_affinity: float
    type_affinity: float
    category_confidence: float
    type_confidence: float
    combined_affinity: float
    confidence: float
    adjustment_points: int
    personal_rank: int
    informative_event_count: int
    category_informative_event_count: int
    type_informative_event_count: int


@dataclass
class _DimensionAccumulator:
    """Collect weighted evidence for one current category or ItemType."""

    weighted_signal_sum: float = 0.0
    event_ids: set[int] = field(default_factory=set)

    def add(self, event_id: int, effective_weight: float) -> None:
        """Keep weighted evidence and distinct informative Events together."""
        self.weighted_signal_sum += effective_weight
        self.event_ids.add(event_id)

    def result(self) -> DimensionAffinity:
        """Normalize and smooth this dimension before exposing it to callers."""
        count = len(self.event_ids)
        if count == 0:
            return DimensionAffinity(affinity=0.0, confidence=0.0, informative_event_count=0)

        # ❌ Удалена нормализация по сумме модулей: она стирала силу сигналов и recency.
        # Среднее по числу событий сохраняет policy weights; K отдельно сглаживает редкую историю.
        raw = self.weighted_signal_sum / count
        confidence = count / (count + SPARSE_HISTORY_K)
        affinity = max(MIN_AFFINITY, min(MAX_AFFINITY, raw * confidence))
        return DimensionAffinity(
            affinity=affinity,
            confidence=confidence,
            informative_event_count=count,
        )


def _as_utc(value: datetime) -> datetime:
    """Treat SQLite's naive timestamps as UTC and normalize aware timestamps."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recency_multiplier(created_at: datetime, now: datetime) -> float:
    """Apply the documented age buckets without dropping old evidence."""
    age = _as_utc(now) - _as_utc(created_at)
    if age <= timedelta(days=30):
        return 1.0
    if age <= timedelta(days=90):
        return 0.75
    return 0.50


def _round_half_away_from_zero(value: float) -> int:
    """Resolve score ties consistently instead of relying on bankers rounding."""
    if value >= 0:
        return floor(value + 0.5)
    return ceil(value - 0.5)


def _collapse_item_events(
    events: list[tuple[int, str, datetime]],
) -> list[tuple[int, str, datetime]]:
    """Limit one Item's repeated feedback while preserving independent signal families."""
    ordered = sorted(events, key=lambda event: (_as_utc(event[2]), event[0]))
    latest_sentiment = None
    latest_priority = None
    latest_terminal = None
    snoozes: list[tuple[int, str, datetime]] = []

    for event in ordered:
        event_type = event[1]
        if event_type in _SENTIMENT_EVENT_TYPES:
            latest_sentiment = event
        elif event_type in _PRIORITY_EVENT_TYPES:
            latest_priority = event
        elif event_type in _TERMINAL_EVENT_TYPES:
            latest_terminal = event
        elif event_type == "SNOOZED":
            snoozes.append(event)

    selected = [
        event for event in (latest_sentiment, latest_priority, latest_terminal) if event is not None
    ]
    selected.extend(snoozes[-MAX_SNOOZE_EVENTS_PER_ITEM:])
    return sorted(selected, key=lambda event: (_as_utc(event[2]), event[0]))


class BehaviourAffinityService:
    """Batch application boundary for user-scoped, derived behavioural ranking.

    The single history query reads current Item dimensions and durable Events;
    the service only returns a projection and never writes ranking into storage.
    """

    async def rank_item(
        self,
        session: AsyncSession,
        user_id: int,
        item: Item,
        *,
        now: datetime | None = None,
    ) -> BehaviourRank:
        """Rank one persisted candidate through the same path as batch ranking."""
        return (await self.rank_items(session, user_id, [item], now=now))[item.id]

    async def rank_items(
        self,
        session: AsyncSession,
        user_id: int,
        items: Sequence[Item],
        *,
        now: datetime | None = None,
    ) -> dict[int, BehaviourRank]:
        """Rank candidates with one user-scoped history read and pure aggregation.

        Candidate IDs and the clock are captured before the first await. The
        database then supplies canonical candidate facts and scoped history in
        two bounded queries, avoiding stale ORM projections without N+1 reads.
        """
        captured_now = _as_utc(now or datetime.now(UTC))
        candidate_ids = []
        for item in items:
            if item.id is None:
                raise ValueError("candidate must be a persisted Item")
            candidate_ids.append(item.id)

        candidate_ids = tuple(dict.fromkeys(candidate_ids))
        if not candidate_ids:
            return {}

        candidate_rows = (
            await session.execute(
                select(Item.id, Item.category, Item.item_type, Item.priority_score)
                .where(Item.user_id == user_id, Item.id.in_(candidate_ids))
                .order_by(Item.id)
            )
        ).all()
        if len(candidate_rows) != len(candidate_ids):
            raise ValueError("all candidates must exist and belong to user_id")
        candidates: list[tuple[int, str | None, ItemType | None, int]] = []
        for item_id, category, item_type, priority_score in candidate_rows:
            if priority_score is None:
                raise ValueError("candidate must have a semantic priority_score")
            candidates.append((item_id, category, item_type, priority_score))

        categories = {category for _, category, _, _ in candidates if category is not None}
        item_types = {item_type for _, _, item_type, _ in candidates if item_type is not None}
        category_scores: dict[str, _DimensionAccumulator] = defaultdict(_DimensionAccumulator)
        type_scores: dict[ItemType, _DimensionAccumulator] = defaultdict(_DimensionAccumulator)

        dimensions = []
        if categories:
            dimensions.append(Item.category.in_(categories))
        if item_types:
            dimensions.append(Item.item_type.in_(item_types))

        if dimensions:
            history_result = await session.execute(
                select(
                    Event.id,
                    Event.item_id,
                    Event.event_type,
                    Event.created_at,
                    Item.category,
                    Item.item_type,
                )
                .join(
                    Item,
                    and_(Item.id == Event.item_id, Item.user_id == Event.user_id),
                )
                .where(
                    Event.user_id == user_id,
                    Event.event_type.in_(_INFORMATIVE_EVENT_TYPES),
                    or_(*dimensions),
                )
                .order_by(Event.created_at, Event.id)
            )
            history = history_result.all()

            events_by_item: dict[int, list[tuple[int, str, datetime]]] = defaultdict(list)
            item_dimensions: dict[int, tuple[str | None, ItemType | None]] = {}
            for event_id, item_id, event_type, created_at, category, item_type in history:
                events_by_item[item_id].append((event_id, event_type, created_at))
                item_dimensions[item_id] = (category, item_type)

            for history_item_id, events in events_by_item.items():
                category, item_type = item_dimensions[history_item_id]
                for event_id, event_type, created_at in _collapse_item_events(events):
                    effective_weight = SIGNAL_WEIGHTS[event_type] * _recency_multiplier(
                        created_at, captured_now
                    )
                    if category in categories:
                        category_scores[category].add(event_id, effective_weight)
                    if item_type in item_types:
                        type_scores[item_type].add(event_id, effective_weight)

        ranks: dict[int, BehaviourRank] = {}
        for item_id, category, item_type, priority_score in candidates:
            category_result = category_scores[category].result() if category is not None else None
            type_result = type_scores[item_type].result() if item_type is not None else None
            category_affinity = category_result.affinity if category_result else 0.0
            type_affinity = type_result.affinity if type_result else 0.0
            category_confidence = category_result.confidence if category_result else 0.0
            type_confidence = type_result.confidence if type_result else 0.0

            combined_affinity = category_affinity * CATEGORY_WEIGHT + type_affinity * TYPE_WEIGHT
            adjustment_points = _round_half_away_from_zero(
                max(MIN_AFFINITY, min(MAX_AFFINITY, combined_affinity)) * MAX_BEHAVIOUR_ADJUSTMENT
            )
            personal_rank = max(
                MIN_PERSONAL_RANK,
                min(MAX_PERSONAL_RANK, priority_score + adjustment_points),
            )
            category_event_ids = (
                category_scores[category].event_ids if category is not None else set()
            )
            type_event_ids = type_scores[item_type].event_ids if item_type is not None else set()

            ranks[item_id] = BehaviourRank(
                category_affinity=category_affinity,
                type_affinity=type_affinity,
                category_confidence=category_confidence,
                type_confidence=type_confidence,
                combined_affinity=combined_affinity,
                confidence=(category_confidence * CATEGORY_WEIGHT + type_confidence * TYPE_WEIGHT),
                adjustment_points=adjustment_points,
                personal_rank=personal_rank,
                informative_event_count=len(category_event_ids | type_event_ids),
                category_informative_event_count=(
                    category_result.informative_event_count if category_result else 0
                ),
                type_informative_event_count=(
                    type_result.informative_event_count if type_result else 0
                ),
            )

        return ranks
