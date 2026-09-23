"""Извлечение representative frames из видео через ffmpeg.

PRODUCT_SPEC §23 требует периодическую выборку + scene/key frames + approximate
dedup. Periodic baseline и scene candidates извлекаются раздельно: лимит
применяется только после полного прохода по timeline, поэтому частые GOP/I-frames
не могут съесть весь budget в начале длинного видео.
"""

import hashlib
import math
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


def _evenly_spaced(frames: list[Path], limit: int) -> list[Path]:
    """Bound a chronological list while retaining coverage through its tail."""
    if limit <= 0:
        return []
    if len(frames) <= limit:
        return frames
    if limit == 1:
        return [frames[0]]
    last = len(frames) - 1
    return [frames[round(index * last / (limit - 1))] for index in range(limit)]


def extract_representative_frames(
    video: Path,
    work_dir: Path,
    *,
    interval_seconds: int = 20,
    max_frames: int = 120,
    scene_threshold: float = 0.35,
    duration_seconds: int | None = None,
    timeout_seconds: float = 300.0,
    runner: Callable[[list[str]], int] | None = None,
) -> list[Path]:
    """Extract bounded full-timeline baseline plus sparse scene candidates.

    runner — инъекция для тестов (по умолчанию subprocess.run, без shell).
    Known duration widens sampling intervals before ffmpeg runs, so each pass
    materializes at most ``max_frames`` JPEGs without spending the budget only
    near the beginning of a long video.
    """

    def _default_runner(argv: list[str]) -> int:
        result = subprocess.run(argv, capture_output=True, timeout=timeout_seconds)
        return result.returncode

    if max_frames <= 0:
        return []

    run = runner or _default_runner
    work_dir.mkdir(parents=True, exist_ok=True)
    periodic_interval = max(1, interval_seconds)
    scene_min_gap: int | None = None
    if duration_seconds and duration_seconds > 0:
        bounded_interval = max(1, math.ceil(duration_seconds / max_frames))
        periodic_interval = max(periodic_interval, bounded_interval)
        scene_min_gap = bounded_interval

    periodic_pattern = work_dir / "periodic_%05d.jpg"
    periodic_argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        f"fps=1/{periodic_interval},mpdecimate",
        "-fps_mode",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-pix_fmt",
        "yuvj420p",
        "-q:v",
        "2",
        str(periodic_pattern),
    ]
    periodic_returncode = run(periodic_argv)
    if periodic_returncode != 0:
        raise AppError(
            "VISUAL_FAILED", f"ffmpeg periodic frame extraction failed: {periodic_returncode}"
        )

    scene_pattern = work_dir / "scene_%05d.jpg"
    scene_filter = rf"select=gt(scene\,{scene_threshold})"
    if scene_min_gap is not None:
        # Scene changes are optional enrichment. Spacing them across the known
        # duration prevents frequent cuts/keyframes from front-loading this pass.
        scene_filter += rf"*if(isnan(prev_selected_t)\,1\,gte(t-prev_selected_t\,{scene_min_gap}))"
    scene_filter += ",mpdecimate"
    scene_argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        scene_filter,
        "-fps_mode",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-pix_fmt",
        "yuvj420p",
        "-q:v",
        "2",
        str(scene_pattern),
    ]
    scene_returncode = run(scene_argv)
    if scene_returncode != 0:
        raise AppError("VISUAL_FAILED", f"ffmpeg scene extraction failed: {scene_returncode}")

    periodic = _dedup(sorted(work_dir.glob("periodic_*.jpg")))
    periodic_hashes = {hashlib.sha1(frame.read_bytes()).hexdigest() for frame in periodic}
    scenes = [
        frame
        for frame in _dedup(sorted(work_dir.glob("scene_*.jpg")))
        if hashlib.sha1(frame.read_bytes()).hexdigest() not in periodic_hashes
    ]
    if not periodic:
        return _evenly_spaced(scenes, max_frames)
    if not scenes or max_frames == 1:
        return _evenly_spaced(periodic, max_frames)

    # Keep periodic coverage dominant, but reserve up to one third of the vision
    # budget for semantic scene changes even when a long video has >max baseline frames.
    scene_quota = min(len(scenes), max(1, max_frames // 3))
    periodic_quota = min(len(periodic), max_frames - scene_quota)
    scene_quota = min(len(scenes), max_frames - periodic_quota)
    return _evenly_spaced(periodic, periodic_quota) + _evenly_spaced(scenes, scene_quota)
