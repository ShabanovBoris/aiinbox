"""Reminder-attributed feedback and derived, bounded scheduling adjustments."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from math import ceil, floor

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.sql import text

from app.domain.enums import ItemState, ItemType, MotivationKind
from app.services.feedback import claim_feedback_callback_receipt
from app.storage.models import Event, Item, ItemSource, Reminder, User

PROACTIVE_ATTENTION = "PROACTIVE_ATTENTION"
MOTIVATION_NUDGE = "MOTIVATION_NUDGE"
REMINDER_DISMISSAL_COOLDOWN = timedelta(hours=24)
MOTIVATION_DISLIKE_WINDOW = timedelta(days=7)
_PREFERENCE_WINDOW = timedelta(days=90)
_FATIGUE_WINDOW = timedelta(days=7)
_REMINDER_EVENT_TYPES = (
    "REMINDER_SENT",
    "REMINDER_OPENED",
    "REMINDER_SNOOZED",
    "REMINDER_DONE",
    "REMINDER_DISMISSED",
    "REMINDER_DISLIKED",
)
_OUTCOME_WEIGHTS = {
    "REMINDER_DONE": 1.0,
    "REMINDER_OPENED": 0.5,
    "REMINDER_SNOOZED": -0.25,
    "REMINDER_DISMISSED": -1.0,
    "REMINDER_DISLIKED": -2.0,
}


def _utc_naive(value: datetime) -> datetime:
    """Use the repository's naive-UTC convention for comparisons with SQLite."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _round_half_away_from_zero(value: float) -> int:
    """Keep sparse fatigue rounding symmetric and independent of Python's tie rule."""
    return floor(value + 0.5) if value >= 0 else ceil(value - 0.5)


def reminder_event_snapshot(reminder: Reminder, *, source_id: int | None = None) -> dict:
    """Project only bounded delivery attribution into analytical Event payloads.

    Reminder payload is the send-time snapshot, so this projection intentionally
    avoids reading mutable Item classification or copying source/hook text.
    """
    payload = reminder.payload_json if isinstance(reminder.payload_json, dict) else {}
    snapshot = {"reminder_type": reminder.type[:32]}
    for key, low, high in (
        ("attention_score", 0, 100),
        ("priority_score", 0, 100),
        ("interest_level", 1, 3),
        ("policy_level", 1, 5),
        ("hook_content_id", 1, 2**63 - 1),
        ("slot", 1, 2),
    ):
        value = payload.get(key)
        if type(value) is int and low <= value <= high:
            snapshot[key] = value

    category = payload.get("category")
    if isinstance(category, str) and category.strip():
        snapshot["category"] = category.strip()[:100]

    try:
        item_type = ItemType(payload.get("item_type"))
    except (TypeError, ValueError):
        pass
    else:
        snapshot["item_type"] = item_type.value

    raw_kind = payload.get("motivation_kind", payload.get("kind"))
    try:
        motivation_kind = MotivationKind(raw_kind)
    except (TypeError, ValueError):
        pass
    else:
        snapshot["motivation_kind"] = motivation_kind.value

    template_id = payload.get("template_id")
    if isinstance(template_id, str) and template_id:
        snapshot["template_id"] = template_id[:96]
    local_date = payload.get("local_date")
    if isinstance(local_date, str) and len(local_date) == 10:
        snapshot["local_date"] = local_date
    if type(source_id) is int and source_id > 0:
        snapshot["source_id"] = source_id
    return snapshot


def record_reminder_event(
    session,
    reminder: Reminder,
    event_type: str,
    *,
    created_at: datetime | None = None,
    callback_id: str | None = None,
    source_id: int | None = None,
) -> Event:
    """Append one reminder-linked fact inside its caller's transaction.

    Send finalizers and reminder callbacks own the surrounding state change;
    keeping Event creation here gives both paths one attribution contract.
    """
    if event_type not in _REMINDER_EVENT_TYPES:
        raise ValueError("unsupported reminder event")
    idempotency_key = None
    if callback_id is not None:
        idempotency_key = f"telegram-callback:{callback_id}"
        if len(idempotency_key) > 160:
            raise ValueError("callback identity exceeds Event idempotency key limit")
    elif event_type == "REMINDER_SENT":
        idempotency_key = f"reminder:{reminder.id}:sent"

    event_time = created_at
    if event_time is None:
        event_time = reminder.sent_at if event_type == "REMINDER_SENT" else datetime.now(UTC)
    event = Event(
        user_id=reminder.user_id,
        item_id=reminder.item_id,
        reminder_id=reminder.id,
        event_type=event_type,
        payload_json=reminder_event_snapshot(reminder, source_id=source_id),
        idempotency_key=idempotency_key,
        created_at=_utc_naive(event_time),
    )
    session.add(event)
    return event


