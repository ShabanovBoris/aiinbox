from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.domain.models import (
    AnalysisResult,
    NormalizedContent,
    ProfilePatch,
    UserProfile,
)
from app.errors import AppError


class LlmCapabilities(BaseModel):
    """Возможности провайдера (ТЗ §24): pipeline деградирует изящно, если
    vision недоступен — Item всё равно достигает READY по транскрипту."""

    structured_output: bool = True
    vision: bool = False


class LlmError(AppError):
    """Ошибка на границе LLM-адаптера (LLM_FAILED / INVALID_LLM_OUTPUT)."""


class LlmProvider(Protocol):
    """Граница сменного LLM-анализатора. SDK (OpenAI/Ollama) живёт только в adapter.

    Расширение контракта (summarize/transcribe/vision) — по мере фаз, не заранее.
    """

    capabilities: LlmCapabilities

    async def analyze(
        self,
        content: NormalizedContent,
        profile: UserProfile,
        categories: list[str],
    ) -> AnalysisResult: ...

    async def summarize_chunk(self, text: str) -> str:
        """Сжать один bounded fragment перед финальным анализом."""
        ...

    async def describe_images(self, images: list[Path], context: str | None) -> str:
        """Компактное описание визуального контента кадров (ТЗ §23)."""
        ...

    async def profile_update(self, instruction: str, current: UserProfile) -> ProfilePatch:
        """Natural language → валидированный ProfilePatch (Phase 8)."""
        ...


class TranscriptionProvider(Protocol):
    """Отдельная граница транскрипции: whisper-эндпоинт OpenAI — другой API и
    другая модель, отдельный adapter уменьшает coupling анализа и STT."""

    async def transcribe(self, audio_path: Path, *, duration_seconds: int | None = None) -> str:
        """Аудио-файл на диске → текст; duration помогает provider-specific batching."""
        ...
