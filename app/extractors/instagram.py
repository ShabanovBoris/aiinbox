"""Bounded Instagram Reel extraction through yt-dlp and the existing STT boundary.

The adapter converts one validated Reel URL into NormalizedContent. The
ProcessingPipeline owns persistence, vision, aggregation, and retry state.
"""

import asyncio
import math
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yt_dlp

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.extractors.video import probe_media_duration
from app.llm.base import TranscriptionProvider, TranscriptionSegmentCheckpoint
from app.services.url_parsing import normalize_url
from app.storage.models import Item, ItemSource

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
_REEL_PATH = re.compile(r"^/reel/([^/]+)/?$")
_DESCRIPTION_LIMIT = 2_000


def is_instagram_reel_url(url: str) -> bool:
    """Recognize only a concrete Reel URL on Instagram's canonical hosts."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower() not in _INSTAGRAM_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        return False
    match = _REEL_PATH.fullmatch(parts.path)
    return bool(match and match.group(1) not in {".", ".."})


def default_ydl_factory(options: dict) -> yt_dlp.YoutubeDL:
    """Keep yt-dlp construction injectable so extraction remains offline-testable."""
    return yt_dlp.YoutubeDL(options)


def _map_ytdlp_error(exc: Exception) -> AppError:
    """Map a few stable yt-dlp failure signals without exposing provider details."""
    message = str(exc).lower()
    if re.search(r"\b429\b", message) or "rate limit" in message or "too many requests" in message:
        return AppError("RATE_LIMITED", "Instagram temporarily rate-limited extraction")
    if (
        re.search(r"\b(?:401|403)\b", message)
        or "login required" in message
        or "log in" in message
        or "sign in" in message
        or "private account" in message
        or "private video" in message
        or "authentication required" in message
    ):
        # Failures are never retried by a worker automatically. Keeping this
        # non-permanent allows the user to retry after an operator changes cookies.
        return AppError("AUTH_REQUIRED", "Instagram requires configured access")
    if (
        re.search(r"\b(?:404|410)\b", message)
        or "unsupported url" in message
        or "not a valid url" in message
    ):
        return AppError(
            "UNSUPPORTED_SOURCE", "Instagram Reel is unavailable or unsupported", permanent=True
        )
    if "timeout" in message or "timed out" in message:
        return AppError("TIMEOUT", "Instagram extraction timed out")
    if "configured media size limit" in message or "max-filesize" in message:
        return AppError("TOO_LARGE", "Instagram media exceeds the configured byte limit", True)
    return AppError("DOWNLOAD_FAILED", "Instagram extraction failed")


class InstagramExtractor:
    """Extract one Reel while leaving durable state and analysis to the pipeline.

    This adapter owns the yt-dlp boundary, bounded temporary media, metadata
    normalization, and STT call. Keeping it source-local lets the existing
    ItemSource checkpoint/retry path remain canonical.
    """

    def __init__(
        self,
        transcriber: TranscriptionProvider,
        temp_dir: Path,
        *,
        max_duration_seconds: int = 7_200,
        max_audio_bytes: int = 50_000_000,
        max_video_bytes: int = 50_000_000,
        cookies_file: Path | str | None = None,
        ydl_factory: Callable[[dict], Any] = default_ydl_factory,
        duration_probe: Callable[[Path], int] = probe_media_duration,
        timeout_seconds: float = 60.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ):
        self.transcriber = transcriber
        self.temp_dir = Path(temp_dir)
        self.max_duration_seconds = max_duration_seconds
        self.max_audio_bytes = max_audio_bytes
        self.max_video_bytes = max_video_bytes
        self.cookies_file = Path(cookies_file) if cookies_file else None
        self._ydl_factory = ydl_factory
        self._duration_probe = duration_probe
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    async def extract(
        self,
        source: Item | ItemSource,
        *,
        completed_segments: Mapping[int, TranscriptionSegmentCheckpoint] | None = None,
        on_segment: Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]] | None = None,
    ) -> NormalizedContent:
        """Return transcript content or an explicit no-transcript fallback candidate."""
        url = source.source_url
        if not url or not is_instagram_reel_url(url):
            raise AppError(
                "UNSUPPORTED_SOURCE", "send a link to one Instagram Reel", permanent=True
            )

        work_dir = self.temp_dir / f"ig-{uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await self._info(url)
            content = self._content_from_info(info, url)
            duration = content.duration_seconds
            if duration is not None:
                self._validate_duration(duration)
                source.content_duration_seconds = duration

            if self._has_no_audio(info):
                if duration is None:
                    video_path = await self._download_media(
                        url, work_dir, media_kind="video", byte_limit=self.max_video_bytes
                    )
                    duration = await self._probe_duration(video_path)
                    self._validate_duration(duration)
                    source.content_duration_seconds = duration
                    content.duration_seconds = duration
                content.metadata["transcript_error_code"] = "NO_AUDIO_TRACK"
                return content

            audio_path = await self._download_media(
                url, work_dir, media_kind="audio", byte_limit=self.max_audio_bytes
            )
            try:
                if duration is None:
                    # yt-dlp metadata can omit duration; probe the downloaded
                    # media before invoking the more expensive transcription API.
                    duration = await self._probe_duration(audio_path)
                    self._validate_duration(duration)
                    source.content_duration_seconds = duration
                    content.duration_seconds = duration
                try:
                    transcript = await self.transcriber.transcribe(
                        audio_path,
                        duration_seconds=duration,
                        completed_segments=completed_segments,
                        on_segment=on_segment,
                    )
                except AppError as exc:
                    if exc.code not in {"NO_AUDIO_TRACK", "EMPTY_TRANSCRIPT"}:
                        raise
                    content.metadata["transcript_error_code"] = exc.code
                    return content
            finally:
                audio_path.unlink(missing_ok=True)

            if not transcript:
                content.metadata["transcript_error_code"] = "EMPTY_TRANSCRIPT"
                return content
            content.text = transcript
            content.metadata["via_stt"] = True
            return content
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def download_video(self, source: Item | ItemSource, work_dir: Path) -> Path:
        """Provide one bounded video file to the shared representative-frame path."""
        url = source.source_url
        if not url or not is_instagram_reel_url(url):
            raise AppError(
                "UNSUPPORTED_SOURCE", "send a link to one Instagram Reel", permanent=True
            )
        info = await self._info(url)
        duration = self._duration(info)
        if duration is not None:
            self._validate_duration(duration)
            source.content_duration_seconds = duration
        video_path = await self._download_media(
            url, work_dir, media_kind="video", byte_limit=self.max_video_bytes
        )
        if duration is None:
            duration = await self._probe_duration(video_path)
            self._validate_duration(duration)
            source.content_duration_seconds = duration
        return video_path

    async def _info(self, url: str) -> dict:
        options = self._base_options(skip_download=True)

        def extract() -> dict:
            with self._ydl_factory(options) as ydl:
                return ydl.extract_info(url, download=False)

        info = await self._call_ytdlp(extract)
        self._validate_single_media(info)
        return info

    async def _download_media(
        self, url: str, work_dir: Path, *, media_kind: str, byte_limit: int
    ) -> Path:
        format_selector = (
            "bestaudio/best"
            if media_kind == "audio"
            else "bestvideo[height<=720]/best[height<=720]"
        )
        stem = "audio" if media_kind == "audio" else "video"
        options = self._base_options(
            skip_download=False,
            format=format_selector,
            max_filesize=byte_limit,
            outtmpl=str(work_dir / f"{stem}.%(ext)s"),
            progress_hooks=[self._size_limit_hook(byte_limit)],
        )

        def download() -> Path:
            with self._ydl_factory(options) as ydl:
                info = ydl.extract_info(url, download=True)
                self._validate_single_media(info)
                prepared = Path(ydl.prepare_filename(info))
                if not prepared.is_absolute():
                    prepared = work_dir / prepared
                path = prepared
                if not path.is_file():
                    candidates = sorted(
                        candidate
                        for candidate in work_dir.glob(f"{stem}.*")
                        if candidate.is_file() and not candidate.name.endswith(".part")
                    )
                    if not candidates:
                        raise AppError("DOWNLOAD_FAILED", "Instagram media file is missing")
                    path = candidates[0]
                if path.is_symlink() or path.resolve().parent != work_dir.resolve():
                    raise AppError(
                        "DOWNLOAD_FAILED", "yt-dlp output escaped its temporary directory"
                    )
                if path.stat().st_size > byte_limit:
                    raise AppError(
                        "TOO_LARGE", "Instagram media exceeds the configured byte limit", True
                    )
                return path

        return await self._call_ytdlp(download)

    @staticmethod
    def _size_limit_hook(byte_limit: int) -> Callable[[dict], None]:
        """Stop yt-dlp during transfer when reported bytes or its estimate exceed the cap."""

        def enforce_limit(status: dict) -> None:
            if status.get("status") != "downloading":
                return
            downloaded = status.get("downloaded_bytes") or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if downloaded > byte_limit or (total is not None and total > byte_limit):
                raise yt_dlp.utils.DownloadError("Configured media size limit exceeded")

        return enforce_limit

    def _base_options(self, **overrides: Any) -> dict:
        """Centralize yt-dlp limits so metadata and media calls share safety flags."""
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self.timeout_seconds,
        }
        if self.cookies_file is not None:
            if not self.cookies_file.is_file() or not os.access(self.cookies_file, os.R_OK):
                raise AppError("AUTH_REQUIRED", "configured Instagram cookies file is unavailable")
            options["cookiefile"] = str(self.cookies_file)
        options.update(overrides)
        return options

    async def _call_ytdlp(self, operation: Callable[[], Any]) -> Any:
        """Run synchronous yt-dlp with socket timeout and finite transient retries."""
        last_error: AppError | None = None
        for attempt in range(self.max_attempts):
            try:
                return await self._run_sync(operation)
            except AppError as exc:
                last_error = exc
            except yt_dlp.utils.YoutubeDLError as exc:
                last_error = _map_ytdlp_error(exc)
            except OSError:
                last_error = AppError("DOWNLOAD_FAILED", "Instagram media file operation failed")
            if last_error.permanent or attempt + 1 == self.max_attempts:
                raise last_error
            await asyncio.sleep(self.backoff_seconds * (2**attempt))
        raise last_error  # pragma: no cover

    @staticmethod
    async def _run_sync(operation: Callable[[], Any]) -> Any:
        """Wait for the bounded yt-dlp thread before temp cleanup on cancellation."""
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except BaseException:
                pass
            raise

    async def _probe_duration(self, media_path: Path) -> int:
        """Probe downloaded media off-loop before any unbounded duration can reach STT."""
        return await self._run_sync(lambda: self._duration_probe(media_path))

    def _validate_duration(self, duration: int) -> None:
        """Stop before STT or frame analysis whenever the configured limit is exceeded."""
        if duration > self.max_duration_seconds:
            raise AppError(
                "TOO_LARGE",
                f"Instagram Reel duration exceeds {self.max_duration_seconds} seconds",
                permanent=True,
            )

    @staticmethod
    def _duration(info: dict) -> int | None:
        """Convert optional yt-dlp duration metadata to a conservative integer bound."""
        value = info.get("duration")
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        return math.ceil(seconds)

    @staticmethod
    def _has_no_audio(info: dict) -> bool:
        """Skip STT only when every reported format explicitly has no audio codec."""
        formats = info.get("formats")
        return bool(
            isinstance(formats, list)
            and formats
            and all(
                isinstance(media_format, dict) and media_format.get("acodec") == "none"
                for media_format in formats
            )
        )

    @staticmethod
    def _validate_single_media(info: Any) -> None:
        """Reject collection-shaped responses before they can expand one source URL."""
        if not isinstance(info, dict):
            raise AppError("EXTRACTION_FAILED", "Instagram Reel metadata is unavailable")
        if "entries" in info or info.get("_type") in {"playlist", "multi_video"}:
            raise AppError(
                "UNSUPPORTED_SOURCE", "Instagram collections are not supported", permanent=True
            )
        identifier = info.get("id")
        if not isinstance(identifier, (str, int)) or not str(identifier).strip():
            raise AppError("EXTRACTION_FAILED", "Instagram Reel identity is missing")

    @classmethod
    def _content_from_info(cls, info: dict, requested_url: str) -> NormalizedContent:
        """Project provider metadata into bounded context with a stable Reel URL."""
        description = info.get("description") or info.get("caption")
        description = (
            description.strip()[:_DESCRIPTION_LIMIT] if isinstance(description, str) else ""
        )
        creator = info.get("uploader") or info.get("creator")
        username = info.get("uploader_id") or info.get("channel_id")
        creator = creator.strip()[:200] if isinstance(creator, str) else None
        username = username.strip()[:200] if isinstance(username, str) else None
        title = info.get("title")
        title = title.strip()[:300] if isinstance(title, str) and title.strip() else None
        duration = cls._duration(info)
        webpage_url = info.get("webpage_url")
        canonical_url = (
            normalize_url(webpage_url)
            if isinstance(webpage_url, str) and is_instagram_reel_url(webpage_url)
            else normalize_url(requested_url)
        )
        return NormalizedContent(
            source_type=SourceType.INSTAGRAM,
            title=title,
            text="",
            url=canonical_url,
            author=creator,
            source_context=description or None,
            duration_seconds=duration,
            metadata={
                "instagram_id": str(info["id"])[:128],
                "creator": creator,
                "username": username,
                "description_excerpt": description or None,
            },
        )
