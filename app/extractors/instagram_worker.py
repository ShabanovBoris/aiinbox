"""Run one yt-dlp operation in a process the async owner can terminate."""

import json
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request

from app.errors import AppError
from app.services.url_parsing import instagram_post_shortcode

_DESCRIPTION_LIMIT = 2_000
MAX_POST_ELEMENTS = 20


def _bounded_text(value: Any, limit: int) -> str | None:
    """Cap untrusted provider text before it crosses the child-process boundary."""
    return value[:limit] if isinstance(value, str) else None


def _compact_info(info: Any) -> dict:
    """Return only bounded metadata needed by the parent extractor."""
    if not isinstance(info, dict):
        return {}

    result: dict[str, Any] = {}
    identifier = info.get("id")
    if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
        result["id"] = str(identifier)[:128] if isinstance(identifier, str) else identifier
    for name, limit in (
        ("title", 300),
        ("description", _DESCRIPTION_LIMIT),
        ("caption", _DESCRIPTION_LIMIT),
        ("uploader", 200),
        ("creator", 200),
        ("uploader_id", 200),
        ("channel_id", 200),
        ("webpage_url", 2_048),
    ):
        value = _bounded_text(info.get(name), limit)
        if value is not None:
            result[name] = value

    duration = info.get("duration")
    if isinstance(duration, (int, float)) and math.isfinite(duration):
        result["duration"] = duration

    formats = info.get("formats")
    result["_all_formats_no_audio"] = bool(
        isinstance(formats, list)
        and formats
        and all(
            isinstance(media_format, dict) and media_format.get("acodec") == "none"
            for media_format in formats
        )
    )
    if "entries" in info:
        result["entries"] = True
    if isinstance(info.get("_type"), str):
        result["_type"] = info["_type"][:32]
    return result


def size_limit_hook(byte_limit: int):
    """Share the transfer cap between the isolated worker and injected test adapters."""

    def enforce_limit(status: dict) -> None:
        if status.get("status") != "downloading":
            return
        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if downloaded > byte_limit or (total is not None and total > byte_limit):
            raise yt_dlp.utils.DownloadError("Configured media size limit exceeded")

    return enforce_limit


def _with_post_query(request: Request, source_url: str) -> Request:
    """Restore the submitted query only on yt-dlp's GET of the same public post."""
    shortcode = instagram_post_shortcode(source_url)
    if not shortcode or request.method != "GET" or request.data is not None:
        return request
    destination = urlsplit(request.url)
    query = urlsplit(source_url).query
    if (
        query
        and not destination.query
        and destination.netloc == "www.instagram.com"
        and instagram_post_shortcode(request.url) == shortcode
    ):
        request = request.copy()
        request.url = destination._replace(query=query).geturl()
    return request


def post_info(url: str, options: dict, ydl_factory=None) -> dict:
    """Read the ordered raw media manifest without resolving a playlist or its URLs."""
    if not instagram_post_shortcode(url):
        raise AppError("UNSUPPORTED_SOURCE", "invalid Instagram post URL", True)
    if ydl_factory is None:

        class PostYoutubeDL(yt_dlp.YoutubeDL):
            def urlopen(self, req):
                if isinstance(req, Request):
                    req = _with_post_query(req, url)
                return super().urlopen(req)

        ydl_factory = PostYoutubeDL

    with ydl_factory({**options, "ignore_no_formats_error": True}) as ydl:
        info = ydl.extract_info(url, download=False, process=False, ie_key="Instagram")
    if not isinstance(info, dict):
        raise AppError("EXTRACTION_FAILED", "Instagram post metadata is unavailable")
    metadata = _compact_info(info)
    if not metadata.get("id"):
        raise AppError("EXTRACTION_FAILED", "Instagram post identity is unavailable")
    entries = info.get("entries", [info])
    if not isinstance(entries, list) or not entries:
        raise AppError("EXTRACTION_FAILED", "Instagram post has no media manifest")
    return {
        "metadata": metadata,
        "total": len(entries),
        "elements": [_compact_element(entry) for entry in entries[:MAX_POST_ELEMENTS]],
    }


