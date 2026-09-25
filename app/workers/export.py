"""Critical runner for durable portable-export jobs."""

import asyncio
import logging
import time

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.export import (
    ExportError,
    ExportService,
    claim_oldest_export_job,
    cleanup_export_artifacts,
)

log = logging.getLogger(__name__)


class ExportWorker:
    """Keep snapshot and ZIP work out of Telegram handlers and recover it after restart."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        export_dir: str,
        *,
        max_content_chars: int,
        retention_seconds: int,
        poll_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.service = ExportService(
            session_factory,
            export_dir,
            max_content_chars=max_content_chars,
        )
        self.export_dir = self.service.export_dir
        self.retention_seconds = retention_seconds
        self.poll_seconds = poll_seconds
        self._next_cleanup_at = 0.0

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Run generation and bounded retention cleanup as one supervised critical task."""
        while not stop.is_set():
            now = time.monotonic()
            if now >= self._next_cleanup_at:
                await cleanup_export_artifacts(
                    self.session_factory,
                    self.export_dir,
                    retention_seconds=self.retention_seconds,
                )
                self._next_cleanup_at = now + 3600
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    async def process_one(self) -> bool:
        """Claim one durable request and turn expected generation errors into an outbox notice."""
        job = await claim_oldest_export_job(self.session_factory)
        if job is None:
            return False
        try:
            await self.service.process(job)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            log.exception(
                "export worker database failure job_id=%s user_id=%s", job.id, job.user_id
            )
            raise
        except ExportError as exc:
            log.warning(
                "export generation failed job_id=%s user_id=%s mode=%s code=%s",
                job.id,
                job.user_id,
                job.mode,
                exc.code,
            )
            await self.service.fail(job.id, exc)
        except Exception as exc:
            # Filesystem/serialization failures are user-visible but must not leak
            # their exception text, which can contain private archive material.
            log.exception(
                "export generation failed job_id=%s user_id=%s mode=%s error_type=%s",
                job.id,
                job.user_id,
                job.mode,
                type(exc).__name__,
            )
            await self.service.fail(
                job.id,
                ExportError("EXPORT_FAILED", "Не удалось подготовить экспорт"),
            )
        return True
