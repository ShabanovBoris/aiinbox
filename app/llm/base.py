from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.domain.models import (
    AnalysisResult,
    AttentionHookGeneration,
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


@dataclass(frozen=True)
class TranscriptionSegmentCheckpoint:
    """Durable identity for one provider-specific STT segment result."""

    text: str
    input_sha256: str
    provider: str
    model: str
    segment_seconds: int
    format_version: str

    def matches_input(
        self,
        *,
        input_sha256: str,
        provider: str,
        model: str,
        segment_seconds: int,
        format_version: str,
    ) -> bool:
        return (
            self.input_sha256 == input_sha256
            and self.provider == provider
            and self.model == model
            and self.segment_seconds == segment_seconds
            and self.format_version == format_version
        )


@dataclass(frozen=True)
class AttentionHookGenerationResult:
    """Provider output plus explicit generation identity for durable hook metadata."""

    generation: AttentionHookGeneration
    provider: str
    model: str


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

    async def describe_images(
        self, images: list[Path], context: str | None, *, preferred_language: str
    ) -> str:
        """Компактное описание визуального контента кадров (ТЗ §23)."""
        ...

    async def profile_update(self, instruction: str, current: UserProfile) -> ProfilePatch:
        """Natural language → валидированный ProfilePatch (Phase 8)."""
        ...

    async def generate_attention_hooks(
        self, source_context: str, *, preferred_language: str
    ) -> AttentionHookGenerationResult:
        """Create source-grounded presentation hooks without exposing provider SDK types."""
        ...


class TranscriptionProvider(Protocol):
    """Отдельная граница транскрипции: whisper-эндпоинт OpenAI — другой API и
    другая модель, отдельный adapter уменьшает coupling анализа и STT."""

    async def transcribe(
        self,
        audio_path: Path,
        *,
        duration_seconds: int | None = None,
        completed_segments: Mapping[int, TranscriptionSegmentCheckpoint] | None = None,
        on_segment: Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]] | None = None,
    ) -> str:
        """Аудио → текст с optional durable checkpoints provider-specific batching."""
        ...
