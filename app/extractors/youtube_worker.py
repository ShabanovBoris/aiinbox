"""Execute one YouTube yt-dlp download inside its killable process boundary."""

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yt_dlp

from app.errors import AppError


def execute(request: dict[str, Any]) -> dict[str, str]:
    """Download one bounded media file; the parent remains its lifecycle owner."""
    options = request["options"]
    download_dir = Path(request["download_dir"])
    download_dir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(request["url"], download=True)
        if not isinstance(info, dict):
            raise AppError("DOWNLOAD_FAILED", "yt-dlp returned no video data")
        prepared_path = Path(ydl.prepare_filename(info))
    if not prepared_path.exists():
        candidates = list(prepared_path.parent.glob(prepared_path.stem + ".*"))
        if not candidates:
            raise AppError("DOWNLOAD_FAILED", "video file missing after download")
        prepared_path = candidates[0]
    if (
        prepared_path.is_symlink()
        or not prepared_path.is_file()
        or prepared_path.resolve().parent != download_dir.resolve()
    ):
        raise AppError("DOWNLOAD_FAILED", "YouTube output escaped its temporary directory")
    if prepared_path.stat().st_size > int(request["byte_limit"]):
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
        response = {"ok": False, "kind": "yt_dlp", "message": str(exc)[:500]}
    except OSError:
        response = {"ok": False, "kind": "os"}
    except Exception as exc:
        response = {"ok": False, "kind": "internal", "message": type(exc).__name__}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
