from typing import Protocol

from app.domain.models import AnalysisResult, NormalizedContent, UserProfile


class LlmError(Exception):
    """Ошибка на границе LLM-адаптера с машиночитаемым кодом
    (PRODUCT_SPEC §57: LLM_FAILED / INVALID_LLM_OUTPUT)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
