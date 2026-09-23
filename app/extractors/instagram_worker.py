"""Run one yt-dlp operation in a process the async owner can terminate."""

import json
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yt_dlp

from app.errors import AppError

_DESCRIPTION_LIMIT = 2_000


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


def execute(request: dict) -> dict:
    """Execute the serializable metadata/download request using yt-dlp's Python API."""
    mode = request["mode"]
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
