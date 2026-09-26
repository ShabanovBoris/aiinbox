"""Persistent notification policy and the SQLite-backed reminder worker.

Digest and snooze keep their no-replay claims; proactive Attention uses a
bounded lease and finalizes successful delivery only after Telegram accepts it.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.bot.formatting import (
    format_attention_reason,
    format_proactive_attention_reminder,
    format_today,
)
from app.bot.keyboards import (
    item_keyboard,
    item_list_keyboard,
    motivation_reminder_keyboard,
    proactive_reminder_keyboard,
)
from app.bot.presentation import item_display_title, item_navigation_entries
from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, ProcessingStatus
from app.domain.models import MotivationCandidate
from app.services.attention_hooks import AttentionHookService
from app.services.attention_ranking import AttentionRank, AttentionRankingService
from app.services.calendar_windows import local_day_window as _shared_local_day_window
from app.services.motivation import MOTIVATION_NUDGE, MotivationService
from app.services.reminder_feedback import (
    REMINDER_DISMISSAL_COOLDOWN,
    ReminderFeedbackService,
    record_reminder_event,
)
from app.services.retrieval import TodayService, load_item_sources_by_item
from app.storage.models import Event, Item, ItemSource, Reminder, User

log = logging.getLogger(__name__)

DAILY_DIGEST = "DAILY_DIGEST"
SNOOZE_RESURFACE = "SNOOZE_RESURFACE"
PROACTIVE_ATTENTION = "PROACTIVE_ATTENTION"
# ❌ Удалён общий порог proactive Attention: доступность кандидата теперь
# определяется выбранной политикой интенсивности, а формула ранжирования не меняется.
ATTENTION_CANDIDATE_LIMIT = 10
PROACTIVE_CLAIM_LEASE = timedelta(minutes=5)
GENERIC_REPEAT_GAP = timedelta(hours=4)
# This absolute send window starts at claim creation, leaving recovery time in
# the longer lease even if a worker pauses before Telegram I/O.
NOTIFICATION_SEND_TIMEOUT = timedelta(minutes=2)
ATTENTION_HOOK_TIMEOUT_SECONDS = 15.0
ATTENTION_HOOK_SEND_SAFETY_SECONDS = 30.0
_BUDGET_REMINDER_TYPES = (DAILY_DIGEST, PROACTIVE_ATTENTION, MOTIVATION_NUDGE)
_GAP_REMINDER_TYPES = (DAILY_DIGEST, PROACTIVE_ATTENTION, SNOOZE_RESURFACE, MOTIVATION_NUDGE)
_DELIVERY_CLAIM_TYPES = (DAILY_DIGEST, SNOOZE_RESURFACE, PROACTIVE_ATTENTION, MOTIVATION_NUDGE)
_ATTENTION_FAMILY_TYPES = (PROACTIVE_ATTENTION, MOTIVATION_NUDGE)
_OPEN_PROACTIVE_STATUSES = ("PENDING", "CLAIMED")
_OPEN_MOTIVATION_STATUSES = ("PENDING", "CLAIMED")
_TRANSIENT_ATTENTION_BLOCKS = frozenset({"quiet_hours", "delivery_in_progress"})
GENERIC_MOTIVATION_DAILY_CAPS: Mapping[int, int] = MappingProxyType({1: 0, 2: 1, 3: 1, 4: 1, 5: 2})
_DEFAULT_SETTINGS = {
    "daily_digest_enabled": True,
    "daily_digest_time": "09:00",
    "quiet_hours_start": "22:30",
    "quiet_hours_end": "08:00",
    "attention_enabled": True,
    "attention_intensity": 3,
    "generic_motivation_enabled": True,
}


@dataclass(frozen=True)
class AttentionIntensityPolicy:
    """One immutable PM-08 frequency policy; it never changes Item ranking."""

    level: int
    label: str
    daily_cap: int
    minimum_gap: timedelta
    same_item_cooldown: timedelta
    minimum_proactive_score: int


@dataclass(frozen=True, slots=True)
class AttentionStatus:
    """Read-only projection of the same gates and candidate sets used by ReminderWorker."""

    timezone_name: str
    local_now: datetime
    quiet_start: time
    quiet_end: time
    quiet_state: str
    policy: AttentionIntensityPolicy
    budget_used: int
    generic_used: int
    generic_cap: int
    latest_type: str | None
    latest_sent_at: datetime | None
    next_possible_at: datetime | None
    proactive_candidates: int
    generic_candidates: int
    block_reasons: tuple[str, ...]


ATTENTION_POLICIES: Mapping[int, AttentionIntensityPolicy] = MappingProxyType(
    {
        1: AttentionIntensityPolicy(1, "Calm", 1, timedelta(hours=8), timedelta(hours=72), 60),
        2: AttentionIntensityPolicy(2, "Light", 2, timedelta(hours=5), timedelta(hours=48), 60),
        3: AttentionIntensityPolicy(3, "Normal", 3, timedelta(hours=3), timedelta(hours=30), 60),
        4: AttentionIntensityPolicy(4, "Active", 4, timedelta(hours=2), timedelta(hours=20), 55),
        5: AttentionIntensityPolicy(
            5, "Aggressive", 6, timedelta(minutes=90), timedelta(hours=12), 50
        ),
    }
)


def attention_policy(level: int) -> AttentionIntensityPolicy:
    """Resolve one supported policy level and reject invalid user input."""
    if type(level) is not int or level not in ATTENTION_POLICIES:
        raise ValueError("attention_intensity должен быть от 1 до 5")
    return ATTENTION_POLICIES[level]


def _utc_naive(value: datetime) -> datetime:
    """Match all scheduler comparisons to the repository's naive-UTC columns."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _local_day_window(now: datetime, zone: ZoneInfo) -> tuple[date, datetime, datetime]:
    """Keep PM-08's private call sites on the shared DST-safe date projection."""
    # ❌ Удалена локальная конвертация UTC-полуночей; PM-08 и PM-10 теперь
    # используют одинаковую DST-семантику из calendar_windows.
    return _shared_local_day_window(now, zone)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_timezone(value: str) -> ZoneInfo:
    """Validate an IANA timezone before it reaches durable user settings."""
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Неизвестный часовой пояс: {value}") from exc


def parse_clock(value: str) -> time:
    """Parse the deliberately small HH:MM settings contract."""
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("Время должно быть в формате HH:MM")
    return time.fromisoformat(value)


def settings_for(user: User) -> dict:
    """Project new-user defaults while explicit migrated settings stay canonical."""
    settings = dict(_DEFAULT_SETTINGS)
    settings.update(user.settings_json or {})
    return settings


def in_quiet_hours(local_time: time, start: time, end: time) -> bool:
    """Support both overnight (22:30–08:00) and same-day quiet windows."""
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _quiet_hours_state(local_time: time, start: time, end: time) -> str:
    """Describe the configured window using the same predicate as scheduler gating."""
    if in_quiet_hours(local_time, start, end):
        return "идут"
    if start < end and local_time < start:
        return "ещё не начались"
    return "закончились"


def _quiet_hours_release(
    local_now: datetime, start: time, end: time, zone: ZoneInfo
) -> datetime | None:
    """Project the current quiet window's end in the configured IANA timezone."""
    if not in_quiet_hours(local_now.time(), start, end) or start == end:
        return None
    local_date = local_now.date()
    if start > end and local_now.time() >= start:
        local_date += timedelta(days=1)
    return datetime.combine(local_date, end, tzinfo=zone)


def digest_target_date(
    local_now: datetime, digest_time: time, quiet_start: time, quiet_end: time
) -> date | None:
    """Return the local day whose digest is due, or None when it is not due."""
    if in_quiet_hours(local_now.time(), quiet_start, quiet_end):
        return None

    if local_now.time() >= digest_time:
        return local_now.date()
    return None


async def get_notification_settings(
    session_factory, telegram_user_id: int
) -> tuple[User, dict] | None:
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            return None
        return user, settings_for(user)


async def get_attention_status(
    session_factory,
    telegram_user_id: int,
    *,
    default_timezone: str = "UTC",
    now: datetime | None = None,
) -> AttentionStatus | None:
    """Read scheduler diagnostics through ReminderWorker's existing eligibility rules."""
    return await ReminderWorker(
        session_factory,
        bot=None,
        default_timezone=default_timezone,
    ).attention_status(telegram_user_id, now=now)


