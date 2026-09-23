"""YouTube extractor: yt-dlp Python API (без shell), playlist запрещён.

Стратегия транскрипта (ТЗ §22): human subtitles → automatic captions →
скачивание аудио + STT fallback. Субтитры уже пригодные — STT не вызывается;
перебираются ВСЕ кандидаты субтитров по порядку до первого пригодного.

Resource invariants (ТЗ §66, AGENTS §25/§27): инкрементальный byte-cap на все
загрузки, bounded retry transient-ошибок, собственная temp-поддиректория на
extraction (уникальная — нет коллизий между параллельными обработками), полная
очистка при успехе/ошибке/отмене.
"""

import asyncio
import json
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import yt_dlp

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.extractors.subprocess_runner import (
    cleanup_temporary_directory,
    run_killable_subprocess,
)
from app.llm.base import TranscriptionProvider, TranscriptionSegmentCheckpoint
from app.services.subtitles import parse_subtitles
from app.storage.models import Item, ItemSource

log = logging.getLogger(__name__)

_WORKER_OUTPUT_LIMIT_BYTES = 64_000
_DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 300.0

_YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def is_youtube_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return (
        host in _YOUTUBE_HOSTS
        or host.endswith(".youtube.com")
        or host.endswith(".youtube-nocookie.com")
    )


def default_ydl_factory(options: dict) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(options)


