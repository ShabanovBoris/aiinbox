"""Deterministic weekly reflection projected from canonical inbox facts.

The service owns only bounded reads over Item, Event, and Reminder. Keeping its
result immutable lets Telegram format a consistent snapshot without retaining a
database session or turning the report into another source of business state.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, ProcessingStatus
from app.services.attention_ranking import AttentionRankingService
from app.services.calendar_windows import local_dates_utc_window
from app.storage.models import Event, Item, Reminder

_DAY = timedelta(days=1)
_STALE_AGE = timedelta(days=30)
_OLD_ITEM_AGE = timedelta(days=30)
_CLEANUP_AGE = timedelta(days=90)
_REVISIT_AGE = timedelta(days=14)
_HIGH_PRIORITY = 75
_REMINDER_EVENT_TYPES = (
    "REMINDER_SENT",
    "REMINDER_OPENED",
    "REMINDER_SNOOZED",
    "REMINDER_DONE",
    "REMINDER_DISMISSED",
    "REMINDER_DISLIKED",
)


@dataclass(frozen=True, slots=True)
class WeeklyFlow:
    """Factual lifecycle flow for the report window, with unclamped net change."""

    created: int
    completed: int
    archived: int
    net_change: int


@dataclass(frozen=True, slots=True)
class WeeklyBacklog:
    """Current active actionable facts, including elapsed-age attention signals."""

    active_actionable: int
    high_priority: int
    high_interest: int
    stale: int
    old_important_unrevisited: int


@dataclass(frozen=True, slots=True)
class CategoryCount:
    """A current canonical category paired with a period-specific fact count."""

    category: str
    count: int


@dataclass(frozen=True, slots=True)
class WeeklyReminderOutcomes:
    """Raw outcome counts for item-specific proactive Attention reminders only."""

    sent: int
    opened: int
    snoozed: int
    done: int
    dismissed: int
    disliked: int

    @property
    def has_activity(self) -> bool:
        """Expose whether any observed outcome makes the Attention section useful."""
        return any((self.sent, self.opened, self.snoozed, self.done, self.dismissed, self.disliked))


@dataclass(frozen=True, slots=True)
class WeeklyRecommendation:
    """Small presentation-ready recommendation facts, never a live ORM Item."""

    kind: str
    item_id: int
    title: str
    estimated_action_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class WeeklyReview:
    """Immutable read model consumed by the Telegram formatter."""

    flow: WeeklyFlow
    backlog: WeeklyBacklog
    created_categories: tuple[CategoryCount, ...]
    completed_categories: tuple[CategoryCount, ...]
    most_postponed: CategoryCount | None
    strongest_progress: CategoryCount | None
    reminder_outcomes: WeeklyReminderOutcomes | None
    recommendations: tuple[WeeklyRecommendation, ...]


class WeeklyReviewService:
    """Build a read-only weekly projection while reusing PM-07 for ranking.

    Calendar boundaries are computed once from the caller's zone; all elapsed-age
    rules use the same captured UTC instant so one report cannot mix time frames.
    """

    async def build(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        zone: ZoneInfo,
        now: datetime | None = None,
    ) -> WeeklyReview:
        """Aggregate one user without writing Events, Reminders, or Item fields."""
        instant = _as_utc(now or datetime.now(UTC))
        local_today = instant.astimezone(zone).date()
        first_day = local_today - timedelta(days=6)
        start, end = local_dates_utc_window(first_day, local_today + _DAY, zone)
        instant_naive = instant.replace(tzinfo=None)

        created, created_categories = await self._created_facts(session, user_id, start, end)
        (
            completed,
            archived,
            completed_categories,
            postponed_categories,
        ) = await self._lifecycle_facts(session, user_id, start, end)
        backlog = await self._backlog_facts(session, user_id, instant_naive)
        reminders = await self._reminder_facts(session, user_id, start, end)
        recommendations = await self._recommendations(session, user_id, instant, instant_naive)

        postponed = postponed_categories[0] if postponed_categories else None
        if postponed is not None and postponed.count < 2:
            postponed = None
        progress = completed_categories[0] if completed_categories else None
        if progress is not None and progress.count < 2:
            progress = None

        reminder_outcomes = WeeklyReminderOutcomes(**reminders) if any(reminders.values()) else None
        flow = WeeklyFlow(
            created=created,
            completed=completed,
            archived=archived,
            net_change=created - completed - archived,
        )
        return WeeklyReview(
            flow=flow,
            backlog=backlog,
            created_categories=created_categories,
            completed_categories=completed_categories,
            most_postponed=postponed,
            strongest_progress=progress,
            reminder_outcomes=reminder_outcomes,
            recommendations=recommendations,
        )

    @staticmethod
    async def _created_facts(
        session: AsyncSession, user_id: int, start: datetime, end: datetime
    ) -> tuple[int, tuple[CategoryCount, ...]]:
        """Count capture by Item creation time and project current categories."""
        conditions = (
            Item.user_id == user_id,
            Item.created_at >= start,
            Item.created_at < end,
        )
        created = int(await session.scalar(select(func.count(Item.id)).where(*conditions)) or 0)
        rows = (
            await session.execute(
                select(Item.category, func.count(Item.id))
                .where(
                    *conditions,
                    Item.category.is_not(None),
                    func.trim(Item.category) != "",
                )
                .group_by(Item.category)
                .order_by(func.count(Item.id).desc(), Item.category.asc())
                .limit(3)
            )
        ).all()
        return created, tuple(CategoryCount(category, int(count)) for category, count in rows)

    @staticmethod
    async def _lifecycle_facts(
        session: AsyncSession, user_id: int, start: datetime, end: datetime
    ) -> tuple[int, int, tuple[CategoryCount, ...], tuple[CategoryCount, ...]]:
        """Read lifecycle actions from Events and join only current Item category."""
        rows = (
            await session.execute(
                select(Event.event_type, Item.category, func.count(Event.id))
                .join(Item, Item.id == Event.item_id)
                .where(
                    Event.user_id == user_id,
                    Item.user_id == user_id,
                    Event.event_type.in_(("DONE", "ARCHIVED", "SNOOZED")),
                    Event.created_at >= start,
                    Event.created_at < end,
                )
                .group_by(Event.event_type, Item.category)
            )
        ).all()
        completed = 0
        archived = 0
        completed_by_category: dict[str, int] = {}
        postponed_by_category: dict[str, int] = {}
        for event_type, category, count in rows:
            if event_type == "DONE":
                completed += count
                if isinstance(category, str) and category.strip():
                    completed_by_category[category] = completed_by_category.get(category, 0) + count
            elif event_type == "ARCHIVED":
                archived += count
            elif event_type == "SNOOZED" and isinstance(category, str) and category.strip():
                postponed_by_category[category] = postponed_by_category.get(category, 0) + count

        return (
            completed,
            archived,
            _top_categories(completed_by_category, limit=3),
            _top_categories(postponed_by_category, limit=1),
        )

    @staticmethod
    async def _backlog_facts(session: AsyncSession, user_id: int, now: datetime) -> WeeklyBacklog:
        """Project current backlog while excluding recent revisit evidence in SQL."""
        active_actionable = and_(
            Item.user_id == user_id,
            Item.processing_status == ProcessingStatus.READY,
            Item.state == ItemState.ACTIVE,
            Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
        )
        stale = and_(
            Item.created_at <= now - _STALE_AGE,
            Item.created_at <= now,
        )
        recent_exposure = (
            select(Event.id)
            .where(
                Event.user_id == user_id,
                Event.item_id == Item.id,
                Event.event_type.in_(("TODAY_SHOWN", "ATTENTION_SHOWN")),
                Event.created_at >= now - _REVISIT_AGE,
                Event.created_at <= now,
            )
            .correlate(Item)
            .exists()
        )
        successful_proactive = (
            select(Reminder.id)
            .where(
                Reminder.user_id == user_id,
                Reminder.item_id == Item.id,
                Reminder.type == "PROACTIVE_ATTENTION",
                Reminder.status == "SENT",
                Reminder.sent_at >= now - _REVISIT_AGE,
                Reminder.sent_at <= now,
            )
            .correlate(Item)
            .exists()
        )
        old_important_unrevisited = and_(
            Item.priority_score >= _HIGH_PRIORITY,
            stale,
            ~recent_exposure,
            ~successful_proactive,
        )

        row = (
            await session.execute(
                select(
                    func.count(Item.id),
                    func.coalesce(
                        func.sum(case((Item.priority_score >= _HIGH_PRIORITY, 1), else_=0)), 0
                    ),
                    func.coalesce(func.sum(case((Item.interest_level == 3, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((stale, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((old_important_unrevisited, 1), else_=0)), 0),
                ).where(active_actionable)
            )
        ).one()
        return WeeklyBacklog(*(int(value or 0) for value in row))

    @staticmethod
    async def _reminder_facts(
        session: AsyncSession, user_id: int, start: datetime, end: datetime
    ) -> dict[str, int]:
        """Aggregate observed outcomes through each Event's canonical Reminder row."""
        rows = (
            await session.execute(
                select(Event.event_type, func.count(Event.id))
                .join(Reminder, Reminder.id == Event.reminder_id)
                .where(
                    Event.user_id == user_id,
                    Reminder.user_id == user_id,
                    Reminder.type == "PROACTIVE_ATTENTION",
                    Event.event_type.in_(_REMINDER_EVENT_TYPES),
                    Event.created_at >= start,
                    Event.created_at < end,
                )
                .group_by(Event.event_type)
            )
        ).all()
        field_by_event = {
            "REMINDER_SENT": "sent",
            "REMINDER_OPENED": "opened",
            "REMINDER_SNOOZED": "snoozed",
            "REMINDER_DONE": "done",
            "REMINDER_DISMISSED": "dismissed",
            "REMINDER_DISLIKED": "disliked",
        }
        counts = {field: 0 for field in field_by_event.values()}
        for event_type, count in rows:
            counts[field_by_event[event_type]] = int(count)
        return counts

    @staticmethod
    async def _recommendations(
        session: AsyncSession,
        user_id: int,
        now: datetime,
        now_naive: datetime,
    ) -> tuple[WeeklyRecommendation, ...]:
        """Reuse the PM-07 ordering once, then select distinct deterministic classes."""
        ranked = await AttentionRankingService().list_candidates(
            session, user_id, limit=None, now=now
        )
        recommendations: list[WeeklyRecommendation] = []
        selected_ids: set[int] = set()

        for item, _rank in ranked:
            if (
                item.priority_score is not None
                and item.priority_score >= _HIGH_PRIORITY
                and _age_at_least(item.created_at, now, _OLD_ITEM_AGE)
            ):
                recommendations.append(
                    WeeklyRecommendation(
                        "RETURN_OLD_IMPORTANT", item.id, item.title or "Без названия"
                    )
                )
                selected_ids.add(item.id)
                break

        for item, _rank in ranked:
            if (
                item.id not in selected_ids
                and item.priority_score is not None
                and item.priority_score >= 60
                and item.estimated_action_minutes is not None
                and item.estimated_action_minutes <= 20
            ):
                recommendations.append(
                    WeeklyRecommendation(
                        "QUICK_WIN",
                        item.id,
                        item.title or "Без названия",
                        item.estimated_action_minutes,
                    )
                )
                selected_ids.add(item.id)
                break

        not_interesting = (
            select(Event.id)
            .where(
                Event.user_id == user_id,
                Event.item_id == Item.id,
                Event.event_type == "NOT_INTERESTING",
            )
            .correlate(Item)
            .exists()
        )
        cleanup_conditions = [
            Item.user_id == user_id,
            Item.processing_status == ProcessingStatus.READY,
            Item.state == ItemState.ACTIVE,
            Item.created_at <= now_naive - _CLEANUP_AGE,
            Item.created_at <= now_naive,
            Item.priority_score < _HIGH_PRIORITY,
            or_(Item.interest_level == 1, not_interesting),
        ]
        if selected_ids:
            cleanup_conditions.append(Item.id.not_in(selected_ids))
        cleanup = (
            await session.execute(
                select(Item.id, Item.title, Item.estimated_action_minutes)
                .where(*cleanup_conditions)
                .order_by(Item.created_at.asc(), Item.priority_score.asc(), Item.id.asc())
                .limit(1)
            )
        ).first()
        if cleanup is not None:
            item_id, title, estimated_action_minutes = cleanup
            recommendations.append(
                WeeklyRecommendation(
                    "CLEANUP_REVIEW",
                    item_id,
                    title or "Без названия",
                    estimated_action_minutes,
                )
            )
        return tuple(recommendations[:3])


def _as_utc(value: datetime) -> datetime:
    """Interpret SQLite naive values as UTC and normalize injected instants once."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_at_least(created_at: datetime, now: datetime, age: timedelta) -> bool:
    """Apply exact elapsed-age thresholds while excluding malformed future Items."""
    created = _as_utc(created_at)
    return created <= now and now - created >= age


def _top_categories(counts: dict[str, int], *, limit: int) -> tuple[CategoryCount, ...]:
    """Use a stable count-descending/name-ascending order for category ties."""
    return tuple(
        CategoryCount(category, count)
        for category, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    )