class ReminderFeedbackService:
    """Own Reminder callbacks and derive bounded policy inputs from Event history.

    Canonical Item state and delivery claims remain with their existing services;
    this service adds only explicit Reminder attribution and read-only projections.
    """

    def __init__(self, session_factory: async_sessionmaker | None = None) -> None:
        self.session_factory = session_factory

    async def apply_callback(
        self,
        telegram_user_id: int,
        reminder_id: int,
        action: str,
        *,
        callback_id: str | None = None,
        snoozed_until: datetime | None = None,
        source_id: int | None = None,
    ) -> str:
        """Validate one SENT Reminder and atomically persist its observable reaction."""
        if self.session_factory is None:
            raise RuntimeError("Reminder callbacks require a session factory")
        if (
            type(reminder_id) is not int
            or reminder_id < 1
            or action
            not in {
                "later",
                "cancel",
                "ok",
                "done",
                "snooze",
                "dismiss",
                "dislike",
                "open",
                "open_original",
            }
        ):
            return "UNAVAILABLE"

        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            user_id = await session.scalar(
                select(User.id).where(User.telegram_user_id == telegram_user_id)
            )
            if user_id is None:
                await session.rollback()
                return "NOT_FOUND"
            reminder = await session.scalar(
                select(Reminder).where(
                    Reminder.id == reminder_id,
                    Reminder.user_id == user_id,
                )
            )
            if reminder is None:
                await session.rollback()
                return "NOT_FOUND"
            if reminder.status != "SENT":
                await session.rollback()
                return "UNAVAILABLE"

            item_action_types = {"PROACTIVE_ATTENTION"}
            if action in {
                "done",
                "snooze",
                "dismiss",
                "open",
                "open_original",
                "later",
                "cancel",
            }:
                if reminder.type not in item_action_types or reminder.item_id is None:
                    await session.rollback()
                    return "UNAVAILABLE"
            elif action == "ok" and reminder.type != MOTIVATION_NUDGE:
                await session.rollback()
                return "UNAVAILABLE"
            elif action == "dislike" and reminder.type not in {
                PROACTIVE_ATTENTION,
                MOTIVATION_NUDGE,
            }:
                await session.rollback()
                return "UNAVAILABLE"

            if callback_id is not None:
                callback_key = f"telegram-callback:{callback_id}"
                if not callback_id or len(callback_key) > 160:
                    await session.rollback()
                    return "UNAVAILABLE"
                if not await claim_feedback_callback_receipt(session, user_id, callback_key):
                    await session.commit()
                    return "DUPLICATE_CALLBACK"

            if action in {"later", "cancel", "ok"}:
                await session.commit()
                return "APPLIED"

            event_type = {
                "done": "REMINDER_DONE",
                "snooze": "REMINDER_SNOOZED",
                "dismiss": "REMINDER_DISMISSED",
                "dislike": "REMINDER_DISLIKED",
                "open": "REMINDER_OPENED",
                "open_original": "REMINDER_OPENED",
            }[action]
            if action == "snooze" and snoozed_until is None:
                await session.rollback()
                return "UNAVAILABLE"
            if action == "open" and (type(source_id) is not int or source_id < 1):
                await session.rollback()
                return "UNAVAILABLE"

            if await self._has_event(session, reminder.id, event_type):
                if action != "open":
                    await session.commit()
                    return "ALREADY_RECORDED"

            if action in {"done", "snooze"}:
                item = await session.get(Item, reminder.item_id)
                if item is None:
                    await session.rollback()
                    return "UNAVAILABLE"
                if action == "done" and item.state is ItemState.DONE:
                    await session.commit()
                    return "ALREADY_DONE"
                if item.state is ItemState.ARCHIVED or (
                    action == "snooze" and item.state is ItemState.DONE
                ):
                    await session.commit()
                    return "UNAVAILABLE"

                # Imported locally to keep the delivery/ranking service graph acyclic.
                from app.services.actions import _apply_item_action_in_session

                item, transitioned = await _apply_item_action_in_session(
                    session,
                    user_id,
                    reminder.item_id,
                    action,
                    snoozed_until=snoozed_until,
                )
                if not transitioned:
                    await session.commit()
                    return (
                        "ALREADY_DONE"
                        if action == "done" and item is not None and item.state is ItemState.DONE
                        else "UNAVAILABLE"
                    )

            if action == "open":
                # Delivery imports Telegram formatting, which itself consumes
                # AttentionRank; defer the adapter dependency to this callback edge.
                from app.services.delivery import enqueue_item_video_delivery_in_session

                delivery_status = await enqueue_item_video_delivery_in_session(
                    session, user_id, reminder.item_id, source_id
                )
                if delivery_status is None:
                    await session.rollback()
                    return "UNAVAILABLE"
                if not await self._has_event(session, reminder.id, event_type):
                    record_reminder_event(
                        session,
                        reminder,
                        event_type,
                        callback_id=callback_id,
                        source_id=source_id,
                    )
                await session.commit()
                return delivery_status

            if action == "open_original":
                record_reminder_event(
                    session,
                    reminder,
                    event_type,
                    callback_id=callback_id,
                )
                await session.commit()
                return "APPLIED"

            record_reminder_event(
                session,
                reminder,
                event_type,
                callback_id=callback_id,
            )
            await session.commit()
            return "APPLIED"

    async def item_reminder_projection(
        self, telegram_user_id: int, reminder_id: int
    ) -> tuple[Reminder, Item, list[ItemSource], bool] | None:
        """Load only an owned, SENT proactive Item reminder for its cancel projection."""
        if self.session_factory is None:
            raise RuntimeError("Reminder callbacks require a session factory")
        async with self.session_factory() as session:
            row = await session.execute(
                select(Reminder, User.telegram_chat_id)
                .join(User, User.id == Reminder.user_id)
                .where(
                    Reminder.id == reminder_id,
                    User.telegram_user_id == telegram_user_id,
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "SENT",
                    Reminder.item_id.is_not(None),
                )
            )
            projection = row.one_or_none()
            if projection is None:
                return None
            reminder, chat_id = projection
            item = await session.get(Item, reminder.item_id)
            if item is None:
                return None
            sources = list(
                (
                    await session.scalars(
                        select(ItemSource)
                        .where(ItemSource.item_id == item.id)
                        .order_by(ItemSource.source_index, ItemSource.id)
                    )
                ).all()
            )
            return reminder, item, sources, chat_id is not None

    @staticmethod
    async def _has_event(session, reminder_id: int, event_type: str) -> bool:
        """Read semantic per-Reminder idempotency while the caller holds writer serialization."""
        return (
            await session.scalar(
                select(Event.id).where(
                    Event.reminder_id == reminder_id,
                    Event.event_type == event_type,
                )
            )
            is not None
        )

    @staticmethod
    async def attention_adjustments(
        session,
        user_id: int,
        items: Sequence[Item],
        *,
        now: datetime,
    ) -> tuple[dict[int, int], int]:
        """Project latest matching Reminder dislikes and one user fatigue score in two reads."""
        instant = _utc_naive(now)
        categories = {item.category for item in items if item.category}
        item_types = {
            item.item_type.value if isinstance(item.item_type, ItemType) else item.item_type
            for item in items
            if item.item_type is not None
        }
        latest_category: dict[str, datetime] = {}
        latest_type: dict[str, datetime] = {}
        if categories or item_types:
            dislikes = (
                await session.execute(
                    select(Event.payload_json, Event.created_at)
                    .where(
                        Event.user_id == user_id,
                        Event.event_type == "REMINDER_DISLIKED",
                        Event.created_at >= instant - _PREFERENCE_WINDOW,
                        Event.created_at <= instant,
                    )
                    .order_by(Event.created_at.desc(), Event.id.desc())
                )
            ).all()
            for payload, created_at in dislikes:
                if (
                    not isinstance(payload, dict)
                    or payload.get("reminder_type") != PROACTIVE_ATTENTION
                ):
                    continue
                event_at = _utc_naive(created_at)
                category = payload.get("category")
                if (
                    isinstance(category, str)
                    and category in categories
                    and category not in latest_category
                ):
                    latest_category[category] = event_at
                item_type = payload.get("item_type")
                if (
                    isinstance(item_type, str)
                    and item_type in item_types
                    and item_type not in latest_type
                ):
                    latest_type[item_type] = event_at

        by_item = {}
        for item in items:
            category_points = _preference_points(
                latest_category.get(item.category), instant, -10, -5
            )
            item_type_value = (
                item.item_type.value if isinstance(item.item_type, ItemType) else item.item_type
            )
            type_points = _preference_points(latest_type.get(item_type_value), instant, -4, -2)
            by_item[item.id] = max(-12, min(0, category_points + type_points))

        fatigue_rows = (
            await session.execute(
                select(Event.event_type).where(
                    Event.user_id == user_id,
                    Event.event_type.in_(_OUTCOME_WEIGHTS),
                    Event.created_at >= instant - _FATIGUE_WINDOW,
                    Event.created_at <= instant,
                )
            )
        ).all()
        event_count = len(fatigue_rows)
        if event_count == 0:
            return by_item, 0
        signal_sum = sum(_OUTCOME_WEIGHTS[event_type] for (event_type,) in fatigue_rows)
        smoothed_receptivity = (signal_sum / event_count) * (event_count / (event_count + 5))
        penalty = (
            0 if smoothed_receptivity >= 0 else _round_half_away_from_zero(smoothed_receptivity * 5)
        )
        return by_item, max(-10, min(0, penalty))

    @staticmethod
    async def latest_dismissals(
        session,
        user_id: int,
        item_ids: Sequence[int],
        *,
        now: datetime,
    ) -> dict[int, datetime]:
        """Batch-load only recent item-specific dismissals for PM-08 send gating."""
        if not item_ids:
            return {}
        instant = _utc_naive(now)
        rows = (
            await session.execute(
                select(Event.item_id, Event.payload_json, Event.created_at)
                .where(
                    Event.user_id == user_id,
                    Event.item_id.in_(item_ids),
                    Event.event_type == "REMINDER_DISMISSED",
                    Event.created_at >= instant - REMINDER_DISMISSAL_COOLDOWN,
                    Event.created_at <= instant,
                )
                .order_by(Event.created_at.desc(), Event.id.desc())
            )
        ).all()
        latest = {}
        for item_id, payload, created_at in rows:
            if item_id in latest:
                continue
            if isinstance(payload, dict) and payload.get("reminder_type") == PROACTIVE_ATTENTION:
                latest[item_id] = _utc_naive(created_at)
        return latest

    @staticmethod
    async def suppressed_motivation_kinds(
        session, user_id: int, *, now: datetime
    ) -> set[MotivationKind]:
        """Return nudge kinds explicitly disliked less than seven elapsed days ago."""
        instant = _utc_naive(now)
        payloads = (
            await session.scalars(
                select(Event.payload_json).where(
                    Event.user_id == user_id,
                    Event.event_type == "REMINDER_DISLIKED",
                    Event.reminder_id.is_not(None),
                    Event.created_at > instant - MOTIVATION_DISLIKE_WINDOW,
                    Event.created_at <= instant,
                )
            )
        ).all()
        suppressed = set()
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("reminder_type") != MOTIVATION_NUDGE:
                continue
            try:
                suppressed.add(MotivationKind(payload.get("motivation_kind")))
            except (TypeError, ValueError):
                continue
        return suppressed


def _preference_points(
    event_at: datetime | None, now: datetime, full_value: int, half_value: int
) -> int:
    """Apply exact elapsed-time decay to one dimension's newest active dislike."""
    if event_at is None:
        return 0
    age = now - event_at
    if age <= timedelta(days=30):
        return full_value
    if age <= _PREFERENCE_WINDOW:
        return half_value
    return 0
