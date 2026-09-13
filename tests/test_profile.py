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
    get_profile,
    load_profile_seed,
    update_profile_from_text,
)
from app.storage.models import User
from tests.fakes import FakeLlmProvider


async def test_profile_persistence_across_sessions(tmp_path, session_factory):
    # Регрессия: профиль сохраняется в users.profile_json и переживает restart.
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    provider = FakeLlmProvider(profile_patch={"profession": "dev", "interests": ["ai"]})
    profile, changed = await update_profile_from_text(
        session_factory, provider, telegram_user_id=42, instruction="я разработчик"
    )
    assert changed == ["profession", "interests"]
    assert profile.profession == "dev"

    # новая сессия видит тот же профиль
    async with session_factory() as session:
        again = await get_profile(session, 1)
    assert again.profession == "dev"


async def test_profile_update_merges_without_losing_fields(tmp_path, session_factory):
    # Регрессия: patch не удаляет незатронутые поля профиля (merge, не replacement).
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    await update_profile_from_text(
        session_factory,
        FakeLlmProvider(profile_patch={"profession": "dev", "interests": ["ai", "piano"]}),
        telegram_user_id=42,
        instruction="start",
    )
    merged, _ = await update_profile_from_text(
        session_factory,
        FakeLlmProvider(profile_patch={"free_text": "фокус на агентах"}),
        telegram_user_id=42,
        instruction="сменить фокус",
    )
    # interests/profession сохранены, free_text добавлен
    assert merged.profession == "dev"
    assert merged.interests == ["ai", "piano"]
    assert merged.free_text == "фокус на агентах"


async def test_profile_update_invalid_patch_rejected(tmp_path, session_factory):
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    provider = FakeLlmProvider(profile_error=LlmError("INVALID_LLM_OUTPUT", "bad patch"))
    with pytest.raises(LlmError) as exc_info:
        await update_profile_from_text(
            session_factory,
            provider,
            telegram_user_id=42,
            instruction="инструкция",
        )
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
    await update_profile_from_text(
        session_factory,
        FakeLlmProvider(profile_patch={"profession": "dev"}),
        telegram_user_id=42,
        instruction="i",
    )
    await on_profile(make_message(42), settings, session_factory)
    assert any("Профессия: dev" in s for s in sent)


async def test_on_profile_update_persists_changes(settings, tmp_path, session_factory, monkeypatch):
    sent = capture_answers(monkeypatch)
    provider = FakeLlmProvider(profile_patch={"profession": "ml engineer", "interests": ["ai"]})
    await on_profile_update(
        make_message(42, "/profile_update теперь ml"),
        settings,
        session_factory,
        provider,
        "теперь ml",
    )
    assert any(s.startswith("Профиль обновлён") for s in sent)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user is not None and user.profile_json["profession"] == "ml engineer"
