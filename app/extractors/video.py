import asyncio
import math
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from uuid import uuid4

from app.bot.files import FileDownloader
from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.llm.base import TranscriptionProvider, TranscriptionSegmentCheckpoint
from app.storage.models import ItemSource


class VideoExtractor:
    """Telegram video → bounded temp file → audio track → transcript.

    Video remains one child source of the parent Item. Transcript persistence is
    owned by ProcessingPipeline; this adapter owns only download/conversion/STT
    and always removes temporary source/audio files after extraction.
    """

    def __init__(
        self,
        transcriber: TranscriptionProvider,
        downloader: FileDownloader,
        temp_dir: Path,
        max_duration_seconds: int = 7200,
        audio_converter: Callable[[Path, Path], None] | None = None,
        duration_probe: Callable[[Path], int] | None = None,
    ):
        self.transcriber = transcriber
        self.downloader = downloader
        self.temp_dir = Path(temp_dir)
        self.max_duration_seconds = max_duration_seconds
        self._audio_converter = audio_converter or _extract_audio_track
        self._duration_probe = duration_probe or _probe_media_duration

    async def extract(
        self,
        source: ItemSource,
        *,
        completed_segments: Mapping[int, TranscriptionSegmentCheckpoint] | None = None,
        on_segment: Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]] | None = None,
    ) -> NormalizedContent:
        """Produce transcript while preserving resumable provider checkpoints."""
        duration = source.content_duration_seconds
        if duration and duration > self.max_duration_seconds:
            raise AppError(
                "TOO_LARGE",
                f"video duration {duration}s exceeds {self.max_duration_seconds}s",
                permanent=True,
            )
        work_dir = self.temp_dir / f"video-{uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            video_path = await self.downloader.download(source.source_file_id, work_dir)
            if not duration:
                # Telegram Document does not guarantee media duration. Probe the
                # downloaded file before ffmpeg conversion/STT so the configured
                # resource bound cannot be bypassed by document-shaped video.
                duration = await asyncio.to_thread(self._duration_probe, video_path)
                source.content_duration_seconds = duration
                if duration > self.max_duration_seconds:
                    raise AppError(
                        "TOO_LARGE",
                        f"video duration {duration}s exceeds {self.max_duration_seconds}s",
                        permanent=True,
                    )
            audio_path = work_dir / "audio.wav"
            await asyncio.to_thread(self._audio_converter, video_path, audio_path)
            transcript = await self.transcriber.transcribe(
                audio_path,
                duration_seconds=duration,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
            if not transcript:
                raise AppError("EMPTY_TRANSCRIPT", "empty video transcript", permanent=True)
            return NormalizedContent(
                source_type=SourceType.VIDEO,
                text=transcript,
                duration_seconds=duration,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def download_video(self, source: ItemSource, work_dir: Path) -> Path:
        """Provide a bounded temporary video to the pipeline's optional vision step."""
        return await self.downloader.download(source.source_file_id, work_dir)


def _extract_audio_track(video_path: Path, audio_path: Path) -> None:
    """Convert video audio to provider-friendly mono WAV using structured ffmpeg args."""
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(audio_path),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=300)
    except FileNotFoundError as exc:
        raise AppError("TRANSCRIPTION_FAILED", "ffmpeg is required for video audio") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("TIMEOUT", "video audio extraction timed out") from exc
    if result.returncode != 0 or not audio_path.exists():
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes)
            else str(result.stderr or "")
        )
        if "does not contain any stream" in stderr.lower():
            raise AppError("NO_AUDIO_TRACK", "video has no audio track", permanent=True)
        raise AppError(
            "TRANSCRIPTION_FAILED",
            f"ffmpeg video audio extraction failed: {result.returncode}",
        )


def _probe_media_duration(video_path: Path) -> int:
    """Validate unknown Telegram video duration before expensive media processing.

    ffprobe is part of the ffmpeg runtime already required by the video pipeline;
    rounding up prevents a fractional duration just over the limit from slipping
    through the integer application bound.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise AppError(
            "EXTRACTION_FAILED", "ffprobe is required to validate video duration"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("TIMEOUT", "video duration probe timed out") from exc
    if result.returncode != 0:
        raise AppError("EXTRACTION_FAILED", "video duration probe failed")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise AppError("EXTRACTION_FAILED", "video duration is unavailable") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise AppError("EXTRACTION_FAILED", "video duration is unavailable")
    return math.ceil(duration)
