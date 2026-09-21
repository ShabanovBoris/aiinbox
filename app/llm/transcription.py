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

    async def transcribe(self, audio_path: Path) -> str:
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
