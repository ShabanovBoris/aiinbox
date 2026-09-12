from typing import Protocol

from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.errors import AppError


class LlmError(AppError):
    """Ошибка на границе LLM-адаптера (LLM_FAILED / INVALID_LLM_OUTPUT)."""


class LlmProvider(Protocol):
    """Граница сменного LLM-провайдера. SDK (OpenAI/Ollama) живёт только в adapter.

    Расширение контракта (summarize/transcribe/vision) — по мере фаз, не заранее.
    """

    async def analyze(
        self,
        content: NormalizedContent,
        profile: UserProfile,
        categories: list[str],
    ) -> AnalysisResult: ...
