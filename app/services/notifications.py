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
from app.bot.keyboards import item_keyboard
from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, ProcessingStatus
from app.services.attention_ranking import AttentionRank, AttentionRankingService
from app.services.retrieval import TodayService
from app.storage.models import Event, Item, ItemSource, Reminder, User

log = logging.getLogger(__name__)

DAILY_DIGEST = "DAILY_DIGEST"
SNOOZE_RESURFACE = "SNOOZE_RESURFACE"
PROACTIVE_ATTENTION = "PROACTIVE_ATTENTION"
MIN_PROACTIVE_ATTENTION_SCORE = 60
ATTENTION_CANDIDATE_LIMIT = 10
PROACTIVE_CLAIM_LEASE = timedelta(minutes=5)
_BUDGET_REMINDER_TYPES = (DAILY_DIGEST, PROACTIVE_ATTENTION)
_GAP_REMINDER_TYPES = (DAILY_DIGEST, PROACTIVE_ATTENTION, SNOOZE_RESURFACE)
_OPEN_PROACTIVE_STATUSES = ("PENDING", "CLAIMED")
_DEFAULT_SETTINGS = {
    "daily_digest_enabled": True,
    "daily_digest_time": "09:00",
    "quiet_hours_start": "22:30",
    "quiet_hours_end": "08:00",
    "attention_enabled": True,
    "attention_intensity": 3,
}


@dataclass(frozen=True)
class AttentionIntensityPolicy:
    """One immutable PM-08 frequency policy; it never changes Item ranking."""

    level: int
    label: str
    daily_cap: int
    minimum_gap: timedelta
    same_item_cooldown: timedelta


