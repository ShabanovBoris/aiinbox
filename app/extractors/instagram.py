"""Bounded Instagram Reel extraction through yt-dlp and the existing STT boundary.

The adapter converts one validated Reel URL into NormalizedContent. The
ProcessingPipeline owns persistence, vision, aggregation, and retry state.
"""

import asyncio
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yt_dlp

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.extractors.instagram_worker import size_limit_hook
from app.extractors.subprocess_runner import run_killable_subprocess
from app.extractors.video import probe_media_duration
from app.llm.base import TranscriptionProvider, TranscriptionSegmentCheckpoint
from app.services.url_parsing import normalize_url
from app.storage.models import Item, ItemSource

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
_REEL_PATH = re.compile(r"^/reel/([^/]+)/?$")
_DESCRIPTION_LIMIT = 2_000
_DURATION_PROBE_TIMEOUT_SECONDS = 15.0
_WORKER_OUTPUT_LIMIT_BYTES = 64_000


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

    async def download_video(
        self,
        source: Item | ItemSource,
        work_dir: Path,
        *,
        byte_limit: int | None = None,
        include_audio: bool = False,
    ) -> Path:
        """Provide a bounded Reel for representative frames or an explicit Telegram send.

        A caller may impose a narrower transport limit while the extractor keeps
        its configured cap as the upper bound for every consumer. Frame analysis
        defaults to video-only; user delivery opts into the combined audio/video stream.
        """
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
        effective_limit = self.max_video_bytes
        if byte_limit is not None:
            effective_limit = min(effective_limit, byte_limit)
        video_path = await self._download_media(
            url,
            work_dir,
            media_kind="video",
            byte_limit=effective_limit,
            include_audio=include_audio,
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

        info = await self._call_ytdlp(
            extract,
            process_request={"mode": "info", "url": url, "options": options},
        )
        self._validate_single_media(info)
        return info

    async def _download_media(
        self,
        url: str,
        work_dir: Path,
        *,
        media_kind: str,
        byte_limit: int,
        include_audio: bool = False,
    ) -> Path:
        format_selector = (
            "bestaudio/best"
            if media_kind == "audio"
            else (
                "best[height<=720]/bestvideo[height<=720]+bestaudio/best"
                if include_audio
                else "bestvideo[height<=720]/best[height<=720]"
            )
        )
        stem = "audio" if media_kind == "audio" else "video"
        download_dir = self.temp_dir / f"ig-ytdlp-{uuid4().hex}"
        options = self._base_options(
            skip_download=False,
            format=format_selector,
            max_filesize=byte_limit,
            outtmpl=str(download_dir / f"{stem}.%(ext)s"),
        )
        fake_options = {
            **options,
            "progress_hooks": [self._size_limit_hook(byte_limit)],
        }

        def download() -> Path:
            download_dir.mkdir(parents=True, exist_ok=True)
            with self._ydl_factory(fake_options) as ydl:
                info = ydl.extract_info(url, download=True)
                self._validate_single_media(info)
                prepared = Path(ydl.prepare_filename(info))
                if (
                    not prepared.is_absolute()
                    and prepared.resolve().parent != download_dir.resolve()
                ):
                    prepared = download_dir / prepared
                path = prepared
                if not path.is_file():
                    candidates = sorted(
                        candidate
                        for candidate in download_dir.glob(f"{stem}.*")
                        if candidate.is_file() and not candidate.name.endswith(".part")
                    )
                    if not candidates:
                        raise AppError("DOWNLOAD_FAILED", "Instagram media file is missing")
                    path = candidates[0]
                if path.is_symlink() or path.resolve().parent != download_dir.resolve():
                    raise AppError(
                        "DOWNLOAD_FAILED", "yt-dlp output escaped its temporary directory"
                    )
                if path.stat().st_size > byte_limit:
                    raise AppError(
                        "TOO_LARGE", "Instagram media exceeds the configured byte limit", True
                    )
                # ❌ Удален перенос файла внутри потока: при отмене он мог
                # конкурировать с очисткой work_dir.
                return path

        # Keep the yt-dlp directory alive until its child exits; promotion
        # happens only after the process can no longer modify the file.
        downloaded_path = await self._call_ytdlp(
            download,
            cleanup_dir=download_dir,
            retain_on_success=True,
            process_request={
                "mode": "download",
                "url": url,
                "options": options,
                "byte_limit": byte_limit,
                "download_dir": str(download_dir),
                "stem": stem,
            },
        )
        try:
            destination = work_dir / f"{stem}{downloaded_path.suffix}"
            shutil.move(downloaded_path, destination)
            return destination
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)

    @staticmethod
    def _size_limit_hook(byte_limit: int) -> Callable[[dict], None]:
        """Keep the injected adapter and killable worker on the same transfer cap."""
        # ❌ Удалена вторая реализация проверки размера: оба пути используют общий hook.
        return size_limit_hook(byte_limit)

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

    async def _call_ytdlp(
        self,
        operation: Callable[[], Any],
        *,
        cleanup_dir: Path | None = None,
        retain_on_success: bool = False,
        process_request: dict | None = None,
    ) -> Any:
        """Keep provider retries here while production calls run in killable children."""
        last_error: AppError | None = None

        def cleanup_download_dir() -> None:
            """Release the private download directory after yt-dlp closes its files."""
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

        for attempt in range(self.max_attempts):
            defer_cleanup = False
            retain_cleanup = False
            use_child_process = (
                process_request is not None and self._ydl_factory is default_ydl_factory
            )
            try:
                if use_child_process:
                    result = await self._run_ytdlp_process(
                        process_request, cleanup_dir, retain_on_success
                    )
                else:
                    # Injected factories preserve the deterministic offline test seam.
                    result = await self._run_sync(
                        operation,
                        on_cancellation=(cleanup_download_dir if cleanup_dir is not None else None),
                    )
                retain_cleanup = cleanup_dir is not None and retain_on_success
                return result
            except asyncio.CancelledError:
                # ❌ Удалено ожидание yt-dlp-потока при отмене: оно могло
                # удерживать Item в PROCESSING после дедлайна.
                # Thread test adapters defer cleanup; production waits for its child process group.
                defer_cleanup = cleanup_dir is not None and not use_child_process
                raise
            except AppError as exc:
                last_error = exc
            except yt_dlp.utils.YoutubeDLError as exc:
                last_error = _map_ytdlp_error(exc)
            except OSError:
                last_error = AppError("DOWNLOAD_FAILED", "Instagram media file operation failed")
            finally:
                if (
                    cleanup_dir is not None
                    and not use_child_process
                    and not defer_cleanup
                    and not retain_cleanup
                ):
                    shutil.rmtree(cleanup_dir, ignore_errors=True)
            if last_error.permanent or attempt + 1 == self.max_attempts:
                raise last_error
            await asyncio.sleep(self.backoff_seconds * (2**attempt))
        raise last_error  # pragma: no cover

    async def _run_ytdlp_process(
        self, request: dict, cleanup_dir: Path | None, retain_on_success: bool
    ) -> Any:
        """Run yt-dlp outside the event loop so cancellation can kill its owner process."""
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            output = await self._run_subprocess(
                [sys.executable, "-m", "app.extractors.instagram_worker"],
                payload,
                timeout_seconds=self.timeout_seconds,
                cleanup_dir=cleanup_dir,
                retain_dir_on_success=retain_on_success,
            )
            try:
                response = json.loads(output)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppError(
                    "DOWNLOAD_FAILED", "Instagram extraction worker returned invalid data"
                ) from exc
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise AppError(
                    "DOWNLOAD_FAILED", "Instagram extraction worker returned invalid data"
                )
            if not response["ok"]:
                kind = response.get("kind")
                message = response.get("message")
                if kind == "app":
                    raise AppError(
                        str(response.get("code", "DOWNLOAD_FAILED")),
                        str(message or "Instagram extraction failed"),
                        bool(response.get("permanent")),
                    )
                if kind == "yt_dlp":
                    raise yt_dlp.utils.DownloadError(str(message or "Instagram extraction failed"))
                if kind == "os":
                    raise OSError(str(message or "Instagram media file operation failed"))
                raise AppError("DOWNLOAD_FAILED", "Instagram extraction worker failed")

            result = response.get("result")
            if not isinstance(result, dict):
                raise AppError(
                    "DOWNLOAD_FAILED", "Instagram extraction worker returned invalid data"
                )
            info = result.get("info")
            if request["mode"] == "info":
                self._validate_single_media(info)
                return info

            self._validate_single_media(info)
            return self._resolve_download_path(result.get("prepared_path"), request)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The child exited before response validation, so its private files are safe to remove.
            if cleanup_dir is not None and "output" in locals():
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            raise

    @staticmethod
    async def _run_subprocess(
        command: list[str],
        payload: bytes,
        *,
        timeout_seconds: float,
        cleanup_dir: Path | None = None,
        retain_dir_on_success: bool = False,
    ) -> bytes:
        """Delegate process ownership so Instagram and YouTube share cancellation semantics."""
        # ❌ Удалены _stop_subprocess() и _wait_for_process_group_exit():
        # общий runner завершает group до очистки и обслуживает оба extractor-а.
        return await run_killable_subprocess(
            command,
            payload,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=_WORKER_OUTPUT_LIMIT_BYTES,
            operation_name="Instagram extraction",
            cleanup_dir=cleanup_dir,
            retain_dir_on_success=retain_dir_on_success,
        )

    @staticmethod
    def _resolve_download_path(prepared_path: Any, request: dict) -> Path:
        """Validate a child result only after the downloader process has exited."""
        download_dir = Path(request["download_dir"])
        path = Path(prepared_path) if isinstance(prepared_path, str) else download_dir / ""
        # A relative prepared path can already include the relative outtmpl directory.
        if not path.is_absolute() and path.resolve().parent != download_dir.resolve():
            path = download_dir / path
        if not path.is_file():
            stem = request["stem"]
            candidates = sorted(
                candidate
                for candidate in download_dir.glob(f"{stem}.*")
                if candidate.is_file() and not candidate.name.endswith(".part")
            )
            if not candidates:
                raise AppError("DOWNLOAD_FAILED", "Instagram media file is missing")
            path = candidates[0]
        if path.is_symlink() or path.resolve().parent != download_dir.resolve():
            raise AppError("DOWNLOAD_FAILED", "yt-dlp output escaped its temporary directory")
        if path.stat().st_size > int(request["byte_limit"]):
            raise AppError("TOO_LARGE", "Instagram media exceeds the configured byte limit", True)
        return path

    @staticmethod
    async def _run_sync(
        operation: Callable[[], Any], *, on_cancellation: Callable[[], None] | None = None
    ) -> Any:
        """Release the async worker promptly without racing a blocking thread's files."""
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            def reap(completed: asyncio.Task[Any]) -> None:
                """Consume late thread results and release its temporary-directory lease."""
                try:
                    completed.result()
                except BaseException:
                    pass
                if on_cancellation is not None:
                    on_cancellation()

            task.add_done_callback(reap)
            raise

    async def _probe_duration(self, media_path: Path) -> int:
        """Probe downloaded media off-loop before any unbounded duration can reach STT."""
        if self._duration_probe is not probe_media_duration:
            return await self._run_sync(lambda: self._duration_probe(media_path))

        try:
            output = await self._run_subprocess(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path.resolve()),
                ],
                b"",
                timeout_seconds=_DURATION_PROBE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise AppError(
                "EXTRACTION_FAILED", "ffprobe is required to validate Instagram duration"
            ) from exc
        except AppError as exc:
            if exc.code == "TIMEOUT":
                raise AppError("TIMEOUT", "Instagram duration probe timed out") from exc
            raise

        try:
            duration = float(output.decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise AppError("EXTRACTION_FAILED", "Instagram duration is unavailable") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise AppError("EXTRACTION_FAILED", "Instagram duration is unavailable")
        return math.ceil(duration)

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
        if isinstance(info.get("_all_formats_no_audio"), bool):
            return info["_all_formats_no_audio"]
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
