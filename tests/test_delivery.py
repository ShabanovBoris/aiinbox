import pytest
from aiogram.types import ReplyParameters
from sqlalchemy import select

from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.services.delivery import (
    ITEM_FAILED,
    ITEM_READY,
    PROFILE_UPDATED,
    DeliveryWorker,
    enqueue_item_delivery,
    enqueue_profile_delivery,
    requeue_sending_deliveries,
)
from app.services.ingestion import ingest_message
from app.storage.models import Delivery, Item, ItemSource, ProfileUpdateJob


class FakeBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.messages: list[tuple[int, str]] = []
        self.message_kwargs: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))
        self.message_kwargs.append(kwargs)


async def make_item(session_factory, *, status=ProcessingStatus.READY) -> Item:
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=7777,
            message_id=1,
            text="item",
        )
    ).items[0]
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        stored.processing_status = status
        stored.processing_stage = status.value
        stored.title = "Разобрать материал"
        stored.category = "Обучение"
        stored.item_type = ItemType.LEARN
        stored.priority_score = 80
        if status is ProcessingStatus.FAILED:
            stored.error_code = "LLM_FAILED"
        await session.commit()
    return item


async def test_delivery_worker_sends_ready_item_and_marks_sent(session_factory):
    item = await make_item(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_READY)
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True
    expected = (
        "✓ Сохранено\n\n🎯 Разобрать материал\nКатегория: Обучение\nТип: LEARN\n"
        "Приоритет: 80/100\nИнтерес: 2/3"
    )
    assert bot.messages == [(7777, expected)]

    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "SENT"
        assert delivery.attempts == 1
        assert delivery.sent_at is not None


@pytest.mark.parametrize(
    ("source_type", "source_metadata", "expected_original_url"),
    [
        (SourceType.VIDEO, None, None),
        (
            SourceType.TEXT,
            {
                "forwarded": True,
                "forward_origin_type": "channel",
                "forward_source_username": "androiddev",
                "forward_message_id": 8712,
            },
            "https://t.me/androiddev/8712",
        ),
    ],
)
@pytest.mark.parametrize(
    ("delivery_type", "status"),
    [
        (ITEM_READY, ProcessingStatus.READY),
        (ITEM_FAILED, ProcessingStatus.FAILED),
    ],
)
async def test_video_result_replies_to_input_and_keeps_forward_origin_link(
    session_factory,
    source_type,
    source_metadata,
    expected_original_url,
    delivery_type,
    status,
):
    """Keep a clickable reply anchor to the submitted video in the result chat."""
    item = await make_item(session_factory, status=status)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        stored.source_type = source_type
        stored.source_metadata_json = source_metadata
        session.add(
            ItemSource(
                item_id=item.id,
                source_index=0,
                source_type=SourceType.VIDEO,
                source_file_id="video-file",
            )
        )
        await enqueue_item_delivery(session, stored, delivery_type)
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True

    assert bot.message_kwargs[0]["reply_parameters"] == ReplyParameters(
        message_id=1,
        allow_sending_without_reply=True,
    )
    buttons = [
        button for row in bot.message_kwargs[0]["reply_markup"].inline_keyboard for button in row
    ]
    original_links = [button.url for button in buttons if button.text == "↗ Открыть оригинал"]
    assert original_links == ([expected_original_url] if expected_original_url else [])


async def test_delivery_worker_retries_telegram_failure_without_losing_intent(session_factory):
    item = await make_item(session_factory, status=ProcessingStatus.FAILED)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_FAILED, reopen=True)
        await session.commit()

    bot = FakeBot(failures=1)
    worker = DeliveryWorker(
        session_factory,
        bot,
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    assert await worker.process_one() is True
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "PENDING"
        assert delivery.attempts == 1
        assert "telegram unavailable" in delivery.last_error

    assert await worker.process_one() is True
    assert len(bot.messages) == 1
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "SENT"
        assert delivery.attempts == 2


async def test_retry_cancels_pending_stale_failure_delivery(session_factory):
    item = await make_item(session_factory, status=ProcessingStatus.FAILED)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_FAILED, reopen=True)
        await session.commit()

    retried = await apply_item_action(session_factory, 42, item.id, "retry")
    assert retried is not None
    assert retried.processing_status is ProcessingStatus.QUEUED

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is False
    assert bot.messages == []
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery).where(Delivery.type == ITEM_FAILED))
        assert delivery.status == "CANCELLED"


async def test_requeue_interrupted_sending_delivery_after_restart(session_factory):
    item = await make_item(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        delivery = await enqueue_item_delivery(session, stored, ITEM_READY)
        delivery.status = "SENDING"
        delivery.attempts = 1
        await session.commit()

    assert await requeue_sending_deliveries(session_factory) == 1
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "PENDING"
        assert delivery.attempts == 1


async def test_profile_delivery_uses_telegram_chat_and_stable_payload(session_factory):
    await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=7777,
        message_id=1,
        text="x",
    )
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        job = ProfileUpdateJob(user_id=item.user_id, instruction="i", status="DONE")
        session.add(job)
        await session.flush()
        await enqueue_profile_delivery(
            session,
            user_id=item.user_id,
            profile_update_job_id=job.id,
            changed=["profession"],
        )
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True
    assert bot.messages == [(7777, "Профиль обновлён: profession")]

    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery).where(Delivery.type == PROFILE_UPDATED))
        assert delivery.status == "SENT"
