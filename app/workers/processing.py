import asyncio
import logging
import time

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enums import ProcessingStatus
from app.llm.base import LlmError
from app.services.processing import ProcessingPipeline
from app.storage.models import Item

log = logging.getLogger(__name__)


async def requeue_stale(session_factory: async_sessionmaker) -> int:
    """Возврат зависших PROCESSING в QUEUED при старте процесса.

    Процесс один (PRODUCT_SPEC §15): любой PROCESSING в БД на момент старта —
    незавершённая работа умершего процесса, её безопасно поставить в очередь заново.
    """
    async with session_factory() as session:
        result = await session.execute(
            update(Item)
            .where(Item.processing_status == ProcessingStatus.PROCESSING)
            .values(processing_status=ProcessingStatus.QUEUED, processing_stage="REQUEUED")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued stale PROCESSING items count=%s", result.rowcount)
        return result.rowcount


class ProcessingWorker:
    """Забирает oldest QUEUED атомарно и доводит Item до READY или FAILED.

    Claim выполнен одним UPDATE с условием QUEUED — два воркера физически не могут
    забрать один Item, даже без внешних блокировок (SQLite сериализует запись).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        pipeline: ProcessingPipeline,
        poll_seconds: float = 1.0,
        on_result=None,
    ):
        self.session_factory = session_factory
        self.pipeline = pipeline
        self.poll_seconds = poll_seconds
        # on_result — auxiliary-колбэк (доставка результата в Telegram);
        # его сбой не должен ломать уже готовый результат.
        self.on_result = on_result

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
                update(Item)
                .where(
                    Item.id
                    == select(Item.id)
                    .where(Item.processing_status == ProcessingStatus.QUEUED)
                    .order_by(Item.created_at, Item.id)
                    .limit(1)
                    .scalar_subquery(),
                    Item.processing_status == ProcessingStatus.QUEUED,
                )
                .values(
                    processing_status=ProcessingStatus.PROCESSING,
                    processing_stage="PROCESSING",
                    updated_at=func.now(),
                )
                .returning(Item.id)
            )
            claimed = result.scalar_one_or_none()
            await session.commit()
            return claimed

    async def mark_failed(self, item_id: int, exc: Exception) -> None:
        async with self.session_factory() as session:
            item = await session.get(Item, item_id)
            if item is None:
                return
            item.processing_status = ProcessingStatus.FAILED
            item.processing_stage = "FAILED"
            item.error_code = exc.code if isinstance(exc, LlmError) else "UNKNOWN"
            item.error_message = str(exc)[:500]
            await session.commit()

    async def process_one(self) -> bool:
        item_id = await self.claim_next()
        if item_id is None:
            return False
        started = time.monotonic()
        try:
            async with self.session_factory() as session:
                item = await session.get(Item, item_id)
                if item is None:
                    return True
                await self.pipeline.run(session, item)
                item.processing_status = ProcessingStatus.READY
                await session.commit()
        except Exception as exc:
            # Ошибка обработки не теряет Item: он переходит в FAILED с кодом
            # и остаётся доступным для Retry (PRODUCT_SPEC §56, §59).
            log.exception("item processing failed id=%s error=%s", item_id, exc)
            await self.mark_failed(item_id, exc)
            return True
        log.info(
            "item processed id=%s duration=%.3fs result=READY", item_id, time.monotonic() - started
        )
        if self.on_result is not None:
            try:
                await self.on_result(item)
            except Exception:
                log.exception("result delivery failed item_id=%s", item_id)
        return True
