import asyncio
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
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

    async def transcribe(
        self,
        audio_path: Path,
        *,
        duration_seconds: int | None = None,
        completed_segments: Mapping[int, str] | None = None,
        on_segment: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> str:
        # duration_seconds нужен provider-specific subclasses; OpenAI multipart
        # сохраняет прежний single-request contract. Checkpoint args нужны
        # segmented providers и намеренно игнорируются single-request transport.
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
    _MAX_SEGMENT_CONCURRENCY = 4

    async def transcribe(
        self,
        audio_path: Path,
        *,
        duration_seconds: int | None = None,
        completed_segments: Mapping[int, str] | None = None,
        on_segment: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> str:
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
            for segment in segments:
                if segment.stat().st_size > self._MAX_MULTIPART_BYTES:
                    raise AppError(
                        "TRANSCRIPTION_FAILED",
                        "OpenRouter transcription segment exceeds multipart limit",
                    )

            cached = dict(completed_segments or {})
            semaphore = asyncio.Semaphore(self._MAX_SEGMENT_CONCURRENCY)

            async def transcribe_segment(index: int, segment: Path) -> str:
                if index in cached:
                    return cached[index]
                async with semaphore:
                    text = await super(OpenRouterTranscriptionProvider, self).transcribe(segment)
                # Callback is provider-agnostic: the application decides whether
                # this checkpoint goes to SQLite, memory, or nowhere.
                if on_segment is not None:
                    await on_segment(index, text)
                return text

            # All segment results are awaited even when one fails, so successful
            # siblings can become durable checkpoints before bounded retry.
            results = await asyncio.gather(
                *(transcribe_segment(index, segment) for index, segment in enumerate(segments)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return "\n".join(text for text in results if isinstance(text, str) and text)


def _split_audio_for_openrouter(
    audio_path: Path,
    output_dir: Path,
    segment_seconds: int,
    runner: Callable[[list[str]], subprocess.CompletedProcess[bytes]] | None = None,
) -> list[Path]:
    """Re-encode long audio into broadly supported WAV chunks for OpenRouter STT.

    runner оставляет subprocess boundary тестируемой без требования ffmpeg в
    unit-test окружении; production path по умолчанию использует subprocess.run.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "segment_%04d.wav"
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
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    try:
        result = (
            runner(argv)
            if runner is not None
            else subprocess.run(argv, capture_output=True, timeout=300)
        )
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
    segments = sorted(output_dir.glob("segment_*.wav"))
    if not segments:
        raise AppError("TRANSCRIPTION_FAILED", "ffmpeg produced no transcription segments")
    return segments
