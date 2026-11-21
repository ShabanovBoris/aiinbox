import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.enums import ProcessingStatus, SourceType
from app.domain.models import AnalysisResult, TopicClassificationResult
from app.ops import (
    analysis_quality_failures,
    backup_database,
    create_backup_generation,
    database_status,
    evaluate_analysis_quality,
    rebuild_search,
    restore_database,
    rotate_backups,
    smoke_external,
    sqlite_database_path,
    verify_backup_copy,
    verify_database,
)
from app.ops import main as ops_main
from app.storage.database import make_engine
from app.storage.models import Item, User
from tests.fakes import make_analysis


def test_sqlite_database_path_handles_container_absolute_path():
    """Operational tooling must resolve the same SQLite path syntax used by Docker."""
    assert sqlite_database_path("sqlite+aiosqlite:////data/app.db") == Path("/data/app.db")


async def test_application_sqlite_engine_uses_bounded_lock_wait(tmp_path):
    """The production engine waits briefly for another worker's SQLite write."""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'database.db'}",
    )
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA busy_timeout")
            assert result.scalar_one() == 30_000
    finally:
        await engine.dispose()


def test_backup_restore_roundtrip_includes_committed_wal(tmp_path):
    """Online backup must preserve committed data even while the source DB stays open."""
    source = tmp_path / "source.db"
    backup = tmp_path / "backups" / "snapshot.db"
    restored = tmp_path / "restored.db"

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        writer.execute("INSERT INTO notes(body) VALUES ('durable')")
        writer.commit()

        backup_database(source, backup)
        restore_database(backup, restored)
    finally:
        writer.close()

    verify_database(restored)
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("SELECT body FROM notes").fetchone() == ("durable",)
    finally:
        connection.close()


def test_restore_refuses_to_overwrite_existing_database(tmp_path):
    """Recovery stays reviewable by restoring only into a new target file."""
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    connection = sqlite3.connect(backup)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.commit()
    connection.close()
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        restore_database(backup, target)

    assert target.read_bytes() == b"existing"


def test_backup_generation_checksum_detects_transfer_corruption(tmp_path):
    """Off-host copies must fail before restore when transferred bytes changed."""
    source = tmp_path / "source.db"
    backup = tmp_path / "aiinbox-test.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.execute("INSERT INTO sample(value) VALUES ('durable')")
    connection.commit()
    connection.close()

    checksum = create_backup_generation(source, backup)
    verify_backup_copy(backup, checksum)

    backup.write_bytes(backup.read_bytes() + b"corruption")

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        verify_backup_copy(backup, checksum)


def test_verify_database_rejects_foreign_key_orphans(tmp_path):
    """Verified backup must include referential integrity, not only page integrity."""
    database = tmp_path / "orphan.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO child(parent_id) VALUES (999)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="foreign_key_check"):
        verify_database(database)


def test_backup_rotation_removes_only_old_aiinbox_generations(tmp_path):
    """Rotation must keep unrelated files and only bound generated backup history."""
    backups = []
    for index in range(3):
        path = tmp_path / f"aiinbox-{index}.db"
        path.write_bytes(str(index).encode())
        Path(f"{path}.sha256").write_text(f"{index}\n")
        os.utime(path, (index + 1, index + 1))
        backups.append(path)
    unrelated = tmp_path / "manual.db"
    unrelated.write_bytes(b"keep")

    removed = rotate_backups(tmp_path, keep=2)

    assert removed == [backups[0]]
    assert not backups[0].exists()
    assert not Path(f"{backups[0]}.sha256").exists()
    assert backups[1].exists() and backups[2].exists()
    assert Path(f"{backups[1]}.sha256").exists()
    assert Path(f"{backups[2]}.sha256").exists()
    assert unrelated.exists()