ATTENTION_POLICIES: Mapping[int, AttentionIntensityPolicy] = MappingProxyType(
    {
        1: AttentionIntensityPolicy(1, "Calm", 1, timedelta(hours=8), timedelta(hours=72)),
        2: AttentionIntensityPolicy(2, "Light", 2, timedelta(hours=5), timedelta(hours=48)),
        3: AttentionIntensityPolicy(3, "Normal", 3, timedelta(hours=3), timedelta(hours=30)),
        4: AttentionIntensityPolicy(4, "Active", 4, timedelta(hours=2), timedelta(hours=20)),
        5: AttentionIntensityPolicy(5, "Aggressive", 6, timedelta(minutes=90), timedelta(hours=12)),
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
    """Translate the current IANA local calendar day to a half-open UTC interval."""
    local_date = _utc_naive(now).replace(tzinfo=UTC).astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return local_date, _utc_naive(start), _utc_naive(end)


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
) -> tuple[User, dict] | None:
    """Validate and persist only requested settings in one user-scoped update."""
    if timezone is not None:
        parse_timezone(timezone)
    for clock in (daily_digest_time, quiet_hours_start, quiet_hours_end):
        if clock is not None:
            parse_clock(clock)
    if attention_enabled is not None and type(attention_enabled) is not bool:
        raise ValueError("attention_enabled должен быть true или false")
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


def format_settings(user: User, settings: dict) -> str:
    enabled = "включён" if settings["daily_digest_enabled"] else "выключен"
    level = settings.get("attention_intensity", 3)
    if type(level) is not int or level not in ATTENTION_POLICIES:
        level = 3
    policy = attention_policy(level)
    attention_enabled = settings.get("attention_enabled") is True
    attention_status = "ON" if attention_enabled else "OFF"
    return (
        "⚙️ Настройки уведомлений\n"
        f"Часовой пояс: {user.timezone}\n"
        f"Ежедневный digest: {enabled} ({settings['daily_digest_time']})\n"
        f"Тихие часы: {settings['quiet_hours_start']}–{settings['quiet_hours_end']}\n\n"
        f"Attention Manager: {attention_status}, {policy.label} ({policy.level})\n\n"
        "Изменить: /settings timezone Europe/Moscow\n"
        "/settings time 09:00\n"
        "/settings quiet 22:30-08:00\n"
        "/settings attention"
    )


def format_attention_settings(settings: dict) -> str:
    """Explain interruption frequency from the same policy the worker enforces."""
    level = settings.get("attention_intensity", 3)
    if type(level) is not int or level not in ATTENTION_POLICIES:
        level = 3
    policy = attention_policy(level)
    status = "ON" if settings.get("attention_enabled") is True else "OFF"
    gap_seconds = int(policy.minimum_gap.total_seconds())
    gap = f"{gap_seconds // 3600}h" if gap_seconds % 3600 == 0 else f"{gap_seconds // 60}m"
    cooldown_hours = int(policy.same_item_cooldown.total_seconds() // 3600)
    return (
        "🧠 Attention Manager\n\n"
        f"Статус: {status}\n"
        f"Интенсивность: {policy.level} — {policy.label}\n"
        f"Лимит: до {policy.daily_cap} уведомлений в день, включая digest\n"
        f"Минимальный интервал: {gap}\n"
        f"Повтор того же Item: через {cooldown_hours}h"
    )


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
        poll_seconds: float = 60.0,
        max_send_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
    ):
        self.session_factory = session_factory
        self.bot = bot
        self.default_timezone = default_timezone
        self.poll_seconds = poll_seconds
        self.max_send_attempts = max(1, max_send_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.process_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def process_once(self, now: datetime | None = None) -> int:
        now = _utc_naive(now or _utc_now())
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
        # minimum gap before the scheduler decides whether to interrupt again.
        for user_id in user_ids:
            try:
                sent += await self._process_proactive_attention(user_id, now)
            except SQLAlchemyError:
                raise
            except Exception:
                log.exception("proactive attention failed user_id=%s", user_id)
        return sent

    async def _process_user(self, user_id: int, now: datetime) -> int:
        async with self.session_factory() as session:
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
            claimed = await self._claim_digest(session, user.id, scheduled_at)
            if not claimed:
                return 0
            items = await TodayService().list_items(session, user.id)
            await session.commit()
        try:
            await self._send_with_retry(user.telegram_chat_id, format_today(items))
        except Exception:
            log.exception("daily digest delivery failed user_id=%s", user_id)
            await self._mark_failed(user_id, None, DAILY_DIGEST, scheduled_at)
            return 0
        await self._mark_digest_sent(user_id, scheduled_at, now)
        return 1

    async def _claim_digest(self, session, user_id: int, scheduled_at: datetime) -> bool:
        statement = (
            sqlite_insert(Reminder)
            .values(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=scheduled_at,
                status="CLAIMED",
                payload_json={"local_date": scheduled_at.date().isoformat()},
            )
            # item_id is NULL for a digest, so the table's general UNIQUE
            # constraint cannot identify it; the partial daily index does.
            .on_conflict_do_nothing()
        )
        result = await session.execute(statement)
        if result.rowcount == 1:
            return True
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
                payload_json={"local_date": scheduled_at.date().isoformat()},
            )
        )
        return result.rowcount == 1

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
        self, user_id: int, scheduled_at: datetime, sent_at: datetime
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
                )
                .values(status="SENT", sent_at=sent_at)
            )
            if result.rowcount != 1:
                raise RuntimeError("digest claim changed before successful-send finalization")
            await session.commit()

    async def _attention_gate(
        self, session, user: User, now: datetime
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

    @staticmethod
    def _claim_is_stale(reminder: Reminder, now: datetime) -> bool:
        created_at = reminder.created_at
        return created_at is None or now - _utc_naive(created_at) >= PROACTIVE_CLAIM_LEASE

    @staticmethod
    def _item_is_actionable(item: Item | None) -> bool:
        return bool(
            item is not None
            and item.processing_status is ProcessingStatus.READY
            and item.state is ItemState.ACTIVE
            and item.item_type in ACTIONABLE_ITEM_TYPES
        )

    async def _process_proactive_attention(self, user_id: int, now: datetime) -> int:
        """Rank only when policy allows, then create or recover one durable claim."""
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
            _, _, _, blocked = await self._attention_gate(session, user, now)
            if open_claim is not None and not self._claim_is_stale(open_claim, now):
                item = await session.get(Item, open_claim.item_id)
                if blocked == "disabled" or not self._item_is_actionable(item):
                    open_claim.status = "CANCELLED"
                    await session.commit()
                return 0
            if blocked is not None:
                # Quiet-hour claims remain durable until the next allowed window;
                # changed settings or exhausted pacing make a claim obsolete.
                if open_claim is not None and blocked != "quiet_hours":
                    open_claim.status = "CANCELLED"
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
        reminder_id, item_id, rank, policy_level = claimed
        prepared = await self._prepare_proactive_send(reminder_id, user_id, item_id, now)
        if prepared is None:
            return 0
        item, sources, chat_id = prepared
        try:
            await self._send_with_retry(
                chat_id,
                format_proactive_attention_reminder(item, rank),
                reply_markup=item_keyboard(item, sources),
            )
        except Exception:
            log.exception(
                "proactive delivery failed user_id=%s item_id=%s reminder_id=%s",
                user_id,
                item_id,
                reminder_id,
            )
            await self._set_proactive_status(reminder_id, "FAILED")
            return 0
        await self._finalize_proactive_send(reminder_id, user_id, item_id, now)
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
    ) -> tuple[int, int, AttentionRank, int] | None:
        """Serialize current settings, pacing facts, cooldowns, and one durable claim."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            user = await session.get(User, user_id)
            if user is None or user.telegram_chat_id is None:
                await session.commit()
                return None
            policy, _, local_date, blocked = await self._attention_gate(session, user, now)
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
            if blocked is not None:
                if open_claim is not None and blocked != "quiet_hours":
                    open_claim.status = "CANCELLED"
                await session.commit()
                return None
            if open_claim is not None and not self._claim_is_stale(open_claim, now):
                await session.commit()
                return None

            qualified = [
                (item_id, rank)
                for item_id, rank in ranked
                if rank.score >= MIN_PROACTIVE_ATTENTION_SCORE
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

            chosen: tuple[int, AttentionRank] | None = None
            if open_claim is not None and open_claim.item_id is not None:
                old_rank = next(
                    (rank for item_id, rank in qualified if item_id == open_claim.item_id),
                    None,
                )
                old_last_sent = cooldowns.get(open_claim.item_id)
                if (
                    old_rank is not None
                    and open_claim.item_id in eligible_ids
                    and (old_last_sent is None or now - old_last_sent >= policy.same_item_cooldown)
                ):
                    chosen = (open_claim.item_id, old_rank)
                else:
                    open_claim.status = "CANCELLED"

            if chosen is None:
                chosen = next(
                    (
                        (item_id, rank)
                        for item_id, rank in qualified
                        if item_id in eligible_ids
                        and (
                            item_id not in cooldowns
                            or now - cooldowns[item_id] >= policy.same_item_cooldown
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
                await session.commit()
                return None
            payload = {
                "attention_score": rank.score,
                "priority_score": rank.priority_score,
                "interest_level": item.interest_level,
                "policy_level": policy.level,
                "reason": format_attention_reason(rank)[:240],
                "budget_local_date": local_date.isoformat(),
            }
            recovered_id = (
                open_claim.id if open_claim is not None and open_claim.item_id == item_id else None
            )
            scheduled_at = await self._next_proactive_schedule(
                session, user_id, item_id, now, recovered_id
            )
            if open_claim is not None and open_claim.item_id == item_id:
                open_claim.status = "CLAIMED"
                open_claim.scheduled_at = scheduled_at
                open_claim.created_at = now
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
                )
                session.add(reminder)
            await session.flush()
            result = (reminder.id, item_id, rank, policy.level)
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
        self, reminder_id: int, user_id: int, item_id: int, now: datetime
    ) -> tuple[Item, list[ItemSource], int] | None:
        """Recheck the claim immediately before Telegram I/O, then close SQLite first."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            reminder = await session.get(Reminder, reminder_id)
            user = await session.get(User, user_id)
            item = await session.get(Item, item_id)
            if (
                reminder is None
                or reminder.status not in _OPEN_PROACTIVE_STATUSES
                or reminder.user_id != user_id
                or reminder.type != PROACTIVE_ATTENTION
                or reminder.item_id != item_id
                or user is None
                or user.telegram_chat_id is None
            ):
                await session.commit()
                return None
            policy, _, _, blocked = await self._attention_gate(session, user, now)
            if blocked is not None:
                if blocked != "quiet_hours":
                    reminder.status = "CANCELLED"
                await session.commit()
                return None
            if not self._item_is_actionable(item):
                reminder.status = "CANCELLED"
                await session.commit()
                return None
            cooldowns = await self._proactive_cooldowns(session, user_id, [item_id])
            last_sent = cooldowns.get(item_id)
            if last_sent is not None and now - last_sent < policy.same_item_cooldown:
                reminder.status = "CANCELLED"
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
            await session.commit()
            return item, sources, chat_id

    async def _set_proactive_status(self, reminder_id: int, status: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status.in_(_OPEN_PROACTIVE_STATUSES),
                )
                .values(status=status)
            )
            await session.commit()

    async def _finalize_proactive_send(
        self, reminder_id: int, user_id: int, item_id: int, sent_at: datetime
    ) -> None:
        """Commit delivery history and PM-07 exposure as one idempotent fact."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.user_id == user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status.in_(_OPEN_PROACTIVE_STATUSES),
                )
                .values(status="SENT", sent_at=sent_at)
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
            await session.commit()

    async def _process_snoozes(self, now: datetime) -> int:
        async with self.session_factory() as session:
            reminders = (
                await session.scalars(
                    select(Reminder).where(
                        Reminder.type == SNOOZE_RESURFACE,
                        Reminder.status == "PENDING",
                        Reminder.scheduled_at <= now,
                    )
                )
            ).all()
            processed: list[tuple[Reminder, Item, int]] = []
            for reminder in reminders:
                item = await session.get(Item, reminder.item_id)
                user = await session.get(User, reminder.user_id)
                if item is None or user is None or user.telegram_chat_id is None:
                    reminder.status = "CANCELLED"
                    continue
                try:
                    zone = parse_timezone(user.timezone or self.default_timezone)
                    local_now = now.replace(tzinfo=UTC).astimezone(zone)
                    settings = settings_for(user)
                    quiet = in_quiet_hours(
                        local_now.time(),
                        parse_clock(settings["quiet_hours_start"]),
                        parse_clock(settings["quiet_hours_end"]),
                    )
                except ValueError:
                    quiet = False
                if quiet:
                    continue
                if (
                    item.state is not ItemState.SNOOZED
                    or item.snoozed_until is None
                    or item.snoozed_until > now
                ):
                    reminder.status = "CANCELLED"
                    continue
                reminder.status = "CLAIMED"
                reminder.sent_at = None
                item.state = ItemState.ACTIVE
                item.snoozed_until = None
                await session.flush()
                processed.append((reminder, item, user.telegram_chat_id))
            await session.commit()
        delivered = 0
        for reminder, item, chat_id in processed:
            try:
                await self._send_with_retry(
                    chat_id, f"⏰ Вернулся отложенный Item: {item.title or 'Без названия'}"
                )
            except Exception:
                log.exception("snooze notification failed item_id=%s", item.id)
                await self._mark_failed(
                    item.user_id, item.id, SNOOZE_RESURFACE, reminder.scheduled_at
                )
            else:
                await self._mark_snooze_sent(item.user_id, item.id, reminder.scheduled_at, now)
                delivered += 1
        return delivered

    async def _mark_snooze_sent(
        self, user_id: int, item_id: int, scheduled_at: datetime, sent_at: datetime
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
                )
                .values(status="SENT", sent_at=sent_at)
            )
            if result.rowcount != 1:
                raise RuntimeError("snooze claim changed before successful-send finalization")
            await session.commit()

    async def _mark_failed(
        self, user_id: int, item_id: int | None, type_: str, scheduled_at: datetime
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.item_id == item_id,
                    Reminder.type == type_,
                    Reminder.scheduled_at == scheduled_at,
                )
                .values(status="FAILED")
            )
            await session.commit()

    async def _send_with_retry(self, chat_id: int, text: str, **send_kwargs) -> None:
        """Retry an unknown Telegram failure finitely before terminal FAILED."""
        for attempt in range(self.max_send_attempts):
            try:
                await self.bot.send_message(chat_id, text, **send_kwargs)
                return
            except Exception:
                if attempt + 1 == self.max_send_attempts:
                    raise
                await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
