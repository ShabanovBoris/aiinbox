"""Critical background runner for durable standalone Ask requests."""

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.llm.base import LlmProvider
from app.services.ask_inbox import (
    AskInboxService,
    ask_failure_code,
    claim_oldest_ask_job,
)

log = logging.getLogger(__name__)


class AskWorker:
    """Keep retrieval/provider latency out of Telegram handlers and resume after restart."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        provider: LlmProvider,
        poll_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.service = AskInboxService(session_factory, provider)
        self.poll_seconds = poll_seconds

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    async def process_one(self) -> bool:
        job = await claim_oldest_ask_job(self.session_factory)
        if job is None:
            return False
        try:
            await self.service.process(job.id)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            log.error(
                "ask worker database failure job=%s user_id=%s",
                job.id,
                job.user_id,
            )
            raise
        except Exception as exc:
            code = ask_failure_code(exc)
            log.warning(
                "ask worker failed job=%s user_id=%s code=%s",
                job.id,
                job.user_id,
                code,
            )
            await self.service.fail(job.id, job.user_id, code)
        return True
