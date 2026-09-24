"""Deterministic backlog facts and Russian templates for PM-10 nudges."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select

from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, MotivationKind, ProcessingStatus
from app.domain.models import MotivationCandidate
from app.services.calendar_windows import local_dates_utc_window, local_day_window
from app.storage.models import Event, Item, Reminder

MOTIVATION_NUDGE = "MOTIVATION_NUDGE"
STALE_IMPORTANT_MIN_AGE = timedelta(days=30)
HIGH_INTEREST_MIN_AGE = timedelta(days=14)
QUICK_WIN_MAX_MINUTES = 20
QUICK_WIN_MIN_PRIORITY = 60
WEEKLY_PROGRESS_MIN_DONE = 5

_KIND_SCORES: Mapping[MotivationKind, int] = MappingProxyType(
    {
        MotivationKind.STALE_IMPORTANT: 60,
        MotivationKind.HIGH_INTEREST_STALE: 50,
        MotivationKind.QUICK_WINS: 40,
        MotivationKind.COMPLETION_STREAK: 30,
        MotivationKind.INBOX_GROWTH: 20,
        MotivationKind.WEEKLY_PROGRESS: 10,
    }
)


@dataclass(frozen=True, slots=True)
class _Template:
    """Keep stable presentation IDs alongside copy for deterministic rotation."""

    template_id: str
    text: str


_TEMPLATES: Mapping[MotivationKind, tuple[_Template, ...]] = MappingProxyType(
    {
        MotivationKind.STALE_IMPORTANT: (
            _Template(
                "stale_important_v1",
                "У тебя {count} важных Item, сохранённых не меньше месяца назад. "
                "Можно вернуть в фокус один.",
            ),
            _Template(
                "stale_important_v2",
                "В активном списке есть {count} важных Item, сохранённых не меньше месяца назад. "
                "Достаточно выбрать один.",
            ),
        ),
        MotivationKind.HIGH_INTEREST_STALE: (
            _Template(
                "high_interest_stale_v1",
                "У {count} активных Item отмечен максимальный интерес; "
                "они ждут не меньше двух недель.",
            ),
            _Template(
                "high_interest_stale_v2",
                "{count} Item с максимальным уровнем интереса сохранены "
                "не меньше двух недель назад. "
                "Можно вернуться к одному.",
            ),
        ),
        MotivationKind.QUICK_WINS: (
            _Template(
                "quick_wins_v1",
                "В backlog есть {count} активных задач до {max_minutes} минут. "
                "Одной небольшой победы достаточно.",
            ),
            _Template(
                "quick_wins_v2",
                "Нашлось {count} активных задач с оценкой до {max_minutes} минут. "
                "Можно выбрать одну.",
            ),
        ),
        MotivationKind.INBOX_GROWTH: (
            _Template(
                "inbox_growth_v1",
                "Сегодня добавлено {created}, закрыто или архивировано {resolved}. "
                "Разница в backlog — {net}.",
            ),
            _Template(
                "inbox_growth_v2",
                "За сегодня: добавлено {created}, завершено или архивировано {resolved}; "
                "разница — {net} Item.",
            ),
        ),
        MotivationKind.COMPLETION_STREAK: (
            _Template(
                "completion_streak_v1",
                "Дни с Done идут подряд: {days}. Если удобно, эту серию можно продолжить.",
            ),
            _Template(
                "completion_streak_v2",
                "Текущая серия включает {days} подряд идущих календарных дней с Done.",
            ),
        ),
        MotivationKind.WEEKLY_PROGRESS: (
            _Template(
                "weekly_progress_v1",
                "За последние {days} дней закрыто {completed} Item. Это движение по backlog.",
            ),
            _Template(
                "weekly_progress_v2",
                "За период в {days} календарных дней отмечено {completed} завершений Item.",
            ),
        ),
    }
)


class MotivationService:
    """Read canonical Item/Event aggregates and project eligible nudge candidates.

    The service intentionally knows nothing about Attention intensity, delivery
    pacing, Telegram, Item content, profile signals, or LLM providers.
    """

    async def candidates(
        self,
        session,
        user_id: int,
        *,
        zone: ZoneInfo,
        now: datetime,
    ) -> list[MotivationCandidate]:
        """Compute current user-scoped facts and apply durable template history."""
        instant = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
        local_date, day_start, day_end = local_day_window(instant, zone)
        facts_by_kind = await self._item_facts(session, user_id, instant)
        growth = await self._inbox_growth(session, user_id, day_start, day_end)
        done_dates, weekly_count = await self._completion_facts(session, user_id, zone, local_date)
        streak = self._current_streak(done_dates, local_date)
        if streak >= 3:
            facts_by_kind[MotivationKind.COMPLETION_STREAK] = {"days": streak}
        if weekly_count >= WEEKLY_PROGRESS_MIN_DONE:
            facts_by_kind[MotivationKind.WEEKLY_PROGRESS] = {
                "completed": weekly_count,
                "days": 7,
            }
        if growth["net"] >= 3:
            facts_by_kind[MotivationKind.INBOX_GROWTH] = growth

        if not facts_by_kind:
            return []
        sent_today, latest_templates = await self._presentation_history(
            session, user_id, day_start, day_end, facts_by_kind
        )
        candidates = []
        for kind, facts in facts_by_kind.items():
            # Successful same-day sends are skipped while other factual kinds can still compete.
            if kind in sent_today:
                continue
            templates = _TEMPLATES[kind]
            latest = latest_templates.get(kind)
            template = next(
                (item for item in templates if item.template_id != latest), templates[0]
            )
            rendered = template.text.format(**facts)
            candidates.append(
                MotivationCandidate(
                    kind=kind,
                    score=_KIND_SCORES[kind],
                    facts=facts,
                    template_id=template.template_id,
                    rendered_text=rendered,
                )
            )
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    async def _item_facts(self, session, user_id: int, now: datetime) -> dict:
        """Aggregate the three active-actionable Item facts in one DB round trip."""
        eligible = (
            Item.user_id == user_id,
            Item.processing_status == ProcessingStatus.READY,
            Item.state == ItemState.ACTIVE,
            Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
        )
        stale_before = now - STALE_IMPORTANT_MIN_AGE
        interest_before = now - HIGH_INTEREST_MIN_AGE
        quick_wins = and_(
            Item.estimated_action_minutes.is_not(None),
            Item.estimated_action_minutes <= QUICK_WIN_MAX_MINUTES,
            Item.priority_score >= QUICK_WIN_MIN_PRIORITY,
        )
        row = (
            await session.execute(
                select(
                    func.sum(
                        case(
                            (
                                and_(Item.priority_score >= 75, Item.created_at <= stale_before),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (
                                and_(Item.interest_level == 3, Item.created_at <= interest_before),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(case((quick_wins, 1), else_=0)),
                ).where(*eligible)
            )
        ).one()
        stale_count, interest_count, quick_count = (int(value or 0) for value in row)
        facts = {}
        if stale_count >= 1:
            facts[MotivationKind.STALE_IMPORTANT] = {"count": stale_count}
        if interest_count >= 1:
            facts[MotivationKind.HIGH_INTEREST_STALE] = {"count": interest_count}
        if quick_count >= 3:
            facts[MotivationKind.QUICK_WINS] = {
                "count": quick_count,
                "max_minutes": QUICK_WIN_MAX_MINUTES,
            }
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

    async def _presentation_history(
        self,
        session,
        user_id: int,
        day_start: datetime,
        day_end: datetime,
        kinds: Mapping[MotivationKind, dict],
    ) -> tuple[set[MotivationKind], dict[MotivationKind, str]]:
        """Use SENT reminders only for same-day kind suppression and template rotation."""
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

        latest_templates = {}
        for kind in kinds:
            latest_payload = await session.scalar(
                select(Reminder.payload_json)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.status == "SENT",
                    Reminder.sent_at.is_not(None),
                    func.json_extract(Reminder.payload_json, "$.kind") == kind.value,
                )
                .order_by(Reminder.sent_at.desc(), Reminder.id.desc())
                .limit(1)
            )
            if isinstance(latest_payload, dict) and isinstance(
                latest_payload.get("template_id"), str
            ):
                latest_templates[kind] = latest_payload["template_id"]
        return sent_today, latest_templates
