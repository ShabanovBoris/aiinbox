"""Фоновый worker /profile_update: LLM patch → DB-side merge → delivery intent.

Durable job (ТЗ §16): постановка в handler, исполнение здесь; статус PENDING/
RUNNING/DONE/FAILED переживает restart. Атомарный claim oldest PENDING.
"""

import asyncio
import logging

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.errors import AppError
from app.llm.base import LlmProvider
from app.services.profile import (
    claim_oldest_profile_update,
    update_profile_from_patch,
)
from app.storage.models import ProfileUpdateJob

log = logging.getLogger(__name__)


class ProfileUpdateWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        provider: LlmProvider,
        poll_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.provider = provider
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
        job = await claim_oldest_profile_update(self.session_factory)
        if job is None:
            return False
        try:
            # update_profile_from_patch атомарно применяет merge И переводит
            # job в DONE одной транзакцией (protocol §9.2-подобная семантика)
            _, changed = await update_profile_from_patch(self.session_factory, self.provider, job)
        except SQLAlchemyError:
            # DB/infrastructure failure must reach the critical-task supervisor;
            # startup recovery will requeue the RUNNING durable job.
            log.exception("profile worker infrastructure failure job=%s", job.id)
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "LLM_FAILED"
            log.warning("profile update failed job=%s code=%s", job.id, code)
            await finish_with_error(self.session_factory, job.id, code, str(exc)[:500])
            return True
        log.info("profile updated job=%s user_id=%s changed=%s", job.id, job.user_id, changed)
        return True


async def finish_with_error(
    session_factory: async_sessionmaker, job_id: int, code: str, message: str
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(ProfileUpdateJob)
            .where(ProfileUpdateJob.id == job_id)
            .values(status="FAILED", error_code=code, error_message=message)
        )
        await session.commit()
