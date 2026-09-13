import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)

from app.errors import AppError

log = logging.getLogger(__name__)


class TokenRedactionFilter(logging.Filter):
    """Гарантирует, что Telegram token не попадёт в записи httpx/httpcore
    логгеров: их INFO-лог печатает полный request URL, а URL скачивания файла
    содержит bot<token> по контракту Telegram Bot API."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and self._token in record.getMessage():
            record.msg = record.getMessage().replace(self._token, "***")
            record.args = None
        return True


class FileDownloader(Protocol):
    """Скачивание файла Telegram по file_id в локальную директорию.

    Граница: экстрактор не знает aiogram; тесты подставляют fake downloader.
    """

    async def download(self, file_id: str, dest_dir: Path) -> Path: ...


class TelegramFileDownloader:
    """Скачивание media из Telegram: get_file + streamed download.

    Retry policy (PRODUCT_SPEC §58): transient ошибки ретраются ограниченно
    с backoff, permanent (TOO_LARGE / 4xx / not found) — одна попытка.
    Частичный файл удаляется при любом неуспехе/отмене; byte-cap применяется
    инкрементально во время скачивания (не только post-factum stat).
    """

    def __init__(
        self,
        bot,
        max_bytes: int,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self._bot = bot
        self._max_bytes = max_bytes
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=False, timeout=60.0)
        )
        token = getattr(bot, "token", "")
        if token:
            # Redaction на logger'ах httpx/httpcore: URL скачивания содержит
            # bot<token>, и INFO-лог httpx печатает URL целиком.
            for logger_name in ("httpx", "httpcore"):
                logger = logging.getLogger(logger_name)
                if not any(
                    isinstance(f, TokenRedactionFilter) and f._token == token
                    for f in logger.filters
                ):
                    logger.addFilter(TokenRedactionFilter(token))

    async def _get_file(self, file_id: str):
        """get_file с permanent-классификацией aiogram client errors (4xx-семантика):
        not found / bad request / unauthorized / forbidden / entity too large —
        повтор бессмыслен; сетевые и серверные (5xx) ошибки остаются transient."""
        try:
            return await self._bot.get_file(file_id)
        except (
            TelegramBadRequest,
            TelegramUnauthorizedError,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramEntityTooLarge,
        ) as exc:
            raise AppError(
                "DOWNLOAD_FAILED", f"telegram client error: {exc}", permanent=True
            ) from exc

    async def download(self, file_id: str, dest_dir: Path) -> Path:
        last_exc: AppError | None = None
        for attempt in range(self._max_attempts):
            dest = Path(dest_dir) / f"{uuid4().hex}.bin"
            try:
                try:
                    tg_file = await self._get_file(file_id)
                except AppError:
                    # permanent-классификация уже проставлена (_get_file)
                    raise
                except Exception as exc:
                    raise AppError(
                        "DOWNLOAD_FAILED", f"get_file failed: {exc}", permanent=False
                    ) from exc
                if tg_file.file_size and tg_file.file_size > self._max_bytes:
                    raise AppError(
                        "TOO_LARGE", f"file exceeds {self._max_bytes} bytes", permanent=True
                    )
                url = f"https://api.telegram.org/file/bot{self._bot.token}/{tg_file.file_path}"
                await self._stream_to_file(
                    client_factory=self._client_factory,
                    url=url,
                    dest=dest,
                    max_bytes=self._max_bytes,
                )
                return dest
            except AppError as exc:
                dest.unlink(missing_ok=True)
                if exc.permanent or attempt == self._max_attempts - 1:
                    raise
                last_exc = exc
            except asyncio.CancelledError:
                dest.unlink(missing_ok=True)
                raise
            except Exception as exc:
                dest.unlink(missing_ok=True)
                if attempt == self._max_attempts - 1:
                    raise AppError(
                        "DOWNLOAD_FAILED", f"download failed: {exc}", permanent=True
                    ) from exc
                last_exc = AppError("DOWNLOAD_FAILED", f"download failed: {exc}")
            await asyncio.sleep(self._backoff_seconds * (2**attempt))
        raise last_exc or AppError("DOWNLOAD_FAILED", "download failed")  # pragma: no cover

    @staticmethod
    async def _stream_to_file(
        client_factory: Callable[[], httpx.AsyncClient],
        url: str,
        dest: Path,
        max_bytes: int,
    ) -> None:
        async with client_factory() as client:
            try:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise AppError(
                            "DOWNLOAD_FAILED",
                            f"HTTP {response.status_code} downloading media",
                            permanent=response.status_code < 500,
                        )
                    size = 0
                    with dest.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise AppError(
                                    "TOO_LARGE",
                                    f"file exceeds {max_bytes} bytes",
                                    permanent=True,
                                )
                            out.write(chunk)
            except httpx.TimeoutException as exc:
                raise AppError("TIMEOUT", "download timed out") from exc
            except httpx.HTTPError as exc:
                raise AppError("DOWNLOAD_FAILED", f"download failed: {exc}") from exc
