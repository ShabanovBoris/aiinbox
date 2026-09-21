"""Durable outbox for immediate Telegram notifications.

The business transaction only records an intent. A separate worker owns the
network side effect, so restart recovery never requires rolling READY/FAILED
Items or completed profile jobs back to an earlier business state.
"""

import asyncio
import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.bot.notify import send_item_failure, send_item_result
from app.storage.models import Delivery, Item, User

log = logging.getLogger(__name__)

ITEM_READY = "ITEM_READY"
ITEM_FAILED = "ITEM_FAILED"
PROFILE_UPDATED = "PROFILE_UPDATED"


async def enqueue_item_delivery(
    session,
    item: Item,
    delivery_type: str,
    *,
    payload: dict | None = None,
    reopen: bool = False,
) -> Delivery:
    """Persist an Item delivery intent inside the caller's business transaction.

    FAILED may legitimately happen again after user Retry, so that delivery key
    can be reopened. READY is not reopened: replaying a completed pipeline must
    not create a duplicate success notification.
    """
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.item_id == item.id,
            Delivery.type == delivery_type,
        )
    )
    if existing is not None:
        if reopen and existing.status in {"SENT", "FAILED"}:
            existing.status = "PENDING"
            existing.attempts = 0
            existing.last_error = None
            existing.sent_at = None
        if payload is not None:
            existing.payload_json = payload
        return existing

    delivery = Delivery(
        user_id=item.user_id,
        item_id=item.id,
        type=delivery_type,
        status="PENDING",
        payload_json=payload,
    )
    session.add(delivery)
    return delivery


async def enqueue_profile_delivery(
    session,
    *,
    user_id: int,
    profile_update_job_id: int,
    changed: list[str],
) -> Delivery:
    """Record profile completion together with its stable notification payload."""
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.profile_update_job_id == profile_update_job_id,
            Delivery.type == PROFILE_UPDATED,
        )
    )
    if existing is not None:
        return existing
    delivery = Delivery(
        user_id=user_id,
        profile_update_job_id=profile_update_job_id,
        type=PROFILE_UPDATED,
        status="PENDING",
        payload_json={"changed": changed},
    )
    session.add(delivery)
    return delivery


async def requeue_sending_deliveries(session_factory) -> int:
    """Recover the side-effect boundary after process death.

    SENDING means the previous process claimed the row but never durably marked
    completion. Requeueing prefers possible duplicate delivery over silent loss.
    """
    async with session_factory() as session:
        result = await session.execute(
            update(Delivery).where(Delivery.status == "SENDING").values(status="PENDING")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued interrupted deliveries count=%s", result.rowcount)
        return result.rowcount


class DeliveryWorker:
    """Owns Telegram side effects for the durable immediate-delivery outbox.

    Claim and attempt count are durable. A Telegram error returns the row to
    PENDING until the bounded attempt budget is exhausted; database failures
    escape to the process supervisor because they compromise queue correctness.
    """

    def __init__(
        self,
        session_factory,
        bot,
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.bot = bot
        self.poll_seconds = poll_seconds
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    async def claim_next(self) -> int | None:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Delivery)
                .where(
                    Delivery.id
                    == select(Delivery.id)
                    .where(Delivery.status == "PENDING")
                    .order_by(Delivery.created_at, Delivery.id)
                    .limit(1)
                    .scalar_subquery(),
                    Delivery.status == "PENDING",
                )
                .values(
                    status="SENDING",
                    attempts=Delivery.attempts + 1,
                    updated_at=func.now(),
                )
                .returning(Delivery.id)
            )
            delivery_id = result.scalar_one_or_none()
            await session.commit()
            return delivery_id

    async def process_one(self) -> bool:
        delivery_id = await self.claim_next()
        if delivery_id is None:
            return False
        try:
            await self._send(delivery_id)
            await self._mark_sent(delivery_id)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            log.exception("delivery worker database failure delivery_id=%s", delivery_id)
            raise
        except Exception as exc:
            retry = await self._record_failure(delivery_id, exc)
            log.warning(
                "telegram delivery failed delivery_id=%s retry=%s error=%s",
                delivery_id,
                retry,
                exc,
            )
            if retry and self.retry_backoff_seconds:
                await asyncio.sleep(self.retry_backoff_seconds)
        return True

    async def _send(self, delivery_id: int) -> None:
        async with self.session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                raise RuntimeError(f"delivery {delivery_id} disappeared")
            user = await session.get(User, delivery.user_id)
            if user is None or user.telegram_chat_id is None:
                raise RuntimeError(f"delivery {delivery_id} has no Telegram chat")
            chat_id = user.telegram_chat_id
            delivery_type = delivery.type
            payload = dict(delivery.payload_json or {})
            item = await session.get(Item, delivery.item_id) if delivery.item_id else None

        if delivery_type == ITEM_READY and item is not None:
            await send_item_result(self.bot, self.session_factory, item)
            return
        if delivery_type == ITEM_FAILED and item is not None:
            await send_item_failure(self.bot, self.session_factory, item)
            return
        if delivery_type == PROFILE_UPDATED:
            changed = payload.get("changed") or []
            await self.bot.send_message(chat_id, "Профиль обновлён: " + ", ".join(changed))
            return
        raise RuntimeError(f"unsupported delivery type={delivery_type}")

    async def _mark_sent(self, delivery_id: int) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(Delivery)
                .where(Delivery.id == delivery_id, Delivery.status == "SENDING")
                .values(status="SENT", sent_at=func.now(), last_error=None)
            )
            await session.commit()

    async def _record_failure(self, delivery_id: int, exc: Exception) -> bool:
        async with self.session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                return False
            retry = delivery.attempts < self.max_attempts
            delivery.status = "PENDING" if retry else "FAILED"
            delivery.last_error = str(exc)[:500]
            await session.commit()
            return retry