async def update_notification_settings(
    session_factory,
    telegram_user_id: int,
    *,
    timezone: str | None = None,
    daily_digest_enabled: bool | None = None,
    daily_digest_time: str | None = None,
    quiet_hours_start: str | None = None,
    quiet_hours_end: str | None = None,
    attention_enabled: bool | None = None,
    attention_intensity: int | None = None,
    generic_motivation_enabled: bool | None = None,
) -> tuple[User, dict] | None:
    """Validate and persist only requested settings in one user-scoped update."""
    if timezone is not None:
        parse_timezone(timezone)
    for clock in (daily_digest_time, quiet_hours_start, quiet_hours_end):
        if clock is not None:
            parse_clock(clock)
    if attention_enabled is not None and type(attention_enabled) is not bool:
        raise ValueError("attention_enabled должен быть true или false")
    if generic_motivation_enabled is not None and type(generic_motivation_enabled) is not bool:
        raise ValueError("generic_motivation_enabled должен быть true или false")
    if attention_intensity is not None:
        attention_policy(attention_intensity)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            return None
        if timezone is not None:
            user.timezone = timezone
        settings_patch = {
            "daily_digest_enabled": daily_digest_enabled,
            "daily_digest_time": daily_digest_time,
            "quiet_hours_start": quiet_hours_start,
            "quiet_hours_end": quiet_hours_end,
            "attention_enabled": attention_enabled,
            "attention_intensity": attention_intensity,
            "generic_motivation_enabled": generic_motivation_enabled,
        }
        settings_patch = {key: value for key, value in settings_patch.items() if value is not None}
        values = {}
        if daily_digest_enabled is True and not settings_for(user)["daily_digest_enabled"]:
            values["daily_digest_enabled_at"] = _utc_now()
        elif daily_digest_enabled is False:
            values["daily_digest_enabled_at"] = None
        if timezone is not None:
            values["timezone"] = timezone
        if settings_patch:
            # json_patch is evaluated by SQLite against the row currently being
            # updated, so concurrent disjoint updates do not lose each other.
            values["settings_json"] = func.json_patch(
                func.coalesce(User.settings_json, "{}"), json.dumps(settings_patch)
            )
        if values:
            await session.execute(update(User).where(User.id == user.id).values(**values))
        await session.commit()
        await session.refresh(user)
        settings = settings_for(user)
        return user, settings


def format_settings(user: User, settings: dict, now: datetime | None = None) -> str:
    enabled = "включён" if settings["daily_digest_enabled"] else "выключен"
    level = settings.get("attention_intensity", 3)
    if type(level) is not int or level not in ATTENTION_POLICIES:
        level = 3
    policy = attention_policy(level)
    attention_enabled = settings.get("attention_enabled") is True
    attention_status = "ON" if attention_enabled else "OFF"
    try:
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        local_now = instant.astimezone(parse_timezone(user.timezone))
        local_clock = local_now.strftime("%H:%M")
    except ValueError:
        local_clock = "недоступно"
    # ❌ Удалены slash-примеры как основной способ редактирования; обычный экран
    # ведёт к кнопкам guided input, а командный синтаксис остаётся запасным.
    return (
        "⚙️ Настройки уведомлений\n"
        f"🌍 Часовой пояс: {user.timezone}\n"
        f"🕒 Сейчас для уведомлений: {local_clock}\n"
        f"Ежедневная сводка: {enabled} ({settings['daily_digest_time']})\n"
        f"Тихие часы: {settings['quiet_hours_start']}–{settings['quiet_hours_end']}\n\n"
        f"Внимание: {'включено' if attention_status == 'ON' else 'выключено'}, "
        f"{policy.label} ({policy.level})\n\n"
        "Параметры можно изменить кнопками ниже. Команды /settings остаются доступны."
    )


