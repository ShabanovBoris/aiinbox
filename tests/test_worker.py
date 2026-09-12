import asyncio

from app.domain.enums import ProcessingStatus
from app.services.ingestion import ingest_message
from app.storage.models import Item
from app.workers.processing import ProcessingWorker, requeue_stale
from tests.fakes import FakePipeline


async def seed(session_factory, message_id: int = 1, text: str = "note"):
    return (
        await ingest_message(
            session_factory, telegram_user_id=42, chat_id=42, message_id=message_id, text=text
        )
    ).items[0]


async def get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


def make_worker(session_factory, pipeline=None):
    return ProcessingWorker(
        session_factory, pipeline if pipeline is not None else FakePipeline(), poll_seconds=0.01
    )


async def test_claim_transitions_queued_to_processing(session_factory):
    item = await seed(session_factory)
    worker = make_worker(session_factory)
    claimed = await worker.claim_next()
    assert claimed == item.id
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.PROCESSING
    # claim не затирает processing_stage — checkpoint принадлежит пайплайну
    assert stored.processing_stage == "INGESTED"


async def test_claim_is_atomic_no_double_processing(session_factory):
    await seed(session_factory)
    worker = make_worker(session_factory)
    first = await worker.claim_next()
    second = await worker.claim_next()
    assert first is not None
    assert second is None


async def test_concurrent_workers_claim_exactly_one_for_single_item(session_factory):
    # D-004 проверяется конкурентно: два воркера, один QUEUED — ровно один claim.
    item = await seed(session_factory)
    workers = [make_worker(session_factory), make_worker(session_factory)]
    claimed = await asyncio.gather(*(w.claim_next() for w in workers))
    assert sorted(c for c in claimed if c is not None) == [item.id]
    assert claimed.count(None) == 1


async def test_concurrent_workers_claim_distinct_items(session_factory):
    first = await seed(session_factory, message_id=1)
    second = await seed(session_factory, message_id=2)
    workers = [make_worker(session_factory), make_worker(session_factory)]
    claimed = await asyncio.gather(*(w.claim_next() for w in workers))
    assert sorted(c for c in claimed if c is not None) == [first.id, second.id]


async def test_worker_processes_queued_to_ready(session_factory):
    item = await seed(session_factory)
    worker = make_worker(session_factory)
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.processing_stage == "READY"
    assert stored.error_code is None


async def test_worker_marks_failed_on_exception(session_factory):
    item = await seed(session_factory)
    worker = make_worker(session_factory, pipeline=FakePipeline(fail=True))
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.FAILED
    assert stored.processing_stage == "FAILED"
    assert stored.error_code == "UNKNOWN"
    assert "boom" in stored.error_message


async def test_worker_claims_oldest_first(session_factory):
    older = await seed(session_factory, message_id=1)
    newer = await seed(session_factory, message_id=2)
    worker = make_worker(session_factory)
    assert await worker.claim_next() == older.id
    assert await worker.claim_next() == newer.id


async def test_processed_item_not_claimed_again(session_factory):
    item = await seed(session_factory)
    worker = make_worker(session_factory)
    await worker.process_one()
    assert await worker.claim_next() is None
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY


async def test_requeue_stale_returns_processing_to_queued(session_factory):
    item = await seed(session_factory)
    worker = make_worker(session_factory)
    await worker.claim_next()
    assert await requeue_stale(session_factory) == 1
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.QUEUED
    assert stored.processing_stage == "INGESTED"  # содержательная стадия сохранена
    assert await requeue_stale(session_factory) == 0
