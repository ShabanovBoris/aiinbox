"""Execute one YouTube yt-dlp download inside its killable process boundary."""

import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yt_dlp

from app.errors import AppError


def _size_limit_hook(byte_limit: int):
    """Stop yt-dlp as soon as a YouTube media transfer crosses its caller's byte cap."""

    def enforce_limit(status: dict) -> None:
        if status.get("status") != "downloading":
            return
        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if downloaded > byte_limit or (total is not None and total > byte_limit):
            raise yt_dlp.utils.DownloadError("Configured media size limit exceeded")

    return enforce_limit


def _probe_media_streams(path: Path) -> frozenset[str]:
    """Reject yt-dlp components that lack the audio/video tracks required by the caller."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise AppError(
            "DOWNLOAD_FAILED", "ffprobe is required to verify downloaded YouTube media"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("TIMEOUT", "YouTube output stream probe timed out") from exc
    if result.returncode != 0:
        return frozenset()
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return frozenset()
    streams = metadata.get("streams") if isinstance(metadata, dict) else None
    if not isinstance(streams, list):
        return frozenset()
    return frozenset(
        stream["codec_type"]
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") in {"audio", "video"}
    )


def execute(request: dict[str, Any]) -> dict[str, str]:
    """Download one bounded media file; the parent remains its lifecycle owner."""
    options = request["options"]
    download_dir = Path(request["download_dir"])
    byte_limit = int(request["byte_limit"])
    required_streams = request.get("required_streams")
    if required_streams is not None and (
        not isinstance(required_streams, list)
        or not required_streams
        or not set(required_streams) <= {"audio", "video"}
    ):
        raise AppError("DOWNLOAD_FAILED", "YouTube worker received invalid stream requirements")
    required = frozenset(required_streams or ())
    options["progress_hooks"] = [_size_limit_hook(byte_limit)]
    download_dir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(request["url"], download=True)
        if not isinstance(info, dict):
            raise AppError("DOWNLOAD_FAILED", "yt-dlp returned no video data")
        output_paths = [info.get("filepath"), ydl.prepare_filename(info)]

    candidates = []
    for raw_path in (*output_paths, *sorted(download_dir.iterdir())):
        if isinstance(raw_path, str) and raw_path:
            path = Path(raw_path)
        elif isinstance(raw_path, Path):
            path = raw_path
        else:
            continue
        if path not in candidates:
            candidates.append(path)

    prepared_path = None
    for path in candidates:
        if (
            path.name.lower().endswith((".part", ".ytdl", ".temp"))
            or path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != download_dir.resolve()
        ):
            continue
        if required and not required.issubset(_probe_media_streams(path)):
            continue
        prepared_path = path
        break

    if prepared_path is None:
        # ❌ Удален выбор первого glob-совпадения: каждый fallback-кандидат
        # проверяется ffprobe на требуемые аудио/видеодорожки.
        raise AppError(
            "DOWNLOAD_FAILED", "no completed YouTube output has the required media streams"
        )
    if prepared_path.stat().st_size > byte_limit:
        raise AppError("TOO_LARGE", "download exceeds its configured byte limit", True)
    return {"path": str(prepared_path)}


def main() -> None:
    """Keep yt-dlp output off stdout so the parent can parse a small JSON result."""
    try:
        request = json.load(sys.stdin)
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
        message = str(exc)
        lowered_message = message.casefold()
        if any(
            marker in lowered_message
            for marker in (
                "configured media size limit exceeded",
                "max-filesize",
                "larger than the maximum file size",
            )
        ):
            response = {
                "ok": False,
                "kind": "app",
                "code": "TOO_LARGE",
                "message": "YouTube media exceeds its configured byte limit",
                "permanent": True,
            }
        else:
            response = {"ok": False, "kind": "yt_dlp", "message": message[:500]}
    except OSError:
        response = {"ok": False, "kind": "os"}
    except Exception as exc:
        response = {"ok": False, "kind": "internal", "message": type(exc).__name__}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
