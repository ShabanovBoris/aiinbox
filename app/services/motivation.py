"""Deterministic reminder signals paired with concrete PM-07-ranked saves."""

from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.domain.enums import MotivationKind
from app.domain.models import MotivationCandidate
from app.services.attention_ranking import AttentionRankingService
from app.services.calendar_windows import local_dates_utc_window, local_day_window
from app.storage.models import Event, Item, Reminder

MOTIVATION_NUDGE = "MOTIVATION_NUDGE"
STALE_IMPORTANT_MIN_AGE = timedelta(days=30)
HIGH_INTEREST_MIN_AGE = timedelta(days=14)
QUICK_WIN_MAX_MINUTES = 20
QUICK_WIN_MIN_PRIORITY = 60
WEEKLY_PROGRESS_MIN_DONE = 5

_KIND_SCORES = MappingProxyType(
    {
        MotivationKind.STALE_IMPORTANT: 60,
        MotivationKind.HIGH_INTEREST_STALE: 50,
        MotivationKind.QUICK_WINS: 40,
        MotivationKind.COMPLETION_STREAK: 30,
        MotivationKind.INBOX_GROWTH: 20,
        MotivationKind.WEEKLY_PROGRESS: 10,
    }
)


# ❌ Удалены aggregate-copy шаблоны и их ротация: мотивация теперь выбирает
# сигнал и конкретное сохранение, а пользовательский текст собирается из самого Item.


class MotivationService:
    """Read signals and use existing PM-07 ordering to assign a concrete focus.

    ReminderWorker retains pacing/arbitration; Telegram text and content remain
    outside this service, and the PM-07 rank formula is reused unchanged.
    """

    async def candidates(
        self,
        session,
        user_id: int,
        *,
        zone: ZoneInfo,
        now: datetime,
    ) -> list[MotivationCandidate]:
        """Pair current facts with their best eligible save in PM-07 order."""
        instant = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
        ranked = await AttentionRankingService().list_candidates(
            session, user_id, limit=None, now=instant
        )
        ranked_items = [item for item, _rank in ranked]
        if not ranked_items:
            return []

        local_date, day_start, day_end = local_day_window(instant, zone)
        facts_by_kind = self._item_facts(ranked_items, instant)
        focus_item = ranked_items[0]
        growth = await self._inbox_growth(session, user_id, day_start, day_end)
        done_dates, weekly_count = await self._completion_facts(session, user_id, zone, local_date)
        streak = self._current_streak(done_dates, local_date)
        if streak >= 3:
            facts_by_kind[MotivationKind.COMPLETION_STREAK] = ({"days": streak}, focus_item)
        if weekly_count >= WEEKLY_PROGRESS_MIN_DONE:
            facts_by_kind[MotivationKind.WEEKLY_PROGRESS] = (
                {"completed": weekly_count, "days": 7},
                focus_item,
            )
        if growth["net"] >= 3:
            facts_by_kind[MotivationKind.INBOX_GROWTH] = (growth, focus_item)

        if not facts_by_kind:
            return []
        sent_today = await self._sent_kinds_today(session, user_id, day_start, day_end)
        candidates = []
        for kind, (facts, focus) in facts_by_kind.items():
            # Successful same-day sends are skipped while other factual kinds can still compete.
            if kind in sent_today:
                continue
            candidates.append(
                MotivationCandidate(
                    kind=kind,
                    score=_KIND_SCORES[kind],
                    facts=facts,
                    focus_item_id=focus.id,
                )
            )
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    @staticmethod
    def _item_facts(ranked_items: list[Item], now: datetime) -> dict:
        """Count existing motivation signals while retaining their top PM-07 Item."""
        stale_before = now - STALE_IMPORTANT_MIN_AGE
        interest_before = now - HIGH_INTEREST_MIN_AGE
        stale = [
            item
            for item in ranked_items
            if item.priority_score >= 75 and item.created_at <= stale_before
        ]
        high_interest = [
            item
            for item in ranked_items
            if item.interest_level == 3 and item.created_at <= interest_before
        ]
        quick_wins = [
            item
            for item in ranked_items
            if item.estimated_action_minutes is not None
            and item.estimated_action_minutes <= QUICK_WIN_MAX_MINUTES
            and item.priority_score >= QUICK_WIN_MIN_PRIORITY
        ]
        facts = {}
        if stale:
            facts[MotivationKind.STALE_IMPORTANT] = ({"count": len(stale)}, stale[0])
        if high_interest:
            facts[MotivationKind.HIGH_INTEREST_STALE] = (
                {"count": len(high_interest)},
                high_interest[0],
            )
        if len(quick_wins) >= 3:
            facts[MotivationKind.QUICK_WINS] = (
                {"count": len(quick_wins), "max_minutes": QUICK_WIN_MAX_MINUTES},
                quick_wins[0],
            )
        return facts

    async def _inbox_growth(
        self, session, user_id: int, day_start: datetime, day_end: datetime
    ) -> dict[str, int]:
        """Compare today's canonical Item creation and durable resolution events."""
        created = await session.scalar(
            select(func.count(Item.id)).where(
                Item.user_id == user_id,
                Item.created_at >= day_start,
                Item.created_at < day_end,
            )
        )
        resolved = await session.scalar(
            select(func.count(Event.id)).where(
                Event.user_id == user_id,
                Event.event_type.in_(("DONE", "ARCHIVED")),
                Event.created_at >= day_start,
                Event.created_at < day_end,
            )
        )
        created_count, resolved_count = int(created or 0), int(resolved or 0)
        return {
            "created": created_count,
            "resolved": resolved_count,
            "net": created_count - resolved_count,
        }

    async def _completion_facts(
        self, session, user_id: int, zone: ZoneInfo, local_date: date
    ) -> tuple[set[date], int]:
        """Load only DONE timestamps, then project streak/week by local dates in Python."""
        week_start = local_date - timedelta(days=6)
        week_start_utc, week_end_utc = local_dates_utc_window(
            week_start, local_date + timedelta(days=1), zone
        )
        timestamps = (
            await session.scalars(
                select(Event.created_at).where(
                    Event.user_id == user_id,
                    Event.event_type == "DONE",
                )
            )
        ).all()
        done_dates = set()
        weekly_count = 0
        for timestamp in timestamps:
            instant = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
            if week_start_utc <= instant.astimezone(UTC).replace(tzinfo=None) < week_end_utc:
                weekly_count += 1
            done_dates.add(instant.astimezone(zone).date())
        return done_dates, weekly_count

    @staticmethod
    def _current_streak(done_dates: set[date], today: date) -> int:
        """Count a current today/yesterday-anchored calendar streak, not a historical best."""
        if today in done_dates:
            day = today
        elif today - timedelta(days=1) in done_dates:
            day = today - timedelta(days=1)
        else:
            return 0
        streak = 0
        while day in done_dates:
            streak += 1
            day -= timedelta(days=1)
        return streak

    async def _sent_kinds_today(
        self,
        session,
        user_id: int,
        day_start: datetime,
        day_end: datetime,
    ) -> set[MotivationKind]:
        """Use successful same-day Reminder facts to suppress repeated signals."""
        sent_today_payloads = (
            await session.scalars(
                select(Reminder.payload_json).where(
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.status == "SENT",
                    Reminder.sent_at.is_not(None),
                    Reminder.sent_at >= day_start,
                    Reminder.sent_at < day_end,
                )
            )
        ).all()
        sent_today = set()
        for payload in sent_today_payloads:
            try:
                sent_today.add(MotivationKind(payload["kind"]))
            except (KeyError, TypeError, ValueError):
                continue

        return sent_today
