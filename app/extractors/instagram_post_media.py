"""Download and prepare one Instagram post element inside the killable media worker."""

import asyncio
import json
import math
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.errors import AppError
from app.extractors.web import PinningTransport
from app.services.frames import extract_representative_frames

_PHOTO_BYTES = 10_000_000
_MAX_MEDIA_URL = 2_500
_CDN_DOMAINS = ("cdninstagram.com", "fbcdn.net")
_LOCAL_MOV = [
    "-protocol_whitelist",
    "file,pipe",
    "-enable_drefs",
    "0",
    "-max_streams",
    "16",
    "-f",
    "mov",
]
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "application/octet-stream"}
_VIDEO_TYPES = {"video/mp4", "audio/mp4", "application/mp4", "application/octet-stream"}


def is_instagram_cdn_url(url: str) -> bool:
    if not url or len(url) > _MAX_MEDIA_URL or any(ord(char) < 33 for char in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.port is None
            and not parts.fragment
            and any(host == domain or host.endswith("." + domain) for domain in _CDN_DOMAINS)
        )
    except ValueError:
        return False


async def _download(
    url: str, path: Path, byte_limit: int, types: set[str], client: httpx.AsyncClient
) -> None:
    for _ in range(5):
        if not is_instagram_cdn_url(url):
            raise AppError("SECURITY_REJECTED", "Instagram media URL or redirect rejected", True)
        try:
            async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as res:
                if res.is_redirect:
                    location = res.headers.get("location")
                    if not location:
                        raise AppError("DOWNLOAD_FAILED", "Instagram media redirect has no target")
                    url = urljoin(url, location)
                    continue
                status = res.status_code
                if status in {401, 403}:
                    raise AppError("AUTH_REQUIRED", "Instagram media access expired or was denied")
                if status == 429:
                    raise AppError("RATE_LIMITED", "Instagram temporarily rate-limited media")
                if status != 200:
                    raise AppError(
                        "DOWNLOAD_FAILED",
                        f"Instagram media returned HTTP {status}",
                        400 <= status < 500,
                    )
                media_type = res.headers.get("content-type", "").partition(";")[0].strip().lower()
                if (
                    media_type not in types
                    or res.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise AppError("UNSUPPORTED_SOURCE", "unexpected Instagram media type", True)
                length = res.headers.get("content-length", "")
                if length.isdecimal() and int(length) > byte_limit:
                    raise AppError("TOO_LARGE", "Instagram media exceeds the byte limit", True)
                size = 0
                with path.open("wb") as output:
                    async for chunk in res.aiter_raw(chunk_size=65_536):
                        size += len(chunk)
                        if size > byte_limit:
                            raise AppError(
                                "TOO_LARGE", "Instagram media exceeds the byte limit", True
                            )
                        output.write(chunk)
                if size == 0:
                    raise AppError("DOWNLOAD_FAILED", "Instagram media response was empty")
                return
        except httpx.TimeoutException:
            raise AppError("TIMEOUT", "Instagram media download timed out") from None
        except httpx.HTTPError:
            raise AppError("DOWNLOAD_FAILED", "Instagram media download failed") from None
    raise AppError("DOWNLOAD_FAILED", "Instagram media redirect limit exceeded", True)


async def _fetch(url: str, path: Path, limit: int, types: set[str]) -> None:
    async with httpx.AsyncClient(
        transport=PinningTransport(),
        follow_redirects=False,
        trust_env=False,
        timeout=15,
    ) as client:
        for attempt in range(2):
            try:
                await _download(url, path, limit, types, client)
                return
            except AppError as exc:
                if attempt or exc.permanent or exc.code not in {"TIMEOUT", "DOWNLOAD_FAILED"}:
                    raise
                await asyncio.sleep(0.5)


def _run(argv: list[str], error_code: str) -> bytes:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=35,
        )
    except subprocess.TimeoutExpired:
        raise AppError("TIMEOUT", "Instagram media preparation timed out") from None
    except FileNotFoundError:
        raise AppError(
            error_code, "Instagram media preparation requires ffmpeg and ffprobe"
        ) from None
    if result.returncode:
        raise AppError(error_code, "Instagram media could not be decoded", True)
    return result.stdout


def _jpeg(raw: Path, target: Path) -> None:
    with raw.open("rb") as image:
        header = image.read(12)
    if header.startswith(b"\xff\xd8\xff"):
        demuxer = "jpeg_pipe"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        demuxer = "png_pipe"
    elif header.startswith(b"RIFF") and header[8:] == b"WEBP":
        demuxer = "webp_pipe"
    else:
        raise AppError("UNSUPPORTED_SOURCE", "unsupported Instagram photo encoding", True)
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            demuxer,
            "-max_pixels",
            "50000000",
            "-i",
            str(raw),
            "-frames:v",
            "1",
            "-vf",
            "scale=w='min(1600,iw)':h='min(1600,ih)':force_original_aspect_ratio=decrease",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "3",
            str(target),
        ],
        "VISUAL_FAILED",
    )


