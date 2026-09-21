import asyncio
import subprocess
import tempfile
from pathlib import Path

from openai import APITimeoutError, AsyncOpenAI

from app.errors import AppError


class OpenAiTranscriptionProvider:
    """OpenAI-compatible STT adapter: отдельный от analysis API/модель."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
        base_url: str | None = None,
    ):
        if not model:
            raise ValueError("OPENAI_TRANSCRIPTION_MODEL is not configured")
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, base_url=base_url)
        self._model = model

    async def transcribe(self, audio_path: Path, *, duration_seconds: int | None = None) -> str:
        # duration_seconds нужен provider-specific subclasses; OpenAI multipart
        # сохраняет прежний single-request contract.
        try:
            with audio_path.open("rb") as audio_file:
                response = await self._client.audio.transcriptions.create(
                    model=self._model, file=audio_file
                )
        except APITimeoutError as exc:
            raise AppError("TIMEOUT", f"transcription timed out: {exc}") from exc
        except Exception as exc:  # граница адаптера: SDK-ошибки → код приложения
            raise AppError("TRANSCRIPTION_FAILED", f"transcription failed: {exc}") from exc
        return (response.text or "").strip()


class OpenRouterTranscriptionProvider(OpenAiTranscriptionProvider):
    """OpenRouter STT transport с bounded segmentation для multipart endpoint.

    OpenRouter принимает OpenAI-style multipart только до 25 MB и рекомендует
    сегментировать длинное аудио из-за upstream processing timeout. Analysis/STT
    SDK остаётся общим, а provider-specific transport constraint живёт здесь.
    """

    _MAX_MULTIPART_BYTES = 25_000_000
    _SEGMENT_SECONDS = 300

    async def transcribe(self, audio_path: Path, *, duration_seconds: int | None = None) -> str:
        if audio_path.stat().st_size <= self._MAX_MULTIPART_BYTES and (
            duration_seconds is None or duration_seconds <= self._SEGMENT_SECONDS
        ):
            return await super().transcribe(audio_path)

        with tempfile.TemporaryDirectory(prefix="openrouter-stt-", dir=audio_path.parent) as temp:
            segment_dir = Path(temp)
            segments = await asyncio.to_thread(
                _split_audio_for_openrouter,
                audio_path,
                segment_dir,
                self._SEGMENT_SECONDS,
            )
            texts: list[str] = []
            for segment in segments:
                if segment.stat().st_size > self._MAX_MULTIPART_BYTES:
                    raise AppError(
                        "TRANSCRIPTION_FAILED",
                        "OpenRouter transcription segment exceeds multipart limit",
                    )
                text = await super().transcribe(segment)
                if text:
                    texts.append(text)
            return "\n".join(texts)


def _split_audio_for_openrouter(
    audio_path: Path,
    output_dir: Path,
    segment_seconds: int,
) -> list[Path]:
    """Re-encode long audio into small AAC chunks for OpenRouter STT requests."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "segment_%04d.aac"
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=300)
    except FileNotFoundError as exc:
        raise AppError(
            "TRANSCRIPTION_FAILED", "ffmpeg is required for long OpenRouter STT"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("TIMEOUT", "OpenRouter audio segmentation timed out") from exc
    if result.returncode != 0:
        raise AppError(
            "TRANSCRIPTION_FAILED",
            f"ffmpeg audio segmentation failed: {result.returncode}",
        )
    segments = sorted(output_dir.glob("segment_*.aac"))
    if not segments:
        raise AppError("TRANSCRIPTION_FAILED", "ffmpeg produced no transcription segments")
    return segments