class YoutubeExtractor:
    def __init__(
        self,
        transcriber: TranscriptionProvider,
        temp_dir: Path,
        max_duration_seconds: int = 7200,
        max_audio_bytes: int = 50_000_000,
        max_video_bytes: int = 50_000_000,
        max_subtitle_bytes: int = 2_000_000,
        subtitle_langs: tuple[str, ...] = ("ru", "en"),
        ydl_factory: Callable[[dict], Any] = default_ydl_factory,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        timeout_seconds: float = 60.0,
        download_timeout_seconds: float = _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ):
        self.transcriber = transcriber
        self.temp_dir = Path(temp_dir)
        self.max_duration_seconds = max_duration_seconds
        self.max_audio_bytes = max_audio_bytes
        self.max_video_bytes = max_video_bytes
        self.max_subtitle_bytes = max_subtitle_bytes
        self.subtitle_langs = subtitle_langs
        self._ydl_factory = ydl_factory
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
        )
        self.timeout_seconds = timeout_seconds
        self.download_timeout_seconds = download_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds

    async def extract(
        self,
        item: Item | ItemSource,
        *,
        completed_segments: Mapping[int, TranscriptionSegmentCheckpoint] | None = None,
        on_segment: Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]] | None = None,
    ) -> NormalizedContent:
        """Собственная temp-поддиректория на extraction: уникальна для параллельных
        обработок, полностью удаляется при успехе/ошибке/отмене (ТЗ §25, §27)."""
        work_dir = self.temp_dir / f"yt-{uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await self._info(item.source_url)
            title = info.get("title")
            description = (info.get("description") or "").strip()[:2000]
            duration = info.get("duration")
            if duration and duration > self.max_duration_seconds:
                raise AppError(
                    "TOO_LARGE",
                    f"video duration {duration}s exceeds {self.max_duration_seconds}s",
                    permanent=True,
                )
            if duration:
                # Persist duration so visual fallback can sample the full timeline.
                item.content_duration_seconds = duration

            transcript, cues = await self._transcript_from_subtitles(info)
            via_stt = transcript is None
            if transcript is None:
                formats = info.get("formats")
                if (
                    isinstance(formats, list)
                    and formats
                    and all(
                        isinstance(video_format, dict) and video_format.get("acodec") == "none"
                        for video_format in formats
                    )
                ):
                    # Only explicit no-audio metadata is enough to skip STT.
                    # The pipeline can then try the visual-only path.
                    raise AppError(
                        "NO_AUDIO_TRACK",
                        "YouTube video has no audio track",
                        permanent=True,
                    )
                # fallback: скачиваем аудио и транскрибируем
                audio_path = await self._download_audio(item.source_url, work_dir)
                try:
                    transcript = await self.transcriber.transcribe(
                        audio_path,
                        duration_seconds=duration,
                        completed_segments=completed_segments,
                        on_segment=on_segment,
                    )
                finally:
                    # guard: пустой prepare_filename даёт Path(".") — не удаляем его
                    if str(audio_path) not in ("", "."):
                        audio_path.unlink(missing_ok=True)
            if not transcript:
                raise AppError("EMPTY_TRANSCRIPT", "empty YouTube transcript")

            canonical = info.get("webpage_url") or item.source_url
            return NormalizedContent(
                source_type=SourceType.YOUTUBE,
                title=title,
                text=transcript,
                url=canonical,
                user_note=item.user_note or None,
                duration_seconds=duration,
                metadata={
                    # description_excerpt нормализуется в None при пустоте:
                    # resume-restore должен давать эквивалентный NormalizedContent
                    "description_excerpt": description or None,
                    "via_stt": via_stt,
                    # cues нормализуются к list[list] — JSON round-trip
                    # сохраняет равенство initial/resumed content
                    "cues": [list(cue) for cue in cues] if cues else None,
                },
            )
        finally:
            cleanup_temporary_directory(work_dir)

    async def _info(self, url: str) -> dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self.timeout_seconds,
        }

        def _extract() -> dict:
            with self._ydl_factory(options) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_extract)
        except yt_dlp.utils.DownloadError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp failed: {exc}") from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp error: {exc}") from exc
        if info is None:
            raise AppError("DOWNLOAD_FAILED", "no info extracted")
        if "entries" in info:
            # playlist без одиночного видео не обрабатываем (ТЗ §22)
            raise AppError(
                "UNSUPPORTED_SOURCE",
                "playlists are not supported; send a single video URL",
                permanent=True,
            )
        return info

    def _subtitle_candidates(self, info: dict) -> list[str]:
        """Строгий порядок (ТЗ §22): СНАЧАЛА все human subtitles (config langs →
        любые), ПОТОМ все automatic captions (config langs → любые).
        URL дедуплицируются с сохранением порядка."""
        candidates: list[str] = []
        seen: set[str] = set()

        def collect(source: str) -> None:
            tracks = info.get(source) or {}
            for lang in self.subtitle_langs:
                for track in tracks.get(lang) or []:
                    url = track.get("url")
                    # safe get: track без url не должен ронять fallback chain
                    if url and track.get("ext") in ("vtt", "srt") and url not in seen:
                        seen.add(url)
                        candidates.append(url)
            for lang, lang_tracks in tracks.items():
                if lang in self.subtitle_langs:
                    continue
                for track in lang_tracks or []:
                    url = track.get("url")
                    if url and track.get("ext") in ("vtt", "srt") and url not in seen:
                        seen.add(url)
                        candidates.append(url)

        collect("subtitles")
        collect("automatic_captions")
        return candidates

    async def _transcript_from_subtitles(self, info: dict) -> tuple[str | None, list]:
        """Перебирает ВСЕХ кандидатов субтитров по порядку (human → auto, языки из
        конфига → любые): первый пригодный (>=40 символов) становится транскриптом.
        STT включается только если ни один кандидат не пригоден."""
        candidates = self._subtitle_candidates(info)
        for sub_url in candidates:
            try:
                raw = await self._fetch_capped(sub_url, self.max_subtitle_bytes)
            except AppError as exc:
                # Любая ошибка кандидата (oversize/сеть/not-found) — кандидат
                # непригоден; пробуем следующего. STT — только после исчерпания.
                log.warning("subtitles candidate unusable url=%s: %s", sub_url, exc)
                continue
            text, cues = parse_subtitles(raw)
            if len(text.strip()) < 40:
                continue  # пустые/служебные субтитры — пробуем следующего кандидата
            log.info("subtitles parsed chars=%s cues=%s url=%s", len(text), len(cues), sub_url)
            # cues нормализуются к list[list] — JSON round-trip сохраняет равенство
            return text, [list(cue) for cue in cues]
        return None, []

    async def _fetch_capped(self, url: str, max_bytes: int) -> str:
        """Streamed GET с инкрементальным byte-cap и bounded retry transient'ов."""
        last_error: AppError | None = None
        for attempt in range(self.max_attempts):
            try:
                async with self._http_client_factory() as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code >= 400:
                            raise AppError(
                                "DOWNLOAD_FAILED",
                                f"HTTP {response.status_code} for {url}",
                                permanent=response.status_code < 500,
                            )
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(buffer) + len(chunk) > max_bytes:
                                raise AppError(
                                    "TOO_LARGE",
                                    f"response exceeds {max_bytes} bytes",
                                    permanent=True,
                                )
                            buffer.extend(chunk)
                        charset = response.charset_encoding or "utf-8"
                        return buffer.decode(charset, errors="replace")
            except httpx.TimeoutException:
                last_error = AppError("TIMEOUT", f"request timed out: {url}")
            except httpx.HTTPError as exc:
                last_error = AppError("DOWNLOAD_FAILED", f"download failed: {exc}")
            except AppError as exc:
                # transient AppError (не permanent) тоже ретраится bounded
                if exc.permanent:
                    raise
                last_error = exc
            await asyncio.sleep(self.backoff_seconds * (2**attempt))
        raise last_error  # pragma: no cover

    async def download_video(
        self,
        url: str,
        work_dir: Path,
        *,
        byte_limit: int | None = None,
        include_audio: bool = False,
    ) -> Path:
        """Download bounded source video for visual analysis or an explicit Telegram send.

        A caller may impose a narrower transport limit while the extractor keeps
        its configured cap as the upper bound for every consumer. Frame analysis
        defaults to video-only; user delivery opts into the combined audio/video stream.
        """
        effective_limit = self.max_video_bytes
        if byte_limit is not None:
            effective_limit = min(effective_limit, byte_limit)
        format_selector = (
            "best[height<=720]/bestvideo[height<=720]+bestaudio/best"
            if include_audio
            else "bestvideo[height<=720]/best[height<=720]"
        )
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Frame analysis stays video-only; an explicit send requests the audio track too.
            "format": format_selector,
            "max_filesize": effective_limit,
            "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
            "socket_timeout": self.timeout_seconds,
        }
        return await self._run_download(url, options, effective_limit)

    async def _download_audio(self, url: str, work_dir: Path) -> Path:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "max_filesize": self.max_audio_bytes,
            "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
            "socket_timeout": self.timeout_seconds,
        }
        return await self._run_download(url, options, self.max_audio_bytes)

    async def _run_download(self, url: str, options: dict, byte_limit: int) -> Path:
        if self._ydl_factory is default_ydl_factory:
            return await self._run_download_worker(url, options, byte_limit)

        def _download() -> Path:
            with self._ydl_factory(options) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                path = Path(filename)
                if path.name.endswith(".part"):
                    raise AppError("DOWNLOAD_FAILED", "yt-dlp left an incomplete video file")
                if not path.exists():
                    # расширение могло измениться при конвертации
                    candidates = [
                        candidate
                        for candidate in path.parent.glob(path.stem + ".*")
                        if candidate.is_file() and not candidate.name.endswith(".part")
                    ]
                    if not candidates:
                        raise AppError("DOWNLOAD_FAILED", "audio file missing after download")
                    path = candidates[0]
                if path.stat().st_size > byte_limit:
                    raise AppError(
                        "TOO_LARGE",
                        f"download exceeds {byte_limit} bytes",
                        permanent=True,
                    )
                return path

        try:
            return await asyncio.to_thread(_download)
        except AppError:
            raise  # partial-файлы убирает cleanup work_dir в extract()
        except yt_dlp.utils.DownloadError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp download failed: {exc}") from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp error: {exc}") from exc

    async def _run_download_worker(self, url: str, options: dict, byte_limit: int) -> Path:
        """Run production media downloads in a killable child; injected factories stay offline."""
        download_dir = Path(options["outtmpl"]).parent
        request = json.dumps(
            {
                "url": url,
                "options": options,
                "byte_limit": byte_limit,
                "download_dir": str(download_dir),
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        output = await run_killable_subprocess(
            [sys.executable, "-m", "app.extractors.youtube_worker"],
            request,
            timeout_seconds=self.download_timeout_seconds,
            output_limit_bytes=_WORKER_OUTPUT_LIMIT_BYTES,
            operation_name="YouTube download",
            cleanup_dir=download_dir,
            retain_dir_on_success=True,
        )

        try:
            response = json.loads(output)
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise AppError("DOWNLOAD_FAILED", "YouTube worker returned invalid data")
            if not response["ok"]:
                kind = response.get("kind")
                message = response.get("message")
                if kind == "app":
                    raise AppError(
                        str(response.get("code", "DOWNLOAD_FAILED")),
                        str(message or "YouTube download failed"),
                        bool(response.get("permanent")),
                    )
                if kind == "yt_dlp":
                    raise AppError(
                        "DOWNLOAD_FAILED", f"yt-dlp download failed: {message or 'unknown error'}"
                    )
                raise AppError("DOWNLOAD_FAILED", "YouTube worker failed")

            result = response.get("result")
            prepared_path = result.get("path") if isinstance(result, dict) else None
            if not isinstance(prepared_path, str):
                raise AppError("DOWNLOAD_FAILED", "YouTube worker returned invalid data")
            path = Path(prepared_path)
            if path.name.endswith(".part"):
                raise AppError("DOWNLOAD_FAILED", "yt-dlp left an incomplete video file")
            if not path.exists():
                candidates = [
                    candidate
                    for candidate in path.parent.glob(path.stem + ".*")
                    if candidate.is_file() and not candidate.name.endswith(".part")
                ]
                if not candidates:
                    raise AppError("DOWNLOAD_FAILED", "video file missing after download")
                path = candidates[0]
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != download_dir.resolve()
            ):
                raise AppError("DOWNLOAD_FAILED", "YouTube output escaped its temporary directory")
            if path.stat().st_size > byte_limit:
                raise AppError("TOO_LARGE", f"download exceeds {byte_limit} bytes", permanent=True)
            return path
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            shutil.rmtree(download_dir, ignore_errors=True)
            raise AppError("DOWNLOAD_FAILED", "YouTube worker returned invalid data") from exc
        except Exception:
            # The worker has exited before response parsing, so releasing its output is safe.
            shutil.rmtree(download_dir, ignore_errors=True)
            raise
