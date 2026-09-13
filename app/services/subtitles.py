"""Разбор субтитров VTT/SRT → плоский текст транскрипта.

Никакой сложной transcript-базы: timestamps при наличии сохраняются в metadata
(contents.metadata_json), в текст идут только реплики с дедупликацией подряд.
"""

import re

_TIMESTAMP = re.compile(
    r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}"
)
_CUE_ID = re.compile(r"^\d+$")
_TAGS = re.compile(r"<[^>]+>")


def parse_subtitles(text: str) -> tuple[str, list[tuple[float, float]]]:
    """VTT/SRT → (текст реплик, [(start, end)] timestamps в секундах)."""
    lines_out: list[str] = []
    cues: list[tuple[float, float]] = []
    previous: str | None = None
    for raw_line in text.splitlines():
        line = _TAGS.sub("", raw_line).strip()
        if not line:
            continue
        match = _TIMESTAMP.match(raw_line)
        if match:
            start_raw, end_raw = raw_line.strip().split("-->")
            cues.append((_parse_seconds(start_raw), _parse_seconds(end_raw)))
            continue
        if line.upper().startswith(("WEBVTT", "NOTE", "KIND:", "LANGUAGE:")):
            continue
        if _CUE_ID.match(line):
            continue
        if line == previous:
            continue  # дубликат реплики в перекрывающихся дорожках
        previous = line
        lines_out.append(line)
    return "\n".join(lines_out), cues


def _parse_seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds
