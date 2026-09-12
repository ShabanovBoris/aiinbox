from app.domain.enums import ProcessingStatus
from app.services.ingestion import ingest_text
from app.storage.models import Item
from app.workers.processing import ProcessingWorker, requeue_stale


async def seed(session_factory, message_id: int = 1, text: str = "note"):
    return await ingest_text(
        session_factory, telegram_user_id=42, chat_id=42, message_id=message_id, text=text
    )


async def get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


async def test_claim_transitions_queued_to_processing(session_factory):
    item = await seed(session_factory)
    worker = ProcessingWorker(session_factory)
    claimed = await worker.claim_next()
    assert claimed == item.id
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.PROCESSING
    assert stored.processing_stage == "PROCESSING"


async def test_claim_is_atomic_no_double_processing(session_factory):
    await seed(session_factory)
    worker = ProcessingWorker(session_factory)
    first = await worker.claim_next()
    second = await worker.claim_next()
    assert first is not None
    assert second is None


async def test_worker_processes_queued_to_ready(session_factory):
    item = await seed(session_factory)
    worker = ProcessingWorker(session_factory)
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.processing_stage == "READY"
    assert stored.error_code is None


async def test_worker_marks_failed_on_exception(session_factory):
    item = await seed(session_factory)

    class FailingWorker(ProcessingWorker):
        async def process_item(self, session, item):
            raise RuntimeError("boom")

    worker = FailingWorker(session_factory)
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.FAILED
    assert stored.processing_stage == "FAILED"
    assert stored.error_code == "UNKNOWN"
    assert "boom" in stored.error_message


async def test_worker_claims_oldest_first(session_factory):
    older = await seed(session_factory, message_id=1)
    newer = await seed(session_factory, message_id=2)
    worker = ProcessingWorker(session_factory)
    assert await worker.claim_next() == older.id
    assert await worker.claim_next() == newer.id


async def test_processed_item_not_claimed_again(session_factory):
    item = await seed(session_factory)
    worker = ProcessingWorker(session_factory)
    await worker.process_one()
    assert await worker.claim_next() is None
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY


async def test_requeue_stale_returns_processing_to_queued(session_factory):
    item = await seed(session_factory)
    worker = ProcessingWorker(session_factory)
    await worker.claim_next()
    assert await requeue_stale(session_factory) == 1
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.QUEUED
    assert stored.processing_stage == "REQUEUED"
    assert await requeue_stale(session_factory) == 0