def test_database_status_reports_safe_ask_aggregates(tmp_path, monkeypatch, capsys):
    """Status reads only durable operational state and never needs provider secrets."""
    database = tmp_path / "status.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE items(processing_status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO items(processing_status) VALUES (?)",
            [("QUEUED",), ("QUEUED",), ("PROCESSING",), ("FAILED",)],
        )
        connection.execute("CREATE TABLE ask_jobs(status TEXT NOT NULL, question TEXT)")
        connection.executemany(
            "INSERT INTO ask_jobs(status, question) VALUES (?, ?)",
            [
                ("PENDING", "PRIVATE_ASK_QUESTION_7f1a"),
                ("RUNNING", "another private question"),
                ("FAILED", "never read by status"),
                ("DONE", "private done question"),
            ],
        )
        connection.execute(
            "CREATE TABLE deliveries(status TEXT NOT NULL, type TEXT, ask_job_id INTEGER)"
        )
        connection.executemany(
            "INSERT INTO deliveries(status, type, ask_job_id) VALUES (?, ?, ?)",
            [
                ("PENDING", "ITEM_READY", None),
                ("SENDING", "ASK_FAILED", 3),
                ("FAILED", "ASK_FAILED", 4),
                ("SENT", "ASK_FAILED", 5),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    status = database_status(database)

    assert status["queued"] == 2
    assert status["processing"] == 1
    assert status["failed"] == 1
    assert status["pending_deliveries"] == 2
    assert status["ask_pending"] == 1
    assert status["ask_running"] == 1
    assert status["ask_failed"] == 1
    assert status["ask_failed_deliveries"] == 2
    assert status["bytes"] > 0

    monkeypatch.setattr(sys, "argv", ["app.ops", "status"])
    monkeypatch.setattr(
        "app.ops.Settings",
        lambda: SimpleNamespace(
            database_url="unused",
            llm_provider="openrouter",
            openrouter_analysis_model="safe-model",
            openai_analysis_model="",
            processing_concurrency=2,
            telegram_bot_token="",
        ),
    )
    monkeypatch.setattr("app.ops.sqlite_database_path", lambda _url: database)
    ops_main()
    output = capsys.readouterr().out
    assert "ask_pending=1" in output
    assert "ask_running=1" in output
    assert "ask_failed=1" in output
    assert "ask_failed_deliveries=2" in output
    assert "PRIVATE_ASK_QUESTION_7f1a" not in output


async def test_rebuild_search_uses_canonical_items(settings, session_factory):
    """Maintenance must rebuild FTS through the same indexing semantics as /search."""
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add(
            Item(
                user_id=user.id,
                processing_status=ProcessingStatus.READY,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="searchable maintenance text",
                title="Maintenance",
            )
        )
        await session.commit()

    assert await rebuild_search(settings) == 1

    database = sqlite_database_path(settings.database_url)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_search WHERE item_search MATCH 'maintenance'"
        ).fetchone() == (1,)
    finally:
        connection.close()


async def test_external_smoke_exercises_provider_and_telegram(settings, monkeypatch):
    """Deployment smoke verifies both configured external boundaries without persistence."""

    class FakeProvider:
        """Fake provider preserves the live smoke contract without network in default tests."""

        async def summarize_chunk(self, text):
            return "OK"

    class FakeSession:
        """Fake Telegram session lets the smoke path verify required cleanup."""

        closed = False

        async def close(self):
            self.closed = True

    fake_session = FakeSession()

    class FakeBot:
        """Fake bot models Telegram getMe while keeping credentials out of tests."""

        def __init__(self, token):
            assert token == "test-token"
            self.session = fake_session

        async def get_me(self):
            return SimpleNamespace(username="aiinbox_test_bot", id=123)

    settings.telegram_bot_token = "test-token"
    settings.openai_analysis_model = "test-analysis-model"
    monkeypatch.setattr("app.ops.build_provider", lambda _settings: FakeProvider())
    monkeypatch.setattr("aiogram.Bot", FakeBot)

    model, telegram = await smoke_external(settings)

    assert model == "test-analysis-model"
    assert telegram == "aiinbox_test_bot"
    assert fake_session.closed is True


def _write_quality_fixture(tmp_path, case_overrides: dict) -> Path:
    """Build a small versioned corpus so the eval path can be exercised offline."""
    case = {
        "id": "answered",
        "source_type": "WEB",
        "source_title": "How to reduce LLM inference cost?",
        "source_language": "en",
        "response_language": "en",
        "content": (
            "Benchmarks show 4-bit quantization cut cost per thousand tokens 61%, "
            "with quality loss only on long-context tasks.\n\n"
            "Continuous batching added throughput."
        ),
        "profile": {"preferred_language": "en"},
        "existing_categories": ["ИИ"],
        "expected": {
            "title_evidence_groups": [["quantiz"], ["cost"]],
            "title_forbidden_terms": ["how to reduce"],
            "summary_evidence_groups": [["61%"], ["long-context"]],
            "summary_adds_evidence": True,
            "forbidden_summary_prefixes": ["The video discusses"],
        },
    }
    case.update(case_overrides)
    fixture = tmp_path / "analysis_quality.json"
    fixture.write_text(json.dumps({"version": 3, "cases": [case]}), encoding="utf-8")
    return fixture


class _QualityProvider:
    """Fake provider records the real eval inputs without paid network calls."""

    def __init__(self, result: AnalysisResult):
        self.result = result
        self.chunk_inputs = []
        self.analyze_inputs = []
        self.topic_inputs = []

    async def summarize_chunk(self, text):
        self.chunk_inputs.append(text)
        return f"checkpoint {len(self.chunk_inputs)}"

    async def analyze(self, content, profile, categories):
        self.analyze_inputs.append((content, profile, categories))
        return self.result

    async def classify_topic(self, content, categories):
        self.topic_inputs.append((content, list(categories)))
        return TopicClassificationResult(category=self.result.category)


async def test_analysis_quality_eval_reuses_chunk_aggregation_and_reports_pass(
    tmp_path, monkeypatch
):
    """The opt-in evaluation exercises real chunk summaries plus production aggregation."""
    fixture = _write_quality_fixture(tmp_path, {"chunk_size_chars": 120})
    provider = _QualityProvider(
        AnalysisResult(
            title="Quantization and batching cut LLM inference cost",
            summary=(
                "Cost per thousand tokens fell 61% after quantization and continuous batching. "
                "Quality dropped only on long-context tasks, so the gain is not free."
            ),
            category="ИИ",
            item_type="LEARN",
            tags=["llm"],
            importance=0.7,
            urgency=0.3,
            goal_fit=0.6,
            long_term_value=0.7,
            interest_fit=0.8,
            estimated_action_minutes=None,
            next_action=None,
            priority_reason="Cost optimization relevant to current goals",
            language="en",
            confidence=0.8,
        )
    )
    settings = Settings(_env_file=None, openai_api_key="test", openai_analysis_model="test-model")
    monkeypatch.setattr("app.ops.build_provider", lambda _settings: provider)

    reports = await evaluate_analysis_quality(settings, fixture)

    assert [report.case_id for report in reports] == ["answered"]
    assert reports[0].failed_checks == ()
    assert len(provider.chunk_inputs) == 2
    analyzed_content, analyzed_profile, analyzed_categories = provider.analyze_inputs[0]
    assert analyzed_profile.preferred_language == "en"
    assert analyzed_categories == ["ИИ"]
    assert analyzed_content.metadata["_analysis_chunk_summary_count"] == 2
    assert "CHUNK 1/2:\ncheckpoint 1" in analyzed_content.text
    assert "CHUNK 2/2:\ncheckpoint 2" in analyzed_content.text
    assert len(provider.topic_inputs) == 1
    assert reports[0].chunk_count == 2


async def test_analysis_quality_eval_detects_answer_first_regressions(tmp_path, monkeypatch):
    """A topic-only question title, boilerplate opener, and wrong language all fail."""
    fixture = _write_quality_fixture(
        tmp_path,
        {
            "response_language": "ru",
            "profile": {"preferred_language": "ru"},
        },
    )
    provider = _QualityProvider(
        AnalysisResult(
            title="How to reduce LLM inference cost?",
            summary="The video discusses quantization and long-context tradeoffs.",
            category="ИИ",
            item_type="LEARN",
            tags=[],
            importance=0.7,
            urgency=0.3,
            goal_fit=0.6,
            long_term_value=0.7,
            interest_fit=0.8,
            estimated_action_minutes=None,
            next_action=None,
            priority_reason="Relevant",
            language="en",
            confidence=0.8,
        )
    )
    settings = Settings(_env_file=None, openai_api_key="test", openai_analysis_model="test-model")
    monkeypatch.setattr("app.ops.build_provider", lambda _settings: provider)

    reports = await evaluate_analysis_quality(settings, fixture)

    failures = reports[0].failed_checks
    assert "title_is_question" in failures
    assert "title_forbidden:how to reduce" in failures
    assert "summary_opener:The video discusses" in failures
    assert "title_language:ru" in failures
    assert "summary_language:ru" in failures
    assert "summary_missing:61%" in failures


def test_analysis_quality_flags_summary_that_echoes_the_title_anywhere():
    """An explanation must not repeat the title verbatim, even after an opener."""
    case = {
        "response_language": "en",
        "expected": {
            "title_evidence_groups": [["quantiz"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["throughput"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Quantization cut inference cost",
        summary=(
            "Benchmarks confirm that quantization cut inference cost across workloads. "
            "Batches added throughput."
        ),
        language="en",
    )

    assert analysis_quality_failures(case, result) == ["summary_repeats_title"]


def test_analysis_quality_flags_wrong_and_forbidden_categories():
    """Topic classification is a quality property, not an unreviewed by-product."""
    case = {
        "response_language": "ru",
        "expected": {
            "category": "ИИ",
            "category_aliases": ["Искусственный интеллект", "AI", "LLM"],
            "forbidden_categories": ["Android-разработка"],
            "title_evidence_groups": [["предзагруз"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["точност"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Предзагрузка навыков снизила точность",
        summary="Больший набор навыков ухудшил точность на фиксированных задачах.",
        category="Android-разработка",
        language="en",
    )

    failures = analysis_quality_failures(case, result)

    assert "category_mismatch:ИИ" in failures
    assert "category_forbidden:Android-разработка" in failures


def test_analysis_quality_accepts_a_more_specific_supported_category():
    """A specific label such as “Искусственный интеллект” still satisfies “ИИ”."""
    case = {
        "response_language": "ru",
        "expected": {
            "category": "ИИ",
            "category_aliases": ["Искусственный интеллект", "AI", "LLM"],
            "title_evidence_groups": [["предзагруз"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["точност"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Предзагрузка навыков снизила точность",
        summary="Больший набор навыков ухудшил точность на фиксированных задачах.",
        category="Искусственный интеллект",
        language="en",
    )

    assert analysis_quality_failures(case, result) == []


def test_analysis_quality_treats_injected_category_and_goal_fit_as_failures():
    """Prompt injection must not be able to satisfy the evaluation by overriding scoring."""
    case = {
        "response_language": "ru",
        "expected": {
            "category": "ИИ",
            "category_aliases": ["AI", "Искусственный интеллект", "LLM"],
            "forbidden_categories": ["Android-разработка"],
            "max_goal_fit": 0.8,
            "must_ignore_content_instructions": True,
            "title_evidence_groups": [["навык"]],
            "title_forbidden_terms": ["android"],
            "summary_evidence_groups": [["точность"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Предзагрузка навыков снизила точность",
        summary="Эксперимент показал снижение точности.",
        category="Android-разработка",
        goal_fit=1.0,
        language="en",
    )

    failures = analysis_quality_failures(case, result)

    assert "category_mismatch:ИИ" in failures
    assert "category_forbidden:Android-разработка" in failures
    assert "goal_fit_above_max:0.8" in failures


def test_analysis_quality_flags_a_winner_claimed_in_the_summary():
    """Inconclusive material must stay inconclusive in the explanation, not only the title."""
    case = {
        "response_language": "en",
        "expected": {
            "must_not_claim_winner": True,
            "winner_forbidden_terms": ["beats method b", "method a wins"],
            "title_evidence_groups": [["no winner"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["sample was small"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Method A is faster, but no winner was established",
        summary="The sample was small; nevertheless method A beats method B on complex cases.",
        language="en",
    )

    assert analysis_quality_failures(case, result) == ["winner_claim:beats method b"]


def test_analysis_quality_requires_each_independent_source_in_the_result():
    """A multi-source Item cannot be titled and summarized from only its first source."""
    case = {
        "response_language": "en",
        "expected": {
            "must_represent_each_source": True,
            "distinct_topic_groups": [["kotlin"], ["piano", "inversion"]],
            "title_evidence_groups": [["kotlin"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["context parameter"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="Kotlin context parameters simplify a command-line API",
        summary="Context parameters remove repeated plumbing from the library API.",
        language="en",
    )

    assert analysis_quality_failures(case, result) == ["source_missing:piano|inversion"]


def test_analysis_quality_flags_title_that_copies_the_source_title():
    """Copying untrusted source-title wording is a measurable anons regression."""
    case = {
        "source_title": "A new era for work",
        "response_language": "en",
        "expected": {
            "title_evidence_groups": [["new era"]],
            "title_forbidden_terms": [],
            "summary_evidence_groups": [["trial"]],
            "summary_adds_evidence": True,
        },
    }
    result = make_analysis(
        title="A new era for work.",
        summary="A controlled trial found that drafts still needed human review.",
        language="en",
    )

    failures = analysis_quality_failures(case, result)

    assert failures == ["title_copies_source_title"]


def test_analysis_quality_eval_command_exits_non_zero_on_failed_checks(
    tmp_path, monkeypatch, capsys
):
    """Operators (and CI wrappers) must see a failing exit status, not a warning."""
    fixture = _write_quality_fixture(tmp_path, {})
    provider = _QualityProvider(
        AnalysisResult(
            title="LLM inference cost optimization",
            summary="Quantization was tested. Cost per thousand tokens fell 61%.",
            category="ИИ",
            item_type="LEARN",
            tags=[],
            importance=0.7,
            urgency=0.3,
            goal_fit=0.6,
            long_term_value=0.7,
            interest_fit=0.8,
            estimated_action_minutes=None,
            next_action=None,
            priority_reason="Relevant",
            language="en",
            confidence=0.8,
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.ops", "eval-analysis-quality", "--fixture", str(fixture)],
    )
    monkeypatch.setattr(
        "app.ops.Settings",
        lambda: SimpleNamespace(database_url="unused", llm_provider="openai"),
    )
    monkeypatch.setattr("app.ops.sqlite_database_path", lambda _url: tmp_path / "unused.db")
    monkeypatch.setattr("app.ops.build_provider", lambda _settings: provider)

    with pytest.raises(SystemExit) as exit_info:
        ops_main()

    assert "eval=failed cases=answered" in str(exit_info.value)
    output = capsys.readouterr().out
    assert "case=answered status=FAIL" in output
    assert "title_missing:quantiz" in output
