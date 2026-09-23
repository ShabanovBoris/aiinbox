"""Phase 8: персональный профиль — persistence, /profile, /profile_update."""

import asyncio

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.bot.formatting import format_profile
from app.bot.handlers import on_profile, on_profile_update
from app.domain.models import UserProfile
from app.domain.priority import PriorityEngine
from app.llm.base import LlmError
from app.services.analysis import Analyzer
from app.services.delivery import PROFILE_UPDATED
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.services.profile import (
    apply_profile_seed,
    configure_profile_seed,
    enqueue_profile_update,
    get_profile,
    load_profile_seed,
    requeue_running_profile_jobs,
    update_profile_from_patch,
)
from app.storage.models import Delivery, ProfileUpdateJob, User
from app.workers.processing import ProcessingWorker
from app.workers.profile import ProfileUpdateWorker
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
    assert again.preferred_language == "ru"


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


async def test_profile_update_changes_response_language(session_factory):
    """Store response-language preference in the existing JSON profile."""
    job = await enqueue_profile_update(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        instruction="перейти на английский язык ответов",
    )
    profile, changed = await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(profile_patch={"preferred_language": "en"}),
        job,
    )

    assert profile.preferred_language == "en"
    assert changed == ["preferred_language"]
    assert "Язык ответа: en" in format_profile(profile)


async def test_constraints_entries_merged_into_dict(tmp_path, session_factory):
    # Регрессия: ConstraintEntry[] → dict в persisted профиле (strict-совместимая
    # форма для Structured Outputs), непустые constraints читаются обратно.
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="лимиты времени"
    )
    merged, _ = await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(
            profile_patch={
                "constraints": [
                    {"key": "weekday_free_minutes", "value": 60},
                    {"key": "weekend_free_minutes", "value": 180},
                ]
            }
        ),
        job,
    )
    assert merged.constraints == {
        "weekday_free_minutes": 60,
        "weekend_free_minutes": 180,
    }
    async with session_factory() as session:
        user = await session.get(User, 1)
    # persisted profile_json.constraints — dict, а не массив
    assert isinstance(user.profile_json["constraints"], dict)


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
    assert "Язык ответа: ru" in text


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


async def test_first_profile_without_seed_persists_user(
    settings, tmp_path, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    configure_profile_seed(tmp_path / "missing-profile.yaml")

    await on_profile(make_message(42), settings, session_factory)

    assert sent
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user is not None
        assert user.telegram_chat_id == 42


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


async def test_analyzer_receives_profile_from_db(tmp_path, session_factory):
    # Регрессия acceptance «profile passed to analyzer»: pipeline передаёт
    # сохранённый профиль пользователя, а не default.
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    await update_profile_from_patch(
        session_factory,
        FakeLlmProvider(profile_patch={"profession": "dev", "interests": ["ai", "piano"]}),
        await enqueue_profile_update(
            session_factory, telegram_user_id=42, chat_id=42, instruction="i"
        ),
    )
    provider = FakeLlmProvider(vision=False)
    worker = ProcessingWorker(
        session_factory, ProcessingPipeline(Analyzer(provider), PriorityEngine()), poll_seconds=0.01
    )
    await worker.process_one()
    ((_, received_profile, _),) = provider.calls
    assert received_profile.profession == "dev"
    assert received_profile.interests == ["ai", "piano"]


async def test_profile_seed_applied_to_empty_profiles_only(tmp_path, session_factory):
    seed = tmp_path / "profile.yaml"
    seed.write_text("profession: dev\n", encoding="utf-8")
    await ingest_message(session_factory, telegram_user_id=42, chat_id=42, message_id=1, text="x")
    await ingest_message(
        session_factory, telegram_user_id=1000, chat_id=1000, message_id=2, text="y"
    )
    # существующий профиль второго пользователя не перезаписывается
    async with session_factory() as session:
        user = await session.get(User, 2)
        user.profile_json = {"profession": "existing"}
        await session.commit()

    applied = await apply_profile_seed(session_factory, seed)
    assert applied == 1  # только пользователь с NULL профилем
    async with session_factory() as session:
        first = await get_profile(session, 1)
        second = await get_profile(session, 2)
    assert first.profession == "dev"
    assert second.profession == "existing"


async def test_missing_seed_file_is_noop(tmp_path, session_factory):
    assert await apply_profile_seed(session_factory, tmp_path / "nope.yaml") == 0


async def test_lazily_created_user_gets_seed_immediately(tmp_path, session_factory, monkeypatch):
    # Регрессия: пользователь, созданный ПОСЛЕ старта, получает seed при первом
    # чтении профиля (lazy seed), существующий профиль не перезаписывается.
    from app.services.profile import configure_profile_seed

    seed = tmp_path / "profile.yaml"
    seed.write_text("profession: seeded\n", encoding="utf-8")
    configure_profile_seed(seed)
    try:
        await ingest_message(
            session_factory, telegram_user_id=777, chat_id=777, message_id=1, text="x"
        )
        async with session_factory() as session:
            profile = await get_profile(session, 1)
        assert profile.profession == "seeded"

        # существующий профиль не перезаписывается
        await update_profile_from_patch(
            session_factory,
            FakeLlmProvider(profile_patch={"profession": "custom"}),
            await enqueue_profile_update(
                session_factory, telegram_user_id=777, chat_id=777, instruction="i"
            ),
        )
        async with session_factory() as session:
            profile = await get_profile(session, 1)
        assert profile.profession == "custom"
    finally:
        configure_profile_seed("")


async def test_profile_update_concurrent_disjoint_fields_merge(tmp_path, session_factory):
    # Регрессия: конкурентные /profile_update разных полей не затирают друг друга
    # (DB-side json_patch merge).
    await asyncio.gather(
        update_profile_from_patch(
            session_factory,
            FakeLlmProvider(profile_patch={"profession": "dev"}),
            await enqueue_profile_update(
                session_factory, telegram_user_id=42, chat_id=42, instruction="a"
            ),
        ),
        update_profile_from_patch(
            session_factory,
            FakeLlmProvider(profile_patch={"free_text": "фокус"}),
            await enqueue_profile_update(
                session_factory, telegram_user_id=42, chat_id=42, instruction="b"
            ),
        ),
    )
    async with session_factory() as session:
        profile = await get_profile(session, 1)
    assert profile.profession == "dev"
    assert profile.free_text == "фокус"


async def test_profile_update_job_lifecycle_done(tmp_path, session_factory):
    # Регрессия: PENDING -> RUNNING (claim) -> DONE (успех) — job не остаётся
    # навсегда в RUNNING.
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="i"
    )
    provider = FakeLlmProvider(profile_patch={"profession": "x"})
    worker = ProfileUpdateWorker(session_factory, provider, poll_seconds=0.01)
    assert await worker.process_one() is True
    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, job.id)
        assert stored.status == "DONE"
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.profile_update_job_id == job.id,
                Delivery.type == PROFILE_UPDATED,
            )
        )
        assert delivery is not None
        assert delivery.status == "PENDING"
        assert delivery.payload_json == {"changed": ["profession"]}
    async with session_factory() as session:
        profile = await get_profile(session, 1)
    assert profile.profession == "x"


