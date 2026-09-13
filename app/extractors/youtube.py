"""YouTube extractor: yt-dlp Python API (без shell), playlist запрещён.

Стратегия транскрипта (ТЗ §22): human subtitles → automatic captions →
скачивание аудио + STT fallback. Субтитры уже пригодные — STT не вызывается.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yt_dlp

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.llm.base import TranscriptionProvider
from app.services.subtitles import parse_subtitles
from app.storage.models import Item

log = logging.getLogger(__name__)

_YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
}


def is_youtube_url(url: str) -> bool:
    return urlparse(url).hostname in _YOUTUBE_HOSTS or (urlparse(url).hostname or "").endswith(
        ".youtube.com"
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
        subtitle_langs: tuple[str, ...] = ("ru", "en"),
        ydl_factory: Callable[[dict], Any] = default_ydl_factory,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        timeout_seconds: float = 60.0,
    ):
        self.transcriber = transcriber
        self.temp_dir = Path(temp_dir)
        self.max_duration_seconds = max_duration_seconds
        self.max_audio_bytes = max_audio_bytes
        self.subtitle_langs = subtitle_langs
        self._ydl_factory = ydl_factory
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
        )
        self.timeout_seconds = timeout_seconds

    async def extract(self, item: Item) -> NormalizedContent:
        info = await self._info(item.source_url)
        title = info.get("title")
        description = info.get("description") or ""
        duration = info.get("duration")
        if duration and duration > self.max_duration_seconds:
            raise AppError(
                "TOO_LARGE",
                f"video duration {duration}s exceeds {self.max_duration_seconds}s",
                permanent=True,
            )

        transcript = await self._transcript_from_subtitles(info)
        via_stt = transcript is None
        audio_path: Path | None = None
        if transcript is None:
            # fallback: скачиваем аудио и транскрибируем
            audio_path = await self._download_audio(item.source_url)
            try:
                transcript = await self.transcriber.transcribe(audio_path)
            except AppError:
                raise
            except Exception as exc:
                raise AppError("TRANSCRIPTION_FAILED", f"stt failed: {exc}") from exc
            finally:
                audio_path.unlink(missing_ok=True)
        if not transcript:
            raise AppError("TRANSCRIPTION_FAILED", "empty transcript")

        canonical = info.get("webpage_url") or item.source_url
        return NormalizedContent(
            source_type=SourceType.YOUTUBE,
            title=title,
            text=transcript,
            url=canonical,
            user_note=item.user_note or None,
            duration_seconds=duration,
            metadata={
                "description_excerpt": description[:2000],
                "via_stt": via_stt,
            },
        )

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

    def _pick_subtitles(self, info: dict) -> str | None:
        """human subtitles → automatic captions; языки из конфига, затем любые."""
        for source in ("subtitles", "automatic_captions"):
            tracks = info.get(source) or {}
            for lang in self.subtitle_langs:
                for track in tracks.get(lang) or []:
                    if track.get("ext") in ("vtt", "srt") and track.get("url"):
                        log.info("using %s subtitles lang=%s", source, lang)
                        return track["url"]
        return None

    async def _transcript_from_subtitles(self, info: dict) -> str | None:
        sub_url = self._pick_subtitles(info)
        if sub_url is None:
            return None
        try:
            async with self._http_client_factory() as client:
                response = await client.get(sub_url)
                response.raise_for_status()
                raw = response.text
        except httpx.TimeoutException as exc:
            raise AppError("TIMEOUT", "subtitles download timed out") from exc
        except httpx.HTTPError as exc:
            log.warning("subtitles fetch failed: %s", exc)
            return None
        text, cues = parse_subtitles(raw)
        if len(text.strip()) < 40:
            return None  # пустые/служебные субтитры не считаются пригодными
        log.info("subtitles parsed chars=%s cues=%s", len(text), len(cues))
        return text

    async def _download_audio(self, url: str) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "max_filesize": self.max_audio_bytes,
            "outtmpl": str(self.temp_dir / "%(id)s.%(ext)s"),
            "socket_timeout": self.timeout_seconds,
        }

        def _download() -> Path:
            with self._ydl_factory(options) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                path = Path(filename)
                if not path.exists():
                    # расширение могло измениться при конвертации
                    candidates = list(path.parent.glob(path.stem + ".*"))
                    if not candidates:
                        raise AppError("DOWNLOAD_FAILED", "audio file missing after download")
                    path = candidates[0]
                if path.stat().st_size > self.max_audio_bytes:
                    path.unlink(missing_ok=True)
                    raise AppError(
                        "TOO_LARGE",
                        f"audio exceeds {self.max_audio_bytes} bytes",
                        permanent=True,
                    )
                return path

        try:
            return await asyncio.to_thread(_download)
        except AppError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp download failed: {exc}") from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            raise AppError("DOWNLOAD_FAILED", f"yt-dlp error: {exc}") from exc
