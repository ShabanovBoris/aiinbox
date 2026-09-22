from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from app.bot.files import FileDownloader
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.llm.base import TranscriptionProvider, TranscriptionSegmentCheckpoint
from app.storage.models import Item


class AudioExtractor:
    """Voice/audio → временный файл → транскрипция → NormalizedContent.

    Временный файл удаляется в finally (успех/ошибка/отмена); транскрипт
    персистится пайплайном в contents (TRANSCRIPT) — retry не повторяет STT.
    """

    def __init__(
        self,
        transcriber: TranscriptionProvider,
        downloader: FileDownloader,
        temp_dir: Path,
    ):
        self.transcriber = transcriber
        self.downloader = downloader
        self.temp_dir = Path(temp_dir)

    async def extract(
        self,
        item: Item,
        *,
        completed_segments: Mapping[int, TranscriptionSegmentCheckpoint] | None = None,
        on_segment: Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]] | None = None,
    ) -> NormalizedContent:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        audio_path = await self.downloader.download(item.source_file_id, self.temp_dir)
        try:
            transcript = await self.transcriber.transcribe(
                audio_path,
                duration_seconds=item.content_duration_seconds,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
        finally:
            audio_path.unlink(missing_ok=True)

        if not transcript:
            raise AppError("TRANSCRIPTION_FAILED", "empty transcript")
        return NormalizedContent(
            source_type=item.source_type,
            text=transcript,
            # пустая заметка нормализуется в None: resume должен давать
            # эквивалентный NormalizedContent (pydantic equality)
            user_note=item.user_note or None,
            duration_seconds=item.content_duration_seconds,
        )