async def test_profile_update_job_failure_lifecycle(tmp_path, session_factory):
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="i"
    )
    worker = ProfileUpdateWorker(
        session_factory,
        FakeLlmProvider(profile_error=LlmError("INVALID_LLM_OUTPUT", "bad")),
        poll_seconds=0.01,
    )
    assert await worker.process_one() is True
    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, job.id)
        assert stored.status == "FAILED"
        assert stored.error_code == "INVALID_LLM_OUTPUT"
    async with session_factory() as session:
        profile = await get_profile(session, 1)
    assert profile.profession is None


async def test_profile_worker_propagates_database_failure_for_process_restart(
    tmp_path, session_factory, monkeypatch
):
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="i"
    )

    async def fail_with_database_error(*args, **kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr("app.workers.profile.update_profile_from_patch", fail_with_database_error)
    worker = ProfileUpdateWorker(session_factory, FakeLlmProvider(), poll_seconds=0.01)

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await worker.process_one()

    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, job.id)
        assert stored.status == "RUNNING"


async def test_running_profile_job_recovered_on_startup(tmp_path, session_factory):
    # Регрессия: RUNNING job после смерти процесса возвращается в PENDING на
    # старте и успешно обрабатывается.
    job = await enqueue_profile_update(
        session_factory, telegram_user_id=42, chat_id=42, instruction="i"
    )
    async with session_factory() as session:
        row = await session.get(ProfileUpdateJob, job.id)
        row.status = "RUNNING"
        await session.commit()

    assert await requeue_running_profile_jobs(session_factory) == 1
    worker = ProfileUpdateWorker(
        session_factory, FakeLlmProvider(profile_patch={"profession": "x"}), poll_seconds=0.01
    )
    assert await worker.process_one() is True
    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, job.id)
        assert stored.status == "DONE"


# ❌ Удален тест best-effort _profile_done_notifier: production больше не
# отправляет callback после commit; адресат и payload проверяются DeliveryWorker.


async def test_recovery_after_mutation_is_idempotent(tmp_path, session_factory):
    # Регрессия recovery после side effect boundary: profile уже применён, но job
    # остался RUNNING (крэш до атомарности в старой схеме) → recovery → повторная
    # обработка → финальный профиль консистентен (replace-merge идемпотентен).
    await enqueue_profile_update(session_factory, telegram_user_id=42, chat_id=42, instruction="i")
    provider = FakeLlmProvider(profile_patch={"profession": "dev"})
    worker = ProfileUpdateWorker(session_factory, provider, poll_seconds=0.01)
    assert await worker.process_one() is True

    # искусственно "крэшим": job назад в RUNNING
    async with session_factory() as session:
        row = await session.get(ProfileUpdateJob, 1)
        row.status = "RUNNING"
        await session.commit()
    assert await requeue_running_profile_jobs(session_factory) == 1

    assert await worker.process_one() is True
    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, 1)
        assert stored.status == "DONE"
    async with session_factory() as session:
        profile = await get_profile(session, 1)
    assert profile.profession == "dev"  # replace-merge идемпотентен
