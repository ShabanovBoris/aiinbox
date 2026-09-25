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


# ❌ Удалены краткие v1/v2 формулировки, звучавшие как telemetry; новые семейства
# короче и по-прежнему используют только факты из MotivationCandidate.
_TEMPLATES: Mapping[MotivationKind, tuple[_Template, ...]] = MappingProxyType(
    {
        MotivationKind.STALE_IMPORTANT: (
            _Template(
                "stale_important_v3",
                "Важные сохранения старше месяца: {count}. Что открыть?",
            ),
            _Template(
                "stale_important_v4",
                "Приоритетные Items старше месяца: {count}. Вернуть один в фокус?",
            ),
            _Template(
                "stale_important_v5",
                "Старые важные сохранения в Inbox: {count}. С чего начать?",
            ),
            _Template(
                "stale_important_v6",
                "Важное, что лежит больше месяца: {count}. Вернуть одно?",
            ),
        ),
        MotivationKind.HIGH_INTEREST_STALE: (
            _Template(
                "high_interest_stale_v3",
                "Сохранения с интересом 3/3 старше двух недель: {count}. Что открыть?",
            ),
            _Template(
                "high_interest_stale_v4",
                "Интерес 3/3 и возраст от двух недель — таких материалов: {count}. "
                "Какой пересмотреть?",
            ),
            _Template(
                "high_interest_stale_v5",
                "Материалы с интересом 3/3 старше двух недель: {count}. Что пересмотреть?",
            ),
            _Template(
                "high_interest_stale_v6",
                "Сохранения с интересом 3/3 старше двух недель: {count}. С чего начать?",
            ),
        ),
        MotivationKind.QUICK_WINS: (
            _Template(
                "quick_wins_v3",
                "Задач с оценкой до {max_minutes} минут: {count}. Выбрать одну?",
            ),
            _Template(
                "quick_wins_v4",
                "Количество задач с оценкой до {max_minutes} минут — {count}. С какой начать?",
            ),
            _Template(
                "quick_wins_v5",
                "Коротких задач по оценке (до {max_minutes} минут): {count}. Один быстрый заход?",
            ),
            _Template(
                "quick_wins_v6",
                "Задач, которым оценили до {max_minutes} минут: {count}. Какую взять?",
            ),
        ),
        MotivationKind.INBOX_GROWTH: (
            _Template(
                "inbox_growth_v3",
                "Сегодня +{net} к списку: {created} новых, {resolved} завершено или архивировано. "
                "Разгрузить один?",
            ),
            _Template(
                "inbox_growth_v4",
                "Inbox вырос на {net}: добавлено {created}, завершено или архивировано {resolved}. "
                "С чего начать?",
            ),
            _Template(
                "inbox_growth_v5",
                "За сегодня — {created} новых и {resolved} завершённых или архивированных; "
                "прирост {net}. Открыть один?",
            ),
            _Template(
                "inbox_growth_v6",
                "Сегодня в Inbox стало на {net} записей больше: +{created} и −{resolved}. "
                "Разобрать одну?",
            ),
        ),
        MotivationKind.COMPLETION_STREAK: (
            _Template(
                "completion_streak_v3",
                "Дни подряд с хотя бы одним Done: {days}. Продолжить серию?",
            ),
            _Template(
                "completion_streak_v4",
                "Done шёл подряд {days} календарных дней. Что станет следующим?",
            ),
            _Template(
                "completion_streak_v5",
                "Длина серии дней с Done: {days}. Выбрать следующий шаг?",
            ),
            _Template(
                "completion_streak_v6",
                "Текущая серия Done — {days} подряд. Продолжить в удобном темпе?",
            ),
        ),
        MotivationKind.WEEKLY_PROGRESS: (
            _Template(
                "weekly_progress_v3",
                "Done за последние {days} дней: {completed}. Какой Item станет следующим?",
            ),
            _Template(
                "weekly_progress_v4",
                "Завершений за {days}-дневный период: {completed}. Есть что закрыть следующим?",
            ),
            _Template(
                "weekly_progress_v5",
                "Done за последние {days} дней: {completed}. Выбрать следующий?",
            ),
            _Template(
                "weekly_progress_v6",
                "Завершения за последние {days} дней: {completed}. Продолжить с одним?",
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
            template = _next_template(templates, latest)
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


def _next_template(templates: tuple[_Template, ...], latest_template_id: str | None) -> _Template:
    """Advance one ordered copy family; legacy history starts from its current first variant."""
    latest_index = next(
        (
            index
            for index, template in enumerate(templates)
            if template.template_id == latest_template_id
        ),
        None,
    )
    return templates[0] if latest_index is None else templates[(latest_index + 1) % len(templates)]
