"""Phase 8: персональный профиль — persistence, /profile, /profile_update."""

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.formatting import format_profile
from app.bot.handlers import on_profile, on_profile_update
from app.domain.models import UserProfile
from app.llm.base import LlmError
from app.services.ingestion import ingest_message
from app.services.profile import (
    enqueue_profile_update,
    get_profile,
    load_profile_seed,
    update_profile_from_patch,
)
from app.storage.models import ProfileUpdateJob
from tests.fakes import FakeLlmProvider


async def test_profile_persistence_across_sessions(tmp_path, session_factory):
    # Регрессия: профиль сохраняется в users.profile_json и переживает restart.
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    provider = FakeLlmProvider(profile_patch={"profession": "dev", "interests": ["ai"]})
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="я разработчик"
    )
    profile, changed = await update_profile_from_patch(session_factory, provider, job)
    assert changed == ["profession", "interests"]
    assert profile.profession == "dev"

    # новая сессия видит тот же профиль
    async with session_factory() as session:
        again = await get_profile(session, 1)
    assert again.profession == "dev"


async def test_profile_update_merges_without_losing_fields(tmp_path, session_factory):
    # Регрессия: patch не удаляет незатронутые поля профиля (merge, не replacement).
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    job1 = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="start"
    )
    await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(profile_patch={"profession": "dev", "interests": ["ai", "piano"]}),
        job1,
    )
    job2 = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="сменить фокус"
    )
    merged, _ = await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(profile_patch={"free_text": "фокус на агентах"}),
        job2,
    )
    # interests/profession сохранены, free_text добавлен
    assert merged.profession == "dev"
    assert merged.interests == ["ai", "piano"]
    assert merged.free_text == "фокус на агентах"


async def test_profile_update_invalid_patch_rejected(tmp_path, session_factory):
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    provider = FakeLlmProvider(profile_error=LlmError("INVALID_LLM_OUTPUT", "bad patch"))
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="инструкция"
    )
    with pytest.raises(LlmError) as exc_info:
        await update_profile_from_patch(session_factory, provider, job)
    assert exc_info.value.code == "INVALID_LLM_OUTPUT"


async def test_profile_seed_from_yaml(tmp_path):
    seed_file = tmp_path / "profile.yaml"
    seed_file.write_text(
        "profession: dev\ngoals:\n  - name: AI\n    weight: 1.0\n",
        encoding="utf-8",
    )
    profile = load_profile_seed(seed_file)
    assert profile.profession == "dev"
    assert profile.goals[0].name == "AI"


def test_profile_seed_missing_file_returns_none(tmp_path):
    assert load_profile_seed(tmp_path / "nope.yaml") is None


def test_format_profile_renders_fields():
    profile = UserProfile(profession="dev", domains=["Android"], interests=["ai"])
    text = format_profile(profile)
    assert "Профессия: dev" in text
    assert "Домены: Android" in text
    assert "Интересы: ai" in text


def make_message(user_id: int, text: str = "/profile") -> Message:
    from datetime import UTC, datetime

    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="T"),
        text=text,
    )


def capture_answers(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return sent


async def test_on_profile_shows_saved_profile(settings, tmp_path, session_factory, monkeypatch):
    sent = capture_answers(monkeypatch)
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(profile_patch={"profession": "dev"}),
        await enqueue_profile_update(
            session_factory, telegram_user_id=42, chat_id=42, instruction="i"
        ),
    )
    await on_profile(make_message(42), settings, session_factory)
    assert any("Профессия: dev" in s for s in sent)


async def test_on_profile_update_enqueues_durable_job(
    settings, tmp_path, session_factory, monkeypatch
):
    # /profile_update: быстрый ACK + durable PENDING job; LLM выполняет worker.
    sent = capture_answers(monkeypatch)
    await on_profile_update(
        make_message(42, "/profile_update теперь ml"),
        settings,
        session_factory,
        "теперь ml",
    )
    assert any(s.startswith("Принял") for s in sent)
    async with session_factory() as session:
        job = await session.scalar(select(ProfileUpdateJob))
        assert job is not None and job.status == "PENDING"
        assert job.instruction == "теперь ml"