def _probe(path: Path, limit: int) -> tuple[int, set[str]]:
    output = _run(
        [
            "ffprobe",
            "-v",
            "error",
            *_LOCAL_MOV,
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(path),
        ],
        "EXTRACTION_FAILED",
    )
    try:
        info = json.loads(output)
        duration = float(info["format"]["duration"])
        streams = {stream["codec_type"] for stream in info["streams"]}
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
    except (ValueError, KeyError, TypeError):
        raise AppError(
            "EXTRACTION_FAILED", "Instagram media duration is unavailable", True
        ) from None
    if duration > limit:
        raise AppError("TOO_LARGE", "Instagram video exceeds the duration limit", True)
    return math.ceil(duration), streams


def _error(exc: AppError) -> dict:
    return {"code": exc.code, "permanent": exc.permanent}


def _prepare_audio(path: Path, target: Path, limit: int) -> None:
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            *_LOCAL_MOV,
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "24k",
            "-fs",
            str(limit + 65_536),
            str(target),
        ],
        "TRANSCRIPTION_FAILED",
    )
    if target.stat().st_size > limit:
        raise AppError("TOO_LARGE", "Instagram audio exceeds the byte limit", True)


def _video_frames(video: Path, directory: Path, request: dict, duration: int) -> list[Path]:
    scale = "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease"

    def run(argv: list[str]) -> int:
        index = argv.index("-i")
        # Frame selection stays shared; restrict the new input to a local MP4 and bound pixels.
        argv[index:index] = [*_LOCAL_MOV, "-max_pixels", "12000000"]
        vf = argv.index("-vf") + 1
        argv[vf] += "," + scale
        _run(argv, "VISUAL_FAILED")
        return 0

    frames = extract_representative_frames(
        video,
        directory / "frames",
        duration_seconds=duration,
        interval_seconds=min(request["frame_interval"], duration),
        max_frames=request["max_frames"],
        scene_threshold=request["scene_threshold"],
        runner=run,
    )
    if frames:
        return frames

    # A short clip can have no periodic samples or scene changes.
    first = directory / "first.jpg"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            *_LOCAL_MOV,
            "-max_pixels",
            "12000000",
            "-i",
            str(video),
            "-vf",
            scale,
            "-frames:v",
            "1",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "3",
            str(first),
        ],
        "VISUAL_FAILED",
    )
    return [first] if first.is_file() else []


def prepare_post_media(request: dict) -> dict:
    """Materialize only the local files needed by STT and Vision for this element."""
    element = request["element"]
    url = element.get("url")
    directory = Path(request["download_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    if element["kind"] == "photo":
        if not url:
            raise AppError("UNSUPPORTED_SOURCE", "Instagram photo URL is unavailable", True)
        raw, jpeg = directory / "photo", directory / "photo.jpg"
        asyncio.run(_fetch(url, raw, _PHOTO_BYTES, _IMAGE_TYPES))
        _jpeg(raw, jpeg)
        return {"images": [jpeg.name]}
    if element["kind"] != "video":
        raise AppError("UNSUPPORTED_SOURCE", "unknown Instagram post media", True)

    max_duration = request["max_duration"]
    if element.get("duration") is not None and element["duration"] > max_duration:
        raise AppError("TOO_LARGE", "Instagram video exceeds the duration limit", True)
    result: dict = {"images": []}
    audio_url = element.get("audio_url")
    if not url and not audio_url:
        raise AppError("UNSUPPORTED_SOURCE", "Instagram video formats are unavailable", True)
    video = directory / "video.mp4"
    duration, streams = None, set()
    if url and (request["visual"] or (request["audio"] and not audio_url)):
        try:
            asyncio.run(_fetch(url, video, request["video_bytes"], _VIDEO_TYPES))
            duration, streams = _probe(video, max_duration)
        except AppError as exc:
            if not audio_url or exc.code in {"TOO_LARGE", "SECURITY_REJECTED"}:
                raise
            result["visual_error"] = _error(exc)

    if request["audio"]:
        try:
            audio_source = video
            if "audio" not in streams and audio_url:
                audio_source = directory / "track.mp4"
                asyncio.run(_fetch(audio_url, audio_source, request["audio_bytes"], _VIDEO_TYPES))
                audio_duration, audio_streams = _probe(audio_source, max_duration)
                duration = duration or audio_duration
            else:
                audio_streams = streams
            if "audio" not in audio_streams:
                raise AppError("NO_AUDIO_TRACK", "Instagram video has no audio", True)
            audio = directory / "audio.mp3"
            _prepare_audio(audio_source, audio, request["audio_bytes"])
            result["audio"] = audio.name
        except AppError as exc:
            result["audio_error"] = _error(exc)
    if request["visual"] and "visual_error" not in result:
        try:
            if "video" not in streams:
                raise AppError("VISUAL_FAILED", "Instagram video stream is unavailable", True)
            frames = _video_frames(video, directory, request, duration)
            if not frames:
                raise AppError("VISUAL_FAILED", "Instagram video contains no usable frames", True)
            result["images"] = [str(frame.relative_to(directory)) for frame in frames]
        except AppError as exc:
            result["visual_error"] = _error(exc)
    result["duration"] = duration
    return result