def _compact_element(entry: Any) -> dict:
    from app.extractors.instagram_post_media import is_instagram_cdn_url

    if not isinstance(entry, dict):
        return {"id": "", "kind": "unknown"}
    compact = _compact_info(entry)
    if not compact.get("id"):
        return {"id": "", "kind": "unknown"}
    result = {"id": compact.get("id", ""), "duration": compact.get("duration")}

    def media_url(value: Any) -> str | None:
        return value if isinstance(value, str) and is_instagram_cdn_url(value) else None

    formats = entry.get("formats")
    if formats or result["duration"]:
        result["kind"] = "video"
        if not isinstance(formats, list):
            return result
        candidates = [
            fmt
            for fmt in formats
            if isinstance(fmt, dict)
            and fmt.get("protocol") in (None, "https")
            and not fmt.get("fragments")
            and not fmt.get("manifest_url")
            and media_url(fmt.get("url"))
        ]
        videos = [fmt for fmt in candidates if fmt.get("vcodec") != "none"]
        if videos:

            def quality(fmt):
                height = fmt.get("height")
                height = height if type(height) is int and height > 0 else 0
                return (
                    fmt.get("acodec") != "none",
                    height <= 720,
                    height if height <= 720 else -height,
                )

            result["url"] = max(videos, key=quality)["url"]
        audio = next(
            (
                fmt
                for fmt in reversed(candidates)
                if fmt.get("vcodec") == "none" and fmt.get("acodec") != "none"
            ),
            None,
        )
        if audio:
            result["audio_url"] = audio["url"]
        return result

    result["kind"] = "photo"
    images = entry.get("thumbnails")
    if isinstance(images, list):
        result["url"] = next(
            (
                image["url"]
                for image in reversed(images)
                if isinstance(image, dict) and media_url(image.get("url"))
            ),
            None,
        )
    return result


def execute(request: dict) -> dict:
    """Execute the serializable metadata/download request using yt-dlp's Python API."""
    mode = request["mode"]
    if mode == "post_info":
        return post_info(request["url"], request["options"])
    if mode == "post_media":
        from app.extractors.instagram_post_media import prepare_post_media

        return prepare_post_media(request)
    options = request["options"]
    if mode == "download":
        # yt-dlp may silently return on an oversized Content-Length before progress hooks run.
        options.pop("max_filesize", None)
        options["progress_hooks"] = [size_limit_hook(int(request["byte_limit"]))]
    if mode not in {"info", "download"}:
        raise AppError("EXTRACTION_FAILED", "Invalid Instagram extraction operation", True)
    if mode == "download":
        Path(request["download_dir"]).mkdir(parents=True, exist_ok=True)

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(request["url"], download=mode == "download")
        result = {"info": _compact_info(info)}
        if mode == "download" and isinstance(info, dict):
            result["prepared_path"] = ydl.prepare_filename(info)
        return result


def main() -> None:
    """Handle one parent request and emit a compact, bounded JSON response."""
    try:
        request = json.load(sys.stdin)
        # stdout is the worker's machine-readable protocol; keep provider output
        # away from it so the parent can decode exactly one JSON response.
        with redirect_stdout(sys.stderr):
            response = {"ok": True, "result": execute(request)}
    except AppError as exc:
        response = {
            "ok": False,
            "kind": "app",
            "code": exc.code,
            "message": str(exc)[:500],
            "permanent": exc.permanent,
        }
    except yt_dlp.utils.YoutubeDLError as exc:
        response = {"ok": False, "kind": "yt_dlp", "message": str(exc)[:500]}
    except OSError as exc:
        response = {"ok": False, "kind": "os", "message": str(exc)[:500]}
    except Exception as exc:
        response = {"ok": False, "kind": "internal", "message": type(exc).__name__}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
