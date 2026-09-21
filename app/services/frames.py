"""Извлечение representative frames из видео через ffmpeg.

PRODUCT_SPEC §23 требует периодическую выборку + scene/key frames + approximate
dedup. ffmpeg закрывает CV-часть своими select/mpdecimate фильтрами; Python
оставляет только exact hash safety-net и лимит результата.
"""

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.errors import AppError


def _dedup(frames: list[Path]) -> list[Path]:
    """Точная дедупликация одинаковых кадров (статичные слайды дают идентичные
    байты при одинаковых настройках). Порядок сохраняется."""
    unique: list[Path] = []
    seen: set[str] = set()
    for frame in frames:
        digest = hashlib.sha1(frame.read_bytes()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique.append(frame)
    return unique


def extract_representative_frames(
    video: Path,
    work_dir: Path,
    *,
    interval_seconds: int = 20,
    max_frames: int = 120,
    scene_threshold: float = 0.35,
    timeout_seconds: float = 300.0,
    runner: Callable[[list[str]], int] | None = None,
) -> list[Path]:
    """ffmpeg выбирает periodic + scene/key frames и приблизительно дедуплицирует.

    runner — инъекция для тестов (по умолчанию subprocess.run, без shell).
    Возвращает максимум max_frames путей; порядок ffmpeg сохраняется.
    """

    def _default_runner(argv: list[str]) -> int:
        result = subprocess.run(argv, capture_output=True, timeout=timeout_seconds)
        return result.returncode

    run = runner or _default_runner
    work_dir.mkdir(parents=True, exist_ok=True)
    pattern = work_dir / "frame_%04d.jpg"
    select = (
        "select="
        "isnan(prev_selected_t)"
        rf"+gte(t-prev_selected_t\,{interval_seconds})"
        rf"+gt(scene\,{scene_threshold})"
        r"+eq(pict_type\,I)"
    )
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        f"{select},mpdecimate",
        "-vsync",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(pattern),
    ]
    returncode = run(argv)
    if returncode != 0:
        raise AppError("VISUAL_FAILED", f"ffmpeg frame extraction failed: {returncode}")
    frames = sorted(work_dir.glob("frame_*.jpg"))
    unique = _dedup(frames)
    return unique[:max_frames]
