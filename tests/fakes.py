"""Детерминированные фейки для тестов: только тестовое окружение, не production path."""

from pathlib import Path

from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.errors import AppError
from app.llm.base import LlmError


def make_analysis(**overrides) -> AnalysisResult:
    base = dict(
        title="Архитектура AI-агентов",
        summary="Разбор подходов к оркестрации агентов.",
        category="AI",
        item_type=ItemType.LEARN,
        tags=["ai", "agents"],
        importance=0.8,
        urgency=0.4,
        goal_fit=0.9,
        long_term_value=0.7,
        interest_fit=0.8,
        estimated_action_minutes=25,
        next_action="Посмотреть блок про tool orchestration",
        suggested_due_at=None,
        priority_reason="Сильно связано с профессиональными целями",
        language="ru",
        confidence=0.9,
    )
    base.update(overrides)
    return AnalysisResult(**base)


class FakeLlmProvider:
    """Подменяет LlmProvider: детерминированный результат или заданная ошибка."""

    def __init__(
        self,
        result: AnalysisResult | None = None,
        error: LlmError | None = None,
        vision: bool = False,
        describe_notes: str | None = None,
        describe_fail: bool = False,
    ):
        from app.llm.base import LlmCapabilities

        self.result = result or make_analysis()
        self.error = error
        self.calls: list[tuple[NormalizedContent, UserProfile, list[str]]] = []
        self.capabilities = LlmCapabilities(structured_output=True, vision=vision)
        self.describe_notes = describe_notes or "На слайдах диаграмма оркестрации."
        self.describe_fail = describe_fail
        self.describe_calls = 0

    async def analyze(
        self, content: NormalizedContent, profile: UserProfile, categories: list[str]
    ):
        self.calls.append((content, profile, list(categories)))
        if self.error is not None:
            raise self.error
        return self.result

    async def describe_images(self, images, context):
        self.describe_calls += 1
        if self.describe_fail:
            raise LlmError("VISUAL_FAILED", "vision down")
        return self.describe_notes


class FakePipeline:
    """Минимальный пайплайн для тестов claim/failure воркера (без LLM)."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def run(self, session, item) -> None:
        if self.fail:
            raise RuntimeError("boom")
        item.title = "stub"
        item.processing_stage = "READY"
        item.processing_status = ProcessingStatus.READY
        await session.commit()


def make_content(text: str = "текст") -> NormalizedContent:
    return NormalizedContent(source_type=SourceType.TEXT, text=text)


def invalid_analysis_json() -> str:
    return json_invalid()


def json_invalid() -> str:
    import json

    # Нарушает схему: score вне диапазона, неизвестный item_type
    return json.dumps({"title": "x", "summary": "y", "goal_fit": 5.0, "item_type": "NOPE"})


class FakeDownloader:
    """Пишет фейковое аудио в temp dir; счётчик для проверки повторов."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def download(self, file_id: str, dest_dir: Path):
        self.calls += 1
        if self.fail:
            raise AppError("DOWNLOAD_FAILED", "telegram download failed")
        dest = Path(dest_dir) / f"{file_id}.ogg"
        dest.write_bytes(b"fake-ogg-bytes")
        return dest


class FakeTranscriber:
    def __init__(self, transcript: str = "Голосовая заметка: изучить агентов", fail: bool = False):
        self.transcript = transcript
        self.fail = fail
        self.calls = 0

    async def transcribe(self, audio_path: Path) -> str:
        self.calls += 1
        if self.fail:
            raise AppError("TRANSCRIPTION_FAILED", "stt failed")
        return self.transcript


def fake_frames_runner(frame_count: int = 3, duplicates: int = 0):
    """Инжектируемый runner для frames extraction: создаёт frame_count файлов
    (+duplicates идентичных копий первого) в целевой директории."""

    def run(argv):
        pattern = argv[-1]
        assert "frame_%04d.jpg" in pattern, f"unexpected pattern {pattern}"
        parent = Path(pattern).parent
        for i in range(1, frame_count + 1 + duplicates):
            path = parent / f"frame_{i:04d}.jpg"
            if 1 < i <= 1 + duplicates:
                path.write_bytes(b"same-as-first")
            elif i == 1:
                path.write_bytes(b"frame-one")
            else:
                path.write_bytes(f"frame-{i}".encode())
        return 0

    return run
