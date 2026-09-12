import logging
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from aiogram import Bot

from app.errors import AppError

log = logging.getLogger(__name__)


class FileDownloader(Protocol):
    """Скачивание файла Telegram по file_id в локальную директорию.

    Граница: экстрактор не знает aiogram; тесты подставляют fake downloader.
    """

    async def download(self, file_id: str, dest_dir: Path) -> Path: ...


class TelegramFileDownloader:
    def __init__(self, bot: Bot, max_bytes: int):
        self._bot = bot
        self._max_bytes = max_bytes

    async def download(self, file_id: str, dest_dir: Path) -> Path:
        tg_file = await bot_get_file(self._bot, file_id)
        if tg_file.file_size and tg_file.file_size > self._max_bytes:
            raise AppError("TOO_LARGE", f"file exceeds {self._max_bytes} bytes")

        original_name = Path(tg_file.file_path or "audio").name
        dest = dest_dir / f"{uuid4().hex}_{original_name}"
        try:
            await self._bot.download_file(tg_file.file_path, destination=dest)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("DOWNLOAD_FAILED", f"telegram download failed: {exc}") from exc
        if dest.stat().st_size > self._max_bytes:
            dest.unlink(missing_ok=True)
            raise AppError("TOO_LARGE", f"file exceeds {self._max_bytes} bytes")
        return dest


async def bot_get_file(bot: Bot, file_id: str):
    try:
        return await bot.get_file(file_id)
    except Exception as exc:
        raise AppError("DOWNLOAD_FAILED", f"get_file failed: {exc}") from exc
