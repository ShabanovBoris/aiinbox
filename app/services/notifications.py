"""Persistent notification settings and the restart-safe reminder worker.

The service keeps scheduling and delivery claims in SQLite. Telegram is only
an output adapter, so a process restart cannot recreate an already claimed
digest or snooze reminder.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.bot.formatting import format_today
from app.domain.enums import ItemState
from app.services.retrieval import TodayService
from app.storage.models import Item, Reminder, User

log = logging.getLogger(__name__)

DAILY_DIGEST = "DAILY_DIGEST"
SNOOZE_RESURFACE = "SNOOZE_RESURFACE"
_DEFAULT_SETTINGS = {
    "daily_digest_enabled": True,
    "daily_digest_time": "09:00",
    "quiet_hours_start": "22:30",
    "quiet_hours_end": "08:00",
}


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
) -> tuple[User, dict] | None:
    """Validate and persist only requested settings in one user-scoped update."""
    if timezone is not None:
        parse_timezone(timezone)
    for clock in (daily_digest_time, quiet_hours_start, quiet_hours_end):
        if clock is not None:
            parse_clock(clock)
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
        }
        settings_patch = {key: value for key, value in settings_patch.items() if value is not None}
        values = {}
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
    return (
        "⚙️ Настройки уведомлений\n"
        f"Часовой пояс: {user.timezone}\n"
        f"Ежедневный digest: {enabled} ({settings['daily_digest_time']})\n"
        f"Тихие часы: {settings['quiet_hours_start']}–{settings['quiet_hours_end']}\n\n"
        "Изменить: /settings timezone Europe/Moscow\n"
        "/settings time 09:00\n"
        "/settings quiet 22:30-08:00"
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
        now = now or _utc_now()
        sent = 0
        async with self.session_factory() as session:
            users = (await session.scalars(select(User))).all()
        for user in users:
            sent += await self._process_user(user.id, now)
        sent += await self._process_snoozes(now)
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
                if local_now.time() >= digest_time:
                    await self._defer_digest(
                        session, user.id, datetime.combine(local_now.date(), time.min)
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
            claimed = await self._claim_digest(session, user.id, scheduled_at, now)
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
        return 1

    async def _claim_digest(
        self, session, user_id: int, scheduled_at: datetime, now: datetime
    ) -> bool:
        statement = (
            sqlite_insert(Reminder)
            .values(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=scheduled_at,
                status="SENT",
                payload_json={"local_date": scheduled_at.date().isoformat()},
                sent_at=now,
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
                status="SENT",
                payload_json={"local_date": scheduled_at.date().isoformat()},
                sent_at=now,
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
                reminder.status = "SENT"
                reminder.sent_at = now
                if (
                    item.state is not ItemState.SNOOZED
                    or item.snoozed_until is None
                    or item.snoozed_until > now
                ):
                    reminder.status = "CANCELLED"
                    continue
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
                delivered += 1
        return delivered

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

    async def _send_with_retry(self, chat_id: int, text: str) -> None:
        """Retry an unknown Telegram failure finitely before terminal FAILED."""
        for attempt in range(self.max_send_attempts):
            try:
                await self.bot.send_message(chat_id, text)
                return
            except Exception:
                if attempt + 1 == self.max_send_attempts:
                    raise
                await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