def format_attention_settings(settings: dict) -> str:
    """Explain interruption frequency from the same policy the worker enforces."""
    level = settings.get("attention_intensity", 3)
    if type(level) is not int or level not in ATTENTION_POLICIES:
        level = 3
    policy = attention_policy(level)
    status = "ON" if settings.get("attention_enabled") is True else "OFF"
    motivation_status = (
        "включены" if settings.get("generic_motivation_enabled") is True else "выключены"
    )
    gap_seconds = int(policy.minimum_gap.total_seconds())
    gap = f"{gap_seconds // 3600} ч" if gap_seconds % 3600 == 0 else f"{gap_seconds // 60} мин"
    cooldown_hours = int(policy.same_item_cooldown.total_seconds() // 3600)
    return (
        "🧠 Attention\n\n"
        f"Статус: {'включён' if status == 'ON' else 'выключен'}\n"
        f"Интенсивность: {policy.level} — {policy.label}\n"
        f"Общие напоминания: {motivation_status}\n"
        f"Лимит: до {policy.daily_cap} уведомлений в день, включая сводку\n"
        f"Минимальный интервал: {gap}\n"
        f"Повтор материала: через {cooldown_hours} ч"
    )


def format_attention_status(status: AttentionStatus) -> str:
    """Render read-only scheduler facts with Russian user-facing blocker labels."""
    latest_labels = {
        DAILY_DIGEST: "Дайджест",
        PROACTIVE_ATTENTION: "Attention",
        SNOOZE_RESURFACE: "Отложенный материал",
        MOTIVATION_NUDGE: "Общее напоминание",
    }
    blockers = {
        "attention_off": "Attention выключен",
        "quiet_hours": "сейчас тихие часы",
        "daily_cap": "достигнут дневной лимит",
        "minimum_gap": "не прошёл минимальный интервал",
        "delivery_in_progress": "уже готовится отправка",
        "no_proactive_candidate": "нет подходящих материалов",
        "no_generic_candidate": "нет подходящего общего напоминания",
        "candidate_cooldown": "ещё действует пауза повторного показа",
        "ready": "ничего — вариант доступен в следующем цикле",
        "no_delivery_target": "не задан чат для доставки",
    }
    lines = [
        "📊 Attention сейчас",
        "",
        f"🌍 Часовой пояс: {status.timezone_name}",
        f"🕒 Локальное время бота: {status.local_now:%H:%M}",
        f"🌙 Тихие часы: {status.quiet_start:%H:%M}–{status.quiet_end:%H:%M} — "
        f"{status.quiet_state}",
        "",
        f"Режим: {status.policy.label} ({status.policy.level})",
        "",
        "Сегодня:",
        f"уведомления {status.budget_used}/{status.policy.daily_cap}",
        f"общие напоминания {status.generic_used}/{status.generic_cap}",
    ]
    if status.latest_type is not None and status.latest_sent_at is not None:
        now_utc = status.local_now.astimezone(UTC).replace(tzinfo=None)
        elapsed = max(0, int((now_utc - status.latest_sent_at).total_seconds()))
        if elapsed < 60:
            ago = "меньше минуты"
        elif elapsed < 3600:
            ago = f"{elapsed // 60} мин"
        else:
            hours, minutes = divmod(elapsed // 60, 60)
            ago = f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"
        lines.extend(
            ("", f"Последнее: {latest_labels.get(status.latest_type, 'Уведомление')} · {ago} назад")
        )
    if status.next_possible_at is not None:
        next_time = (
            status.next_possible_at.strftime("%H:%M")
            if status.next_possible_at.date() == status.local_now.date()
            else status.next_possible_at.strftime("%d.%m %H:%M")
        )
        lines.extend(("", f"Следующее возможно не раньше: {next_time}"))
    blocker_labels = [blockers.get(reason, "нет данных") for reason in status.block_reasons]
    blocker_text = (
        "ничего — вариант доступен в следующем цикле"
        if status.block_reasons == ("ready",)
        else "; ".join(blocker_labels)
    )
    lines.extend(
        (
            "",
            f"Подходящие материалы: {status.proactive_candidates}",
            f"Поводы для общего напоминания: {status.generic_candidates}",
            "",
            f"Сейчас блокирует: {blocker_text}",
            "",
            "Ручной просмотр Сегодня/Attention считается показом материала; "
            "недавно просмотренное может временно не приходить повторно.",
        )
    )
    return "\n".join(lines)


async def add_snooze_reminder(session, user_id: int, item_id: int, scheduled_at: datetime) -> None:
    """Persist the snooze delivery key in the same transaction as the action."""
    existing = await session.scalar(
        select(Reminder).where(
            Reminder.user_id == user_id,
            Reminder.item_id == item_id,
            Reminder.type == SNOOZE_RESURFACE,
            Reminder.scheduled_at == scheduled_at,
        )
    )
    if existing is not None:
        # Reusing a timestamp after Cancel/FAILED represents a new snooze
        # action, so the durable key can be reopened without inserting a
        # duplicate row.
        if existing.status in {"CANCELLED", "FAILED", "SENT"}:
            existing.status = "PENDING"
            existing.sent_at = None
        return
    session.add(
        Reminder(
            user_id=user_id,
            item_id=item_id,
            type=SNOOZE_RESURFACE,
            scheduled_at=scheduled_at,
            status="PENDING",
        )
    )


async def cancel_snooze_reminders(session, user_id: int, item_id: int) -> None:
    await session.execute(
        update(Reminder)
        .where(
            Reminder.user_id == user_id,
            Reminder.item_id == item_id,
            Reminder.type == SNOOZE_RESURFACE,
            Reminder.status == "PENDING",
        )
        .values(status="CANCELLED")
    )


class ReminderWorker:
    """Periodic asyncio worker; SQLite remains the scheduler and idempotency store."""

    def __init__(
        self,
        session_factory,
        bot,
        default_timezone: str = "UTC",
        poll_seconds: float = 30.0,
        max_send_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        attention_hook_service: AttentionHookService | None = None,
    ):
        self.session_factory = session_factory
        self.bot = bot
        self.default_timezone = default_timezone
        self.poll_seconds = poll_seconds
        self.max_send_attempts = max(1, max_send_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.attention_hook_service = attention_hook_service

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.process_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def process_once(self, now: datetime | None = None) -> int:
        if now is not None:
            now = _utc_naive(now)
        sent = 0
        async with self.session_factory() as session:
            user_ids = list((await session.scalars(select(User.id))).all())
        # Digest runs first so its successful delivery participates in this
        # cycle's PM-08 budget and minimum-gap checks.
        for user_id in user_ids:
            try:
                sent += await self._process_user(user_id, now)
            except SQLAlchemyError:
                raise
            except Exception:
                log.exception("digest processing failed user_id=%s", user_id)
        sent += await self._process_snoozes(now)
        # Snooze has no quota cost, but a successful resurfacing anchors the
        # minimum gap before the scheduler decides which Attention intervention to use.
        for user_id in user_ids:
            try:
                sent += await self._process_attention_intervention(user_id, now)
            except SQLAlchemyError:
                raise
            except Exception:
                log.exception("attention intervention failed user_id=%s", user_id)
        return sent

    async def _process_user(self, user_id: int, now: datetime | None) -> int:
        sent_at_override = now
        async with self.session_factory() as session:
            # Digest claim acquisition serializes with snooze/proactive claims;
            # the transaction ends before the Telegram request below.
            await session.execute(text("BEGIN IMMEDIATE"))
            now = _utc_naive(now) if now is not None else _utc_now()
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                return 0
            try:
                zone = parse_timezone(user.timezone or self.default_timezone)
            except ValueError:
                log.warning("invalid timezone user_id=%s; using default", user.id)
                zone = parse_timezone(self.default_timezone)
            local_now = now.replace(tzinfo=UTC).astimezone(zone)
            settings = settings_for(user)
            if not settings["daily_digest_enabled"]:
                return 0
            try:
                digest_time = parse_clock(settings["daily_digest_time"])
                quiet_start = parse_clock(settings["quiet_hours_start"])
                quiet_end = parse_clock(settings["quiet_hours_end"])
            except ValueError:
                log.warning("invalid notification clock user_id=%s; using defaults", user.id)
                settings = dict(_DEFAULT_SETTINGS)
                digest_time = parse_clock(settings["daily_digest_time"])
                quiet_start = parse_clock(settings["quiet_hours_start"])
                quiet_end = parse_clock(settings["quiet_hours_end"])
            target_date = digest_target_date(local_now, digest_time, quiet_start, quiet_end)
            digest_quiet = in_quiet_hours(digest_time, quiet_start, quiet_end)
            if in_quiet_hours(local_now.time(), quiet_start, quiet_end):
                # Durable evidence is created only after the configured time
                # has passed; a later morning poll can then recover exactly
                # this deferred local date without synthetic catch-up.
                deferred_date = None
                if local_now.time() >= digest_time:
                    deferred_date = local_now.date()
                elif quiet_start > quiet_end and local_now.time() < quiet_end:
                    # After midnight, yesterday's daily schedule is already
                    # due regardless of whether its time was before or inside
                    # the overnight quiet window.
                    deferred_date = local_now.date() - timedelta(days=1)
                if deferred_date is not None:
                    due_at = datetime.combine(deferred_date, digest_time, tzinfo=zone)
                    activation_at = user.daily_digest_enabled_at
                    if activation_at is None:
                        await session.commit()
                        return 0
                    activation_utc = activation_at.replace(tzinfo=UTC)
                    if activation_utc <= due_at.astimezone(UTC):
                        await self._defer_digest(
                            session, user.id, datetime.combine(deferred_date, time.min)
                        )
                await session.commit()
                return 0
            deferred = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user.id,
                    Reminder.type == DAILY_DIGEST,
                    Reminder.status == "PENDING",
                )
                .order_by(Reminder.scheduled_at)
            )
            if deferred is not None:
                target_date = deferred.scheduled_at.date()
            elif quiet_start > quiet_end and digest_quiet and local_now.time() >= quiet_end:
                # A digest configured inside overnight quiet hours was due on
                # the preceding local date; materialize that fact before claim.
                target_date = local_now.date() - timedelta(days=1)
                await self._defer_digest(session, user.id, datetime.combine(target_date, time.min))
            if target_date is None:
                await session.commit()
                return 0
            # For a digest, scheduled_at is a local-calendar-day identity,
            # not a UTC instant; this survives a timezone change within that day.
            scheduled_at = datetime.combine(target_date, time.min)
            lease_now = _utc_now()
            if await self._has_active_notification_claim(session, user.id, lease_now):
                await session.commit()
                return 0
            claim_generation = await self._claim_digest(session, user.id, scheduled_at, lease_now)
            if claim_generation is None:
                return 0
            items = await TodayService().list_items(session, user.id)
            sources_by_item = await load_item_sources_by_item(session, [item.id for item in items])
            await session.commit()
        try:
            await self._send_with_retry(
                user.telegram_chat_id,
                format_today(items, sources_by_item),
                claimed_at=lease_now,
                reply_markup=item_list_keyboard(item_navigation_entries(items, sources_by_item)),
            )
        except Exception:
            log.exception("daily digest delivery failed user_id=%s", user_id)
            await self._mark_failed(user_id, None, DAILY_DIGEST, scheduled_at, claim_generation)
            return 0
        sent_at = self._delivery_timestamp(sent_at_override)
        await self._mark_digest_sent(user_id, scheduled_at, sent_at, claim_generation)
        return 1

    @staticmethod
    def _delivery_timestamp(clock_override: datetime | None) -> datetime:
        """Record Telegram acceptance time while keeping explicit worker clocks deterministic."""
        return _utc_naive(clock_override) if clock_override is not None else _utc_now()

    async def _claim_digest(
        self, session, user_id: int, scheduled_at: datetime, now: datetime
    ) -> int | None:
        """Persist the digest's per-user send reservation before network I/O."""
        statement = (
            sqlite_insert(Reminder)
            .values(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=scheduled_at,
                status="CLAIMED",
                claimed_at=now,
                claim_generation=1,
                payload_json={"local_date": scheduled_at.date().isoformat()},
            )
            # item_id is NULL for a digest, so the table's general UNIQUE
            # constraint cannot identify it; the partial daily index does.
            .on_conflict_do_nothing()
            .returning(Reminder.claim_generation)
        )
        result = await session.execute(statement)
        generation = result.scalar_one_or_none()
        if generation is not None:
            return generation
        result = await session.execute(
            update(Reminder)
            .where(
                Reminder.user_id == user_id,
                Reminder.type == DAILY_DIGEST,
                Reminder.scheduled_at == scheduled_at,
                Reminder.status == "PENDING",
            )
            .values(
                status="CLAIMED",
                claimed_at=now,
                claim_generation=Reminder.claim_generation + 1,
                payload_json={"local_date": scheduled_at.date().isoformat()},
            )
            .returning(Reminder.claim_generation)
        )
        return result.scalar_one_or_none()

    async def _defer_digest(self, session, user_id: int, scheduled_at: datetime) -> None:
        """Persist a quiet-hours deferral so recovery has explicit evidence."""
        await session.execute(
            sqlite_insert(Reminder)
            .values(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=scheduled_at,
                status="PENDING",
                payload_json={"local_date": scheduled_at.date().isoformat()},
            )
            .on_conflict_do_nothing()
        )

    async def _mark_digest_sent(
        self, user_id: int, scheduled_at: datetime, sent_at: datetime, claim_generation: int
    ) -> None:
        """Count a digest only after Telegram accepts it, retaining its prior claim."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.item_id.is_(None),
                    Reminder.type == DAILY_DIGEST,
                    Reminder.scheduled_at == scheduled_at,
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status="SENT", sent_at=sent_at, claimed_at=None)
            )
            if result.rowcount == 1:
                reminder = await session.scalar(
                    select(Reminder)
                    .where(
                        Reminder.user_id == user_id,
                        Reminder.type == DAILY_DIGEST,
                        Reminder.scheduled_at == scheduled_at,
                        Reminder.status == "SENT",
                    )
                    .execution_options(populate_existing=True)
                )
                if reminder is not None:
                    record_reminder_event(session, reminder, "REMINDER_SENT", created_at=sent_at)
            if result.rowcount != 1:
                log.warning("digest finalization ignored after claim recovery user_id=%s", user_id)
            await session.commit()

    async def _attention_gate(
        self,
        session,
        user: User,
        now: datetime,
        *,
        exclude_claim_id: int | None = None,
    ) -> tuple[AttentionIntensityPolicy, ZoneInfo | None, date | None, str | None]:
        """Apply interruption-only rules before any PM-07 ranking work."""
        settings = settings_for(user)
        raw_level = settings.get("attention_intensity", 3)
        if type(raw_level) is not int or raw_level not in ATTENTION_POLICIES:
            log.warning("invalid attention intensity user_id=%s; using Normal", user.id)
            raw_level = 3
        policy = attention_policy(raw_level)
        if type(settings.get("attention_enabled")) is not bool:
            log.warning("invalid attention enabled setting user_id=%s; treating as OFF", user.id)
            return policy, None, None, "disabled"
        if not settings["attention_enabled"]:
            return policy, None, None, "disabled"

        try:
            zone = parse_timezone(user.timezone or self.default_timezone)
        except ValueError:
            log.warning("invalid timezone user_id=%s; using default", user.id)
            zone = parse_timezone(self.default_timezone)
        local_now = _utc_naive(now).replace(tzinfo=UTC).astimezone(zone)
        try:
            quiet_start = parse_clock(settings["quiet_hours_start"])
            quiet_end = parse_clock(settings["quiet_hours_end"])
        except ValueError:
            log.warning("invalid notification clock user_id=%s; using defaults", user.id)
            quiet_start = parse_clock(_DEFAULT_SETTINGS["quiet_hours_start"])
            quiet_end = parse_clock(_DEFAULT_SETTINGS["quiet_hours_end"])
        if in_quiet_hours(local_now.time(), quiet_start, quiet_end):
            return policy, zone, local_now.date(), "quiet_hours"

        # Claim leases use fresh wall time; `now` may be a deterministic policy
        # timestamp supplied by a test or worker replay.
        lease_now = _utc_now()
        if await self._has_active_notification_claim(
            session, user.id, lease_now, exclude_reminder_id=exclude_claim_id
        ):
            return policy, zone, local_now.date(), "delivery_in_progress"

        local_date, day_start, next_day_start = _local_day_window(now, zone)
        budget_used = await session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.user_id == user.id,
                Reminder.type.in_(_BUDGET_REMINDER_TYPES),
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
                Reminder.sent_at >= day_start,
                Reminder.sent_at < next_day_start,
            )
        )
        if budget_used >= policy.daily_cap:
            return policy, zone, local_date, "daily_cap"

        latest_sent_at = await session.scalar(
            select(func.max(Reminder.sent_at)).where(
                Reminder.user_id == user.id,
                Reminder.type.in_(_GAP_REMINDER_TYPES),
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
            )
        )
        if latest_sent_at is not None and now - _utc_naive(latest_sent_at) < policy.minimum_gap:
            return policy, zone, local_date, "minimum_gap"
        return policy, zone, local_date, None

    async def attention_status(
        self, telegram_user_id: int, now: datetime | None = None
    ) -> AttentionStatus | None:
        """Read current gating and candidates through scheduler helpers without acquiring claims."""
        instant = _utc_naive(now) if now is not None else _utc_now()
        async with self.session_factory() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            if user is None:
                return None
            settings = settings_for(user)
            policy, _gate_zone, _local_date, blocked = await self._attention_gate(
                session, user, instant
            )
            try:
                zone = parse_timezone(user.timezone or self.default_timezone)
            except ValueError:
                zone = parse_timezone(self.default_timezone)
            local_now = instant.replace(tzinfo=UTC).astimezone(zone)
            try:
                quiet_start = parse_clock(settings["quiet_hours_start"])
                quiet_end = parse_clock(settings["quiet_hours_end"])
            except ValueError:
                quiet_start = parse_clock(_DEFAULT_SETTINGS["quiet_hours_start"])
                quiet_end = parse_clock(_DEFAULT_SETTINGS["quiet_hours_end"])
            _, day_start, day_end = _local_day_window(instant, zone)
            budget_used = int(
                await session.scalar(
                    select(func.count(Reminder.id)).where(
                        Reminder.user_id == user.id,
                        Reminder.type.in_(_BUDGET_REMINDER_TYPES),
                        Reminder.status == "SENT",
                        Reminder.sent_at.is_not(None),
                        Reminder.sent_at >= day_start,
                        Reminder.sent_at < day_end,
                    )
                )
                or 0
            )
            generic_used = int(
                await session.scalar(
                    select(func.count(Reminder.id)).where(
                        Reminder.user_id == user.id,
                        Reminder.type == MOTIVATION_NUDGE,
                        Reminder.status == "SENT",
                        Reminder.sent_at.is_not(None),
                        Reminder.sent_at >= day_start,
                        Reminder.sent_at < day_end,
                    )
                )
                or 0
            )
            latest_reminder = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user.id,
                    Reminder.type.in_(_GAP_REMINDER_TYPES),
                    Reminder.status == "SENT",
                    Reminder.sent_at.is_not(None),
                )
                .order_by(Reminder.sent_at.desc(), Reminder.id.desc())
                .limit(1)
            )
            qualified, proactive = await self._proactive_candidate_projection(
                session, user.id, policy, instant
            )
            motivation = await self._motivation_candidates(session, user, policy, zone, instant)

            if user.telegram_chat_id is None:
                block_reasons = ("no_delivery_target",)
            elif blocked is not None:
                block_reasons = ("attention_off" if blocked == "disabled" else blocked,)
            else:
                latest_attention = await self._latest_sent_type(
                    session, user.id, _ATTENTION_FAMILY_TYPES
                )
                selected = self._choose_attention_intervention(
                    policy, bool(proactive), bool(motivation), latest_attention
                )
                if selected is not None:
                    block_reasons = ("ready",)
                else:
                    reasons = []
                    if not qualified:
                        reasons.append("no_proactive_candidate")
                    elif not proactive:
                        reasons.append("candidate_cooldown")
                    if not motivation:
                        reasons.append("no_generic_candidate")
                    block_reasons = tuple(reasons or ("no_proactive_candidate",))

            releases: list[datetime] = []
            quiet_release = _quiet_hours_release(local_now, quiet_start, quiet_end, zone)
            if quiet_release is not None:
                releases.append(quiet_release.astimezone(UTC))
            if budget_used >= policy.daily_cap:
                releases.append(day_end.replace(tzinfo=UTC))
            if latest_reminder is not None and latest_reminder.sent_at is not None:
                latest_sent_at = _utc_naive(latest_reminder.sent_at)
                if instant - latest_sent_at < policy.minimum_gap:
                    releases.append((latest_sent_at + policy.minimum_gap).replace(tzinfo=UTC))
            lease_now = _utc_now()
            active_claims = list(
                (
                    await session.scalars(
                        select(Reminder.claimed_at).where(
                            Reminder.user_id == user.id,
                            Reminder.type.in_(_DELIVERY_CLAIM_TYPES),
                            Reminder.status == "CLAIMED",
                            Reminder.claimed_at.is_not(None),
                            Reminder.claimed_at > lease_now - PROACTIVE_CLAIM_LEASE,
                        )
                    )
                ).all()
            )
            releases.extend(
                (_utc_naive(claimed_at) + PROACTIVE_CLAIM_LEASE).replace(tzinfo=UTC)
                for claimed_at in active_claims
                if claimed_at is not None
            )
            next_possible_at = max(releases).astimezone(zone) if releases else None
            return AttentionStatus(
                timezone_name=zone.key,
                local_now=local_now,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
                quiet_state=_quiet_hours_state(local_now.time(), quiet_start, quiet_end),
                policy=policy,
                budget_used=budget_used,
                generic_used=generic_used,
                generic_cap=GENERIC_MOTIVATION_DAILY_CAPS[policy.level],
                latest_type=latest_reminder.type if latest_reminder is not None else None,
                latest_sent_at=(
                    _utc_naive(latest_reminder.sent_at)
                    if latest_reminder is not None and latest_reminder.sent_at is not None
                    else None
                ),
                next_possible_at=next_possible_at,
                proactive_candidates=len(proactive),
                generic_candidates=len(motivation),
                block_reasons=block_reasons,
            )

    async def _has_active_notification_claim(
        self,
        session,
        user_id: int,
        now: datetime,
        *,
        exclude_reminder_id: int | None = None,
    ) -> bool:
        """Read the durable cross-type reservation used to serialize interruptions.

        SQLite claim writers run under BEGIN IMMEDIATE, so this read and their
        subsequent CLAIMED transition form one serialized send-intent boundary.
        Expired digest/snooze claims remain no-replay records but stop blocking.
        """
        statement = select(Reminder.id).where(
            Reminder.user_id == user_id,
            Reminder.type.in_(_DELIVERY_CLAIM_TYPES),
            Reminder.status == "CLAIMED",
            Reminder.claimed_at.is_not(None),
            Reminder.claimed_at > _utc_naive(now) - PROACTIVE_CLAIM_LEASE,
        )
        if exclude_reminder_id is not None:
            statement = statement.where(Reminder.id != exclude_reminder_id)
        return await session.scalar(statement.limit(1)) is not None

    async def _proactive_cooldowns(
        self, session, user_id: int, item_ids: list[int]
    ) -> dict[int, datetime]:
        """Read successful proactive history for a shortlist in one grouped query."""
        if not item_ids:
            return {}
        result = await session.execute(
            select(Reminder.item_id, func.max(Reminder.sent_at))
            .where(
                Reminder.user_id == user_id,
                Reminder.type == PROACTIVE_ATTENTION,
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
                Reminder.item_id.in_(item_ids),
            )
            .group_by(Reminder.item_id)
        )
        return {item_id: _utc_naive(sent_at) for item_id, sent_at in result.all()}

    async def _dismissal_cooldowns(
        self, session, user_id: int, item_ids: list[int], now: datetime
    ) -> dict[int, datetime]:
        """Use recent Reminder feedback as a scheduler-only 24-hour timing block."""
        return await ReminderFeedbackService.latest_dismissals(session, user_id, item_ids, now=now)

    @staticmethod
    def _proactive_cooldown_active(
        item_id: int,
        now: datetime,
        policy: AttentionIntensityPolicy,
        proactive_sent: Mapping[int, datetime],
        dismissed_at: Mapping[int, datetime],
    ) -> bool:
        """Apply max(PM-08 same-Item delay, PM-11 dismissal delay) without ranking effects."""
        instant = _utc_naive(now)
        last_sent = proactive_sent.get(item_id)
        if last_sent is not None and instant - last_sent < policy.same_item_cooldown:
            return True
        dismissal = dismissed_at.get(item_id)
        return dismissal is not None and instant - dismissal < REMINDER_DISMISSAL_COOLDOWN

    @staticmethod
    def _claim_is_stale(reminder: Reminder, now: datetime) -> bool:
        # ❌ Удален расчет lease по created_at: recovery не должен менять время
        # создания Reminder; claimed_at и generation отвечают за lease и owner.
        claimed_at = reminder.claimed_at
        return (
            claimed_at is None or _utc_naive(now) - _utc_naive(claimed_at) >= PROACTIVE_CLAIM_LEASE
        )

    @staticmethod
    def _item_is_actionable(item: Item | None) -> bool:
        return bool(
            item is not None
            and item.processing_status is ProcessingStatus.READY
            and item.state is ItemState.ACTIVE
            and item.item_type in ACTIONABLE_ITEM_TYPES
        )

    async def _proactive_candidate_projection(
        self, session, user_id: int, policy: AttentionIntensityPolicy, now: datetime
    ) -> tuple[list[tuple[int, AttentionRank]], list[tuple[int, AttentionRank]]]:
        """Share intensity, actionability and cooldown eligibility with read-only diagnostics."""
        ranked = await AttentionRankingService().list_candidates(
            session, user_id, limit=ATTENTION_CANDIDATE_LIMIT, now=now
        )
        qualified = [
            (item, rank)
            for item, rank in ranked
            if rank.score >= policy.minimum_proactive_score and self._item_is_actionable(item)
        ]
        cooldowns = await self._proactive_cooldowns(
            session, user_id, [item.id for item, _ in qualified]
        )
        item_ids = [item.id for item, _ in qualified]
        dismissals = await self._dismissal_cooldowns(session, user_id, item_ids, now)
        candidate_ids = [(item.id, rank) for item, rank in qualified]
        sendable = [
            (item.id, rank)
            for item, rank in qualified
            if not self._proactive_cooldown_active(item.id, now, policy, cooldowns, dismissals)
        ]
        return candidate_ids, sendable

    async def _sendable_proactive_candidates(
        self, session, user_id: int, policy: AttentionIntensityPolicy, now: datetime
    ) -> list[tuple[int, AttentionRank]]:
        """Project PM-07's current shortlist through only PM-08 sendability filters."""
        _candidates, sendable = await self._proactive_candidate_projection(
            session, user_id, policy, now
        )
        return sendable

    async def _latest_sent_type(self, session, user_id: int, types: tuple[str, ...]) -> str | None:
        """Read the latest successful family member for deterministic pacing/arbitration."""
        return await session.scalar(
            select(Reminder.type)
            .where(
                Reminder.user_id == user_id,
                Reminder.type.in_(types),
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
            )
            .order_by(Reminder.sent_at.desc(), Reminder.id.desc())
            .limit(1)
        )

    async def _motivation_candidates(
        self,
        session,
        user: User,
        policy: AttentionIntensityPolicy,
        zone: ZoneInfo,
        now: datetime,
    ):
        """Apply only the generic opt-in/cap before asking the fact service for candidates."""
        settings = settings_for(user)
        if type(settings.get("generic_motivation_enabled")) is not bool:
            log.warning("invalid generic motivation setting user_id=%s; treating as OFF", user.id)
            return []
        generic_cap = GENERIC_MOTIVATION_DAILY_CAPS[policy.level]
        if not settings["generic_motivation_enabled"] or generic_cap == 0:
            return []
        local_date, day_start, day_end = _local_day_window(now, zone)
        sent_today = await session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.user_id == user.id,
                Reminder.type == MOTIVATION_NUDGE,
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
                Reminder.sent_at >= day_start,
                Reminder.sent_at < day_end,
            )
        )
        if sent_today >= generic_cap:
            return []
        latest_generic = await session.scalar(
            select(func.max(Reminder.sent_at)).where(
                Reminder.user_id == user.id,
                Reminder.type == MOTIVATION_NUDGE,
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
            )
        )
        if (
            latest_generic is not None
            and _utc_naive(now) - _utc_naive(latest_generic) < GENERIC_REPEAT_GAP
        ):
            return []
        # ❌ Удалено чередование типов как условие следующего generic reminder:
        # проверяется явный четырёхчасовой интервал, а cap остаётся отдельным.
        candidates = await MotivationService().candidates(session, user.id, zone=zone, now=now)
        suppressed = await ReminderFeedbackService.suppressed_motivation_kinds(
            session, user.id, now=now
        )
        return [candidate for candidate in candidates if candidate.kind not in suppressed]

    @staticmethod
    def _choose_attention_intervention(
        policy: AttentionIntensityPolicy,
        has_proactive: bool,
        has_motivation: bool,
        latest_attention_type: str | None,
    ) -> str | None:
        """Apply PM-10 arbitration without changing PM-07 rank or PM-08 eligibility."""
        if policy.level <= 3:
            if has_proactive:
                return PROACTIVE_ATTENTION
            return MOTIVATION_NUDGE if has_motivation else None
        preferred = (
            PROACTIVE_ATTENTION
            if latest_attention_type != PROACTIVE_ATTENTION
            else MOTIVATION_NUDGE
        )
        if preferred == PROACTIVE_ATTENTION:
            if has_proactive:
                return PROACTIVE_ATTENTION
            return MOTIVATION_NUDGE if has_motivation else None
        if has_motivation:
            return MOTIVATION_NUDGE
        return PROACTIVE_ATTENTION if has_proactive else None

    async def _cancel_stale_open_claim(self, reminder_id: int, type_: str, generation: int) -> None:
        """Fence cancellation of an obsolete claim against a concurrent recovery owner."""
        statuses = (
            _OPEN_MOTIVATION_STATUSES if type_ == MOTIVATION_NUDGE else _OPEN_PROACTIVE_STATUSES
        )
        stale_before = _utc_now() - PROACTIVE_CLAIM_LEASE
        async with self.session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.type == type_,
                    Reminder.status.in_(statuses),
                    Reminder.claim_generation == generation,
                    (Reminder.claimed_at.is_(None) | (Reminder.claimed_at <= stale_before)),
                )
                .values(status="CANCELLED", claimed_at=None)
            )
            await session.commit()

    async def _process_attention_intervention(self, user_id: int, now: datetime | None) -> int:
        """Choose one current Item or backlog intervention after digest and snooze phases."""
        evaluation_now = _utc_naive(now) if now is not None else _utc_now()
        stale_to_cancel: list[tuple[int, str, int]] = []
        blocked = None
        selected = None
        recover_proactive_claim = False
        async with self.session_factory() as session:
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                return 0
            proactive_claim = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status.in_(_OPEN_PROACTIVE_STATUSES),
                )
                .order_by(Reminder.id)
                .limit(1)
            )
            motivation_claim = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.status.in_(_OPEN_MOTIVATION_STATUSES),
                )
                .order_by(Reminder.id)
                .limit(1)
            )
            lease_now = _utc_now()
            if any(
                claim is not None and not self._claim_is_stale(claim, lease_now)
                for claim in (proactive_claim, motivation_claim)
            ):
                return 0
            policy, zone, _, blocked = await self._attention_gate(session, user, evaluation_now)
            if blocked is not None:
                if blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    for claim in (proactive_claim, motivation_claim):
                        if claim is not None:
                            type_ = claim.type
                            stale_to_cancel.append((claim.id, type_, claim.claim_generation))
            else:
                proactive = await self._sendable_proactive_candidates(
                    session, user_id, policy, evaluation_now
                )
                motivation = await self._motivation_candidates(
                    session, user, policy, zone, evaluation_now
                )
                latest_attention = await self._latest_sent_type(
                    session, user_id, _ATTENTION_FAMILY_TYPES
                )
                selected = self._choose_attention_intervention(
                    policy, bool(proactive), bool(motivation), latest_attention
                )
                if motivation_claim is not None and selected != MOTIVATION_NUDGE:
                    stale_to_cancel.append(
                        (
                            motivation_claim.id,
                            motivation_claim.type,
                            motivation_claim.claim_generation,
                        )
                    )
                if selected is None and proactive_claim is not None:
                    recover_proactive_claim = True
            await session.commit()

        for reminder_id, type_, generation in stale_to_cancel:
            await self._cancel_stale_open_claim(reminder_id, type_, generation)
        if blocked is not None:
            return 0

        if selected == MOTIVATION_NUDGE:
            return await self._process_motivation_nudge(user_id, now)
        # ❌ Удален безусловный proactive-вызов: выбор типа перед отправкой
        # гарантирует не более одного Attention intervention за цикл.
        if selected == PROACTIVE_ATTENTION or recover_proactive_claim:
            return await self._process_proactive_attention(user_id, now)
        return 0

    @staticmethod
    def _motivation_slot_at(local_date: date, slot: int) -> datetime:
        """Encode a durable local-day/ordinal identity in the existing schedule column."""
        # PM-10 scheduled_at is a slot key here, not a due instant; seconds 1..2
        # distinguish bounded daily slots while preserving the local date identity.
        return datetime.combine(local_date, time.min) + timedelta(seconds=slot)

    async def _claim_motivation_nudge(
        self, user_id: int, now: datetime
    ) -> tuple[int, int, datetime, MotivationCandidate, int, int, datetime, int] | None:
        """Recompute arbitration and persist one fenced user-level send claim."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                await session.commit()
                return None
            open_claim = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.status.in_(_OPEN_MOTIVATION_STATUSES),
                )
                .order_by(Reminder.id)
                .limit(1)
            )
            lease_now = _utc_now()
            if open_claim is not None and not self._claim_is_stale(open_claim, lease_now):
                await session.commit()
                return None
            policy, zone, local_date, blocked = await self._attention_gate(
                session,
                user,
                now,
                exclude_claim_id=open_claim.id if open_claim is not None else None,
            )
            if blocked is not None or zone is None or local_date is None:
                if open_claim is not None and blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None

            candidates = await self._motivation_candidates(session, user, policy, zone, now)
            if not candidates:
                if open_claim is not None:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None
            proactive = await self._sendable_proactive_candidates(session, user_id, policy, now)
            latest_attention = await self._latest_sent_type(
                session, user_id, _ATTENTION_FAMILY_TYPES
            )
            selected = self._choose_attention_intervention(
                policy, bool(proactive), True, latest_attention
            )
            if selected != MOTIVATION_NUDGE:
                if open_claim is not None:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None

            _, day_start, day_end = _local_day_window(now, zone)
            sent_today = int(
                await session.scalar(
                    select(func.count(Reminder.id)).where(
                        Reminder.user_id == user_id,
                        Reminder.type == MOTIVATION_NUDGE,
                        Reminder.status == "SENT",
                        Reminder.sent_at.is_not(None),
                        Reminder.sent_at >= day_start,
                        Reminder.sent_at < day_end,
                    )
                )
                or 0
            )
            generic_cap = GENERIC_MOTIVATION_DAILY_CAPS[policy.level]
            slot = sent_today + 1
            if slot > generic_cap:
                if open_claim is not None:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None

            candidate = candidates[0]
            scheduled_at = self._motivation_slot_at(local_date, slot)
            if open_claim is not None and open_claim.scheduled_at != scheduled_at:
                # A claim from a previous local day is cancelled; current-day
                # work is a fresh fact evaluation, never a queued catch-up.
                open_claim.status = "CANCELLED"
                open_claim.claimed_at = None
                open_claim = None
            reminder = await session.scalar(
                select(Reminder).where(
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.item_id.is_(None),
                    Reminder.scheduled_at == scheduled_at,
                )
            )
            if reminder is not None and reminder.status == "SENT":
                await session.commit()
                return None
            if reminder is not None and reminder.status in _OPEN_MOTIVATION_STATUSES:
                if not self._claim_is_stale(reminder, lease_now):
                    await session.commit()
                    return None
            claimed_at = _utc_now()
            payload = {
                "kind": candidate.kind.value,
                "facts": dict(candidate.facts),
                "template_id": candidate.template_id,
                "policy_level": policy.level,
                "local_date": local_date.isoformat(),
                "slot": slot,
            }
            if reminder is None:
                reminder = Reminder(
                    user_id=user_id,
                    item_id=None,
                    type=MOTIVATION_NUDGE,
                    scheduled_at=scheduled_at,
                    status="CLAIMED",
                    payload_json=payload,
                    created_at=now,
                    claimed_at=claimed_at,
                    claim_generation=1,
                )
                session.add(reminder)
            else:
                reminder.status = "CLAIMED"
                reminder.claimed_at = claimed_at
                reminder.claim_generation = (reminder.claim_generation or 0) + 1
                reminder.sent_at = None
                reminder.payload_json = payload
            await session.flush()
            result = (
                reminder.id,
                reminder.claim_generation,
                reminder.scheduled_at,
                candidate,
                policy.level,
                user.telegram_chat_id,
                claimed_at,
                slot,
            )
            await session.commit()
            return result

    async def _prepare_motivation_send(
        self,
        reminder_id: int,
        user_id: int,
        claim_generation: int,
        now: datetime | None,
    ) -> tuple[MotivationCandidate, int, int, datetime, int, int] | None:
        """Refresh facts and every live PM-08 condition before releasing SQLite for Telegram."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            prepare_now = _utc_naive(now) if now is not None else _utc_now()
            reminder = await session.get(Reminder, reminder_id)
            user = await session.get(User, user_id)
            if (
                reminder is None
                or reminder.user_id != user_id
                or reminder.type != MOTIVATION_NUDGE
                or reminder.item_id is not None
                or reminder.status != "CLAIMED"
                or reminder.claim_generation != claim_generation
                or user is None
                or user.telegram_chat_id is None
            ):
                await session.commit()
                return None
            if self._claim_is_stale(reminder, prepare_now):
                await session.commit()
                return None
            policy, zone, local_date, blocked = await self._attention_gate(
                session, user, prepare_now, exclude_claim_id=reminder_id
            )
            if blocked is not None or zone is None or local_date is None:
                if blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    reminder.status = "CANCELLED"
                    reminder.claimed_at = None
                await session.commit()
                return None
            claim_payload = reminder.payload_json or {}
            if claim_payload.get("local_date") != local_date.isoformat():
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None

            candidates = await self._motivation_candidates(session, user, policy, zone, prepare_now)
            if not candidates:
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None
            proactive = await self._sendable_proactive_candidates(
                session, user_id, policy, prepare_now
            )
            latest_attention = await self._latest_sent_type(
                session, user_id, _ATTENTION_FAMILY_TYPES
            )
            selected = self._choose_attention_intervention(
                policy, bool(proactive), True, latest_attention
            )
            if selected != MOTIVATION_NUDGE:
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None

            _, day_start, day_end = _local_day_window(prepare_now, zone)
            sent_today = int(
                await session.scalar(
                    select(func.count(Reminder.id)).where(
                        Reminder.user_id == user_id,
                        Reminder.type == MOTIVATION_NUDGE,
                        Reminder.status == "SENT",
                        Reminder.sent_at.is_not(None),
                        Reminder.sent_at >= day_start,
                        Reminder.sent_at < day_end,
                    )
                )
                or 0
            )
            generic_cap = GENERIC_MOTIVATION_DAILY_CAPS[policy.level]
            if sent_today >= generic_cap:
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None

            candidate = candidates[0]
            slot = claim_payload.get("slot")
            if (
                type(slot) is not int
                or not 1 <= slot <= generic_cap
                or slot != sent_today + 1
                or reminder.scheduled_at != self._motivation_slot_at(local_date, slot)
            ):
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None
            reminder.payload_json = {
                "kind": candidate.kind.value,
                "facts": dict(candidate.facts),
                "template_id": candidate.template_id,
                "policy_level": policy.level,
                "local_date": local_date.isoformat(),
                "slot": slot,
            }
            await session.commit()
            return (
                candidate,
                user.telegram_chat_id,
                claim_generation,
                reminder.claimed_at,
                slot,
                policy.level,
            )

    async def _finalize_motivation_send(
        self, reminder_id: int, user_id: int, claim_generation: int, sent_at: datetime
    ) -> None:
        """Record only successful Telegram delivery as PM-10 budget and pacing history."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.user_id == user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.item_id.is_(None),
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status="SENT", sent_at=sent_at, claimed_at=None)
            )
            if result.rowcount == 1:
                reminder = await session.scalar(
                    select(Reminder)
                    .where(Reminder.id == reminder_id, Reminder.status == "SENT")
                    .execution_options(populate_existing=True)
                )
                if reminder is not None:
                    record_reminder_event(session, reminder, "REMINDER_SENT", created_at=sent_at)
            if result.rowcount != 1:
                log.warning(
                    "motivation finalization ignored after claim recovery reminder_id=%s",
                    reminder_id,
                )
            await session.commit()

    async def _process_motivation_nudge(self, user_id: int, now: datetime | None) -> int:
        """Run one durable PM-10 claim, final revalidation, Telegram send, and fencing step."""
        sent_at_override = now
        claim_now = _utc_naive(now) if now is not None else _utc_now()
        claimed = await self._claim_motivation_nudge(user_id, claim_now)
        if claimed is None:
            return 0
        (
            reminder_id,
            generation,
            scheduled_at,
            candidate,
            policy_level,
            chat_id,
            claimed_at,
            slot,
        ) = claimed
        prepared = await self._prepare_motivation_send(
            reminder_id, user_id, generation, sent_at_override
        )
        if prepared is None:
            return 0
        candidate, chat_id, generation, claimed_at, slot, policy_level = prepared
        try:
            await self._send_with_retry(
                chat_id,
                candidate.rendered_text,
                claimed_at=claimed_at,
                reply_markup=motivation_reminder_keyboard(reminder_id),
            )
        except Exception:
            log.exception(
                "motivation delivery failed user_id=%s reminder_id=%s kind=%s",
                user_id,
                reminder_id,
                candidate.kind.value,
            )
            await self._mark_failed(user_id, None, MOTIVATION_NUDGE, scheduled_at, generation)
            return 0
        sent_at = self._delivery_timestamp(sent_at_override)
        await self._finalize_motivation_send(reminder_id, user_id, generation, sent_at)
        log.info(
            "motivation nudge sent user_id=%s reminder_id=%s kind=%s template_id=%s "
            "policy_level=%s slot=%s",
            user_id,
            reminder_id,
            candidate.kind.value,
            candidate.template_id,
            policy_level,
            slot,
        )
        return 1

    # ❌ Удален дополнительный post-send запрос слота: значение уже захвачено
    # вместе с claim, и чтение после Telegram могло бы исказить успех доставки.

    async def _process_proactive_attention(self, user_id: int, now: datetime | None) -> int:
        """Rank only when policy allows, then create or recover one durable claim."""
        sent_at_override = now
        now = _utc_naive(now) if now is not None else _utc_now()
        async with self.session_factory() as session:
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                return 0
            open_claim = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status.in_(_OPEN_PROACTIVE_STATUSES),
                )
                .order_by(Reminder.id)
                .limit(1)
            )
            _, _, _, blocked = await self._attention_gate(
                session,
                user,
                now,
                exclude_claim_id=open_claim.id if open_claim is not None else None,
            )
            lease_now = _utc_now()
            if open_claim is not None and not self._claim_is_stale(open_claim, lease_now):
                item = await session.get(Item, open_claim.item_id)
                if blocked == "disabled" or not self._item_is_actionable(item):
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                    await session.commit()
                return 0
            if blocked is not None:
                # Quiet-hours and cross-type reservations are transient; a
                # changed setting or exhausted pacing invalidates the old claim.
                if open_claim is not None and blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                    await session.commit()
                return 0

            ranked = await AttentionRankingService().list_candidates(
                session, user_id, limit=ATTENTION_CANDIDATE_LIMIT, now=now
            )
            ranked_ids = [(item.id, rank) for item, rank in ranked]
            if not ranked_ids and open_claim is None:
                return 0
            await session.commit()

        claimed = await self._claim_proactive_attention(user_id, ranked_ids, now)
        if claimed is None:
            return 0
        reminder_id, item_id, rank, policy_level, claim_generation, claimed_at = claimed
        hook_presentation = None
        if self.attention_hook_service is not None:
            remaining_seconds = (
                claimed_at + NOTIFICATION_SEND_TIMEOUT - _utc_now()
            ).total_seconds()
            hook_timeout = max(
                0.0,
                min(
                    ATTENTION_HOOK_TIMEOUT_SECONDS,
                    remaining_seconds - ATTENTION_HOOK_SEND_SAFETY_SECONDS,
                ),
            )
            hook_presentation = await self.attention_hook_service.for_reminder(
                item_id=item_id,
                user_id=user_id,
                reminder_id=reminder_id,
                claim_generation=claim_generation,
                timeout_seconds=hook_timeout,
            )
        focus_source_id = (
            hook_presentation.hook.source_id if hook_presentation is not None else None
        )
        prepared = await self._prepare_proactive_send(
            reminder_id, user_id, item_id, claim_generation, sent_at_override
        )
        if prepared is None:
            return 0
        item, sources, chat_id, rank, policy_level = prepared
        try:
            await self._send_with_retry(
                chat_id,
                format_proactive_attention_reminder(
                    item,
                    hook_text=(
                        hook_presentation.rendered_text if hook_presentation is not None else None
                    ),
                    sources=sources,
                ),
                claimed_at=claimed_at,
                reply_markup=proactive_reminder_keyboard(
                    reminder_id,
                    item,
                    sources,
                    focus_source_id=focus_source_id,
                    original_available=chat_id is not None,
                ),
            )
        except Exception:
            log.exception(
                "proactive delivery failed user_id=%s item_id=%s reminder_id=%s",
                user_id,
                item_id,
                reminder_id,
            )
            await self._set_proactive_status(reminder_id, claim_generation, "FAILED")
            return 0
        sent_at = self._delivery_timestamp(sent_at_override)
        await self._finalize_proactive_send(
            reminder_id, user_id, item_id, claim_generation, sent_at
        )
        log.info(
            "proactive reminder sent user_id=%s item_id=%s reminder_id=%s "
            "policy_level=%s attention_score=%s",
            user_id,
            item_id,
            reminder_id,
            policy_level,
            rank.score,
        )
        return 1

    async def _claim_proactive_attention(
        self, user_id: int, ranked: list[tuple[int, AttentionRank]], now: datetime
    ) -> tuple[int, int, AttentionRank, int, int, datetime] | None:
        """Serialize current settings, pacing facts, cooldowns, and one durable claim."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                await session.commit()
                return None
            open_claim = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status.in_(_OPEN_PROACTIVE_STATUSES),
                )
                .order_by(Reminder.id)
                .limit(1)
            )
            policy, _, local_date, blocked = await self._attention_gate(
                session,
                user,
                now,
                exclude_claim_id=open_claim.id if open_claim is not None else None,
            )
            if blocked is not None:
                if open_claim is not None and blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None
            lease_now = _utc_now()
            if open_claim is not None and not self._claim_is_stale(open_claim, lease_now):
                await session.commit()
                return None

            qualified = [
                (item_id, rank)
                for item_id, rank in ranked
                if rank.score >= policy.minimum_proactive_score
            ]
            item_ids = [item_id for item_id, _ in qualified]
            eligible_ids = set()
            if item_ids:
                eligible_ids = set(
                    (
                        await session.scalars(
                            select(Item.id).where(
                                Item.id.in_(item_ids),
                                Item.user_id == user_id,
                                Item.processing_status == ProcessingStatus.READY,
                                Item.state == ItemState.ACTIVE,
                                Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
                            )
                        )
                    ).all()
                )
            cooldowns = await self._proactive_cooldowns(session, user_id, item_ids)
            dismissals = await self._dismissal_cooldowns(session, user_id, item_ids, now)

            chosen: tuple[int, AttentionRank] | None = None
            if open_claim is not None and open_claim.item_id is not None:
                old_rank = next(
                    (rank for item_id, rank in qualified if item_id == open_claim.item_id),
                    None,
                )
                if (
                    old_rank is not None
                    and open_claim.item_id in eligible_ids
                    and not self._proactive_cooldown_active(
                        open_claim.item_id, now, policy, cooldowns, dismissals
                    )
                ):
                    chosen = (open_claim.item_id, old_rank)
                else:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None

            if chosen is None:
                chosen = next(
                    (
                        (item_id, rank)
                        for item_id, rank in qualified
                        if item_id in eligible_ids
                        and not self._proactive_cooldown_active(
                            item_id, now, policy, cooldowns, dismissals
                        )
                    ),
                    None,
                )
            if chosen is None:
                await session.commit()
                return None

            item_id, rank = chosen
            item = await session.get(Item, item_id)
            if not self._item_is_actionable(item):
                if open_claim is not None:
                    open_claim.status = "CANCELLED"
                    open_claim.claimed_at = None
                await session.commit()
                return None
            payload = {
                "attention_score": rank.score,
                "priority_score": rank.priority_score,
                "interest_level": item.interest_level,
                "category": item.category,
                "item_type": item.item_type.value if item.item_type is not None else None,
                "policy_level": policy.level,
                "reason": format_attention_reason(rank)[:240],
                "budget_local_date": local_date.isoformat(),
            }
            if open_claim is not None and open_claim.item_id == item_id:
                old_payload = open_claim.payload_json or {}
                for key in ("hook_content_id", "template_id"):
                    if key in old_payload:
                        payload[key] = old_payload[key]
            recovered_id = (
                open_claim.id if open_claim is not None and open_claim.item_id == item_id else None
            )
            scheduled_at = await self._next_proactive_schedule(
                session, user_id, item_id, now, recovered_id
            )
            claimed_at = _utc_now()
            if open_claim is not None and open_claim.item_id == item_id:
                open_claim.status = "CLAIMED"
                open_claim.scheduled_at = scheduled_at
                open_claim.claim_generation = (open_claim.claim_generation or 0) + 1
                open_claim.claimed_at = claimed_at
                open_claim.sent_at = None
                open_claim.payload_json = payload
                reminder = open_claim
            else:
                reminder = Reminder(
                    user_id=user_id,
                    item_id=item_id,
                    type=PROACTIVE_ATTENTION,
                    scheduled_at=scheduled_at,
                    status="CLAIMED",
                    payload_json=payload,
                    created_at=now,
                    claimed_at=claimed_at,
                    claim_generation=1,
                )
                session.add(reminder)
            await session.flush()
            result = (
                reminder.id,
                item_id,
                rank,
                policy.level,
                reminder.claim_generation,
                claimed_at,
            )
            await session.commit()
            return result

    async def _next_proactive_schedule(
        self,
        session,
        user_id: int,
        item_id: int,
        now: datetime,
        current_reminder_id: int | None,
    ) -> datetime:
        """Keep the existing per-Item Reminder key unique across failed retries."""
        query = select(func.max(Reminder.scheduled_at)).where(
            Reminder.user_id == user_id,
            Reminder.item_id == item_id,
            Reminder.type == PROACTIVE_ATTENTION,
        )
        if current_reminder_id is not None:
            query = query.where(Reminder.id != current_reminder_id)
        latest = await session.scalar(query)
        if latest is not None and _utc_naive(latest) >= now:
            return _utc_naive(latest) + timedelta(microseconds=1)
        return now

    async def _prepare_proactive_send(
        self,
        reminder_id: int,
        user_id: int,
        item_id: int,
        claim_generation: int,
        now: datetime | None,
    ) -> tuple[Item, list[ItemSource], int, AttentionRank, int] | None:
        """Revalidate current PM-07 rank and live claim before releasing SQLite for I/O."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            # This serialized snapshot governs every time-sensitive send decision;
            # the worker-cycle timestamp may have crossed quiet hours or a PM-08 boundary.
            prepare_now = _utc_naive(now) if now is not None else _utc_now()
            reminder = await session.get(Reminder, reminder_id)
            user = await session.get(User, user_id)
            item = await session.get(Item, item_id)
            if (
                reminder is None
                or reminder.status not in _OPEN_PROACTIVE_STATUSES
                or reminder.user_id != user_id
                or reminder.type != PROACTIVE_ATTENTION
                or reminder.item_id != item_id
                or reminder.status != "CLAIMED"
                or reminder.claim_generation != claim_generation
                or user is None
                or user.telegram_chat_id is None
            ):
                await session.commit()
                return None
            if self._claim_is_stale(reminder, prepare_now):
                # Leave ownership untouched: the next cycle can recover this
                # generation after the writer lock is released.
                await session.commit()
                return None
            policy, _, _, blocked = await self._attention_gate(
                session, user, prepare_now, exclude_claim_id=reminder_id
            )
            if blocked is not None:
                if blocked not in _TRANSIENT_ATTENTION_BLOCKS:
                    reminder.status = "CANCELLED"
                    reminder.claimed_at = None
                await session.commit()
                return None
            if not self._item_is_actionable(item):
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None
            current_candidate = await AttentionRankingService().rank_item(
                session, user_id, item_id, now=prepare_now
            )
            if (
                current_candidate is None
                or current_candidate[1].score < policy.minimum_proactive_score
            ):
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None
            item, current_rank = current_candidate
            cooldowns = await self._proactive_cooldowns(session, user_id, [item_id])
            dismissals = await self._dismissal_cooldowns(session, user_id, [item_id], prepare_now)
            if self._proactive_cooldown_active(item_id, prepare_now, policy, cooldowns, dismissals):
                reminder.status = "CANCELLED"
                reminder.claimed_at = None
                await session.commit()
                return None
            sources = list(
                (
                    await session.scalars(
                        select(ItemSource)
                        .where(ItemSource.item_id == item_id)
                        .order_by(ItemSource.source_index, ItemSource.id)
                    )
                ).all()
            )
            chat_id = user.telegram_chat_id
            payload = dict(reminder.payload_json or {})
            payload.update(
                {
                    "attention_score": current_rank.score,
                    "priority_score": current_rank.priority_score,
                    "interest_level": item.interest_level,
                    "category": item.category,
                    "item_type": item.item_type.value if item.item_type is not None else None,
                    "policy_level": policy.level,
                    "reason": format_attention_reason(current_rank)[:240],
                }
            )
            reminder.payload_json = payload
            if self._claim_is_stale(reminder, prepare_now):
                await session.commit()
                return None
            await session.commit()
            return item, sources, chat_id, current_rank, policy.level

    async def _set_proactive_status(
        self, reminder_id: int, claim_generation: int, status: str
    ) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status=status, claimed_at=None)
            )
            if result.rowcount != 1:
                log.info(
                    "proactive claim status ignored after recovery reminder_id=%s", reminder_id
                )
            await session.commit()

    async def _finalize_proactive_send(
        self,
        reminder_id: int,
        user_id: int,
        item_id: int,
        claim_generation: int,
        sent_at: datetime,
    ) -> None:
        """Commit delivery history and PM-07 exposure as one idempotent fact."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.user_id == user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status="SENT", sent_at=sent_at, claimed_at=None)
            )
            if result.rowcount == 1:
                session.add(
                    Event(
                        user_id=user_id,
                        item_id=item_id,
                        event_type="ATTENTION_SHOWN",
                        payload_json={"source": "proactive_attention", "reminder_id": reminder_id},
                        idempotency_key=f"reminder:{reminder_id}:attention_shown",
                        created_at=sent_at,
                    )
                )
                reminder = await session.scalar(
                    select(Reminder)
                    .where(Reminder.id == reminder_id, Reminder.status == "SENT")
                    .execution_options(populate_existing=True)
                )
                if reminder is not None:
                    record_reminder_event(session, reminder, "REMINDER_SENT", created_at=sent_at)
            await session.commit()

    async def _process_snoozes(self, now: datetime | None) -> int:
        sent_at_override = now
        now = _utc_naive(now) if now is not None else _utc_now()
        async with self.session_factory() as session:
            reminder_ids = list(
                (
                    await session.scalars(
                        select(Reminder.id)
                        .where(
                            Reminder.type == SNOOZE_RESURFACE,
                            Reminder.status == "PENDING",
                            Reminder.scheduled_at <= now,
                        )
                        .order_by(Reminder.scheduled_at, Reminder.id)
                    )
                ).all()
            )
            await session.commit()
        delivered = 0
        # ❌ Удален пакетный захват всех due snooze: отдельная транзакция на
        # отправку сериализует её с digest/proactive и не открывает окно обхода пауз.
        for reminder_id in reminder_ids:
            claimed = await self._claim_snooze(reminder_id, now)
            if claimed is None:
                continue
            (
                user_id,
                item_id,
                scheduled_at,
                claim_generation,
                claimed_at,
                title,
                chat_id,
                keyboard,
            ) = claimed
            try:
                await self._send_with_retry(
                    chat_id,
                    f"⏰ Вернулся отложенный Item: {title}",
                    claimed_at=claimed_at,
                    reply_markup=keyboard,
                )
            except Exception:
                log.exception("snooze notification failed item_id=%s", item_id)
                await self._mark_failed(
                    user_id, item_id, SNOOZE_RESURFACE, scheduled_at, claim_generation
                )
            else:
                sent_at = self._delivery_timestamp(sent_at_override)
                await self._mark_snooze_sent(
                    user_id, item_id, scheduled_at, sent_at, claim_generation
                )
                delivered += 1
        return delivered

    async def _claim_snooze(
        self, reminder_id: int, now: datetime
    ) -> tuple[int, int, datetime, int, datetime, str, int, object] | None:
        """Claim one due snooze under the same per-user reservation as proactive sends."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            reminder = await session.get(Reminder, reminder_id)
            if (
                reminder is None
                or reminder.type != SNOOZE_RESURFACE
                or reminder.status != "PENDING"
                or reminder.scheduled_at > now
            ):
                await session.commit()
                return None
            user = await session.get(User, reminder.user_id)
            item = await session.get(Item, reminder.item_id)
            if user is None or user.telegram_chat_id is None or item is None:
                reminder.status = "CANCELLED"
                await session.commit()
                return None
            lease_now = _utc_now()
            if await self._has_active_notification_claim(session, user.id, lease_now):
                await session.commit()
                return None
            try:
                zone = parse_timezone(user.timezone or self.default_timezone)
                local_now = _utc_naive(now).replace(tzinfo=UTC).astimezone(zone)
                settings = settings_for(user)
                quiet = in_quiet_hours(
                    local_now.time(),
                    parse_clock(settings["quiet_hours_start"]),
                    parse_clock(settings["quiet_hours_end"]),
                )
            except ValueError:
                quiet = False
            if quiet:
                await session.commit()
                return None
            if (
                item.state is not ItemState.SNOOZED
                or item.snoozed_until is None
                or item.snoozed_until > now
            ):
                reminder.status = "CANCELLED"
                await session.commit()
                return None
            claimed_at = _utc_now()
            reminder.status = "CLAIMED"
            reminder.claimed_at = claimed_at
            reminder.claim_generation = (reminder.claim_generation or 0) + 1
            reminder.sent_at = None
            item.state = ItemState.ACTIVE
            item.snoozed_until = None
            sources = list(
                (
                    await session.scalars(
                        select(ItemSource)
                        .where(ItemSource.item_id == item.id)
                        .order_by(ItemSource.source_index, ItemSource.id)
                    )
                ).all()
            )
            keyboard = item_keyboard(
                item,
                sources,
                original_available=user.telegram_chat_id is not None,
            )
            await session.flush()
            claimed = (
                user.id,
                item.id,
                reminder.scheduled_at,
                reminder.claim_generation,
                claimed_at,
                item_display_title(item, sources),
                user.telegram_chat_id,
                keyboard,
            )
            await session.commit()
            return claimed

    async def _mark_snooze_sent(
        self,
        user_id: int,
        item_id: int,
        scheduled_at: datetime,
        sent_at: datetime,
        claim_generation: int,
    ) -> None:
        """Make a snooze reminder a gap anchor only after Telegram accepts it."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.item_id == item_id,
                    Reminder.type == SNOOZE_RESURFACE,
                    Reminder.scheduled_at == scheduled_at,
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status="SENT", sent_at=sent_at, claimed_at=None)
            )
            if result.rowcount == 1:
                reminder = await session.scalar(
                    select(Reminder)
                    .where(
                        Reminder.user_id == user_id,
                        Reminder.item_id == item_id,
                        Reminder.type == SNOOZE_RESURFACE,
                        Reminder.scheduled_at == scheduled_at,
                        Reminder.status == "SENT",
                    )
                    .execution_options(populate_existing=True)
                )
                if reminder is not None:
                    record_reminder_event(session, reminder, "REMINDER_SENT", created_at=sent_at)
            if result.rowcount != 1:
                log.warning("snooze finalization ignored after claim recovery item_id=%s", item_id)
            await session.commit()

    async def _mark_failed(
        self,
        user_id: int,
        item_id: int | None,
        type_: str,
        scheduled_at: datetime,
        claim_generation: int,
    ) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.item_id == item_id,
                    Reminder.type == type_,
                    Reminder.scheduled_at == scheduled_at,
                    Reminder.status == "CLAIMED",
                    Reminder.claim_generation == claim_generation,
                )
                .values(status="FAILED", claimed_at=None)
            )
            if result.rowcount != 1:
                log.info("notification failure ignored after claim recovery type=%s", type_)
            await session.commit()

    async def _send_with_retry(
        self, chat_id: int, text: str, *, claimed_at: datetime, **send_kwargs
    ) -> None:
        """Stop retries at a deadline anchored to the durable claim, before lease expiry."""
        deadline = _utc_naive(claimed_at) + NOTIFICATION_SEND_TIMEOUT
        remaining_seconds = (deadline - _utc_now()).total_seconds()
        if remaining_seconds <= 0:
            raise TimeoutError("notification send deadline expired before Telegram I/O")
        async with asyncio.timeout(remaining_seconds):
            for attempt in range(self.max_send_attempts):
                try:
                    await self.bot.send_message(chat_id, text, **send_kwargs)
                    return
                except Exception:
                    if attempt + 1 == self.max_send_attempts:
                        raise
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
