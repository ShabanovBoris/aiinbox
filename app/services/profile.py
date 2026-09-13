"""Персональный профиль пользователя: persistence + LLM patch + фоновые jobs.

Профиль хранится JSON'ом в users.profile_json (ТЗ §34 допускает); patch через
LLM валидируется Pydantic и применяется DB-side атомарно (json_patch) —
конкурентные обновления разных полей не затирают друг друга.
/profile_update ставит durable job (быстрый ACK); LLM+merge выполняет worker.
"""

import json
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import ProfilePatch, UserProfile
from app.errors import AppError
from app.llm.base import LlmProvider
from app.storage.models import ProfileUpdateJob, User

log = logging.getLogger(__name__)

# Путь к seed-файлу, настраивается на старте (configure_profile_seed).
_seed_path: str | None = None


def configure_profile_seed(path: str | Path) -> None:
    global _seed_path
    _seed_path = str(path)


async def get_profile(session: AsyncSession, user_id: int) -> UserProfile:
    user = await session.get(User, user_id)
    if user is not None and user.profile_json:
        try:
            return UserProfile.model_validate(user.profile_json)
        except ValidationError:
            log.warning("corrupted profile_json for user %s — using default", user_id)
        return UserProfile()
    # Ленивый seed: пользователь создан после старта — seed применяется сразу
    # при первом чтении профиля; существующие профили не перезаписываются.
    if user is not None and user.profile_json is None and _seed_path:
        seed = load_profile_seed(_seed_path)
        if seed is not None:
            user.profile_json = seed.model_dump()
            await session.commit()
            return seed
    return UserProfile()


async def save_profile(session: AsyncSession, user_id: int, profile: UserProfile) -> None:
    user = await session.get(User, user_id)
    if user is None:
        return
    user.profile_json = profile.model_dump()
    await session.commit()


def load_profile_seed(path: str | Path) -> UserProfile | None:
    """Начальный профиль из YAML-файла (profile.example.yaml → profile.yaml)."""
    path = Path(path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return UserProfile.model_validate(data)


async def apply_profile_seed(session_factory: async_sessionmaker, seed_path: str | Path) -> int:
    """Seed при старте: пользователи с пустым профилем получают seed;
    существующие профили не перезаписываются. Возвращает число обновлённых."""
    profile = load_profile_seed(seed_path)
    if profile is None:
        return 0
    async with session_factory() as session:
        result = await session.execute(
            update(User)
            .where(User.profile_json.is_(None))
            .values(profile_json=profile.model_dump())
        )
        await session.commit()
        if result.rowcount:
            log.info("profile seed applied to %s user(s)", result.rowcount)
        return result.rowcount


async def enqueue_profile_update(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    chat_id: int,
    instruction: str,
    default_timezone: str = "UTC",
) -> ProfileUpdateJob:
    """/profile_update: durable job — handler отвечает мгновенно,
    LLM/merge выполняет фоновый worker (ТЗ: handler без тяжёлой работы)."""
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=default_timezone,
        )
        job = ProfileUpdateJob(user_id=user.id, instruction=instruction, status="PENDING")
        session.add(job)
        await session.commit()
        log.info("profile update job queued id=%s user_id=%s", job.id, user.id)
        return job


async def claim_oldest_profile_update(
    session_factory: async_sessionmaker,
) -> ProfileUpdateJob | None:
    """Атомарный claim oldest PENDING job (аналогично claim_next у Items)."""
    async with session_factory() as session:
        result = await session.execute(
            update(ProfileUpdateJob)
            .where(
                ProfileUpdateJob.id
                == select(ProfileUpdateJob.id)
                .where(ProfileUpdateJob.status == "PENDING")
                .order_by(ProfileUpdateJob.created_at, ProfileUpdateJob.id)
                .limit(1)
                .scalar_subquery(),
                ProfileUpdateJob.status == "PENDING",
            )
            .values(status="RUNNING")
            .returning(ProfileUpdateJob.id)
        )
        claimed = result.scalar_one_or_none()
        await session.commit()
        if claimed is None:
            return None
        return await session.get(ProfileUpdateJob, claimed)


async def requeue_running_profile_jobs(session_factory: async_sessionmaker) -> int:
    """Startup recovery: RUNNING job после смерти процесса возвращается в PENDING."""
    async with session_factory() as session:
        result = await session.execute(
            update(ProfileUpdateJob)
            .where(ProfileUpdateJob.status == "RUNNING")
            .values(status="PENDING")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued RUNNING profile jobs count=%s", result.rowcount)
        return result.rowcount


async def finish_profile_update(
    session_factory: async_sessionmaker,
    job: ProfileUpdateJob,
    *,
    error: AppError | None = None,
) -> None:
    async with session_factory() as session:
        stored = await session.get(ProfileUpdateJob, job.id)
        if stored is None:
            return
        if error is None:
            stored.status = "DONE"
        else:
            stored.status = "FAILED"
            stored.error_code = error.code
            stored.error_message = str(error)[:500]
        await session.commit()


async def update_profile_from_patch(
    session_factory: async_sessionmaker,
    provider: LlmProvider,
    job: ProfileUpdateJob,
) -> tuple[UserProfile, list[str]]:
    """Apply patch: LLM profile_update → DB-side атомарный json_patch merge.

    RFC 7396 merge на уровне БД: конкурентные обновления РАЗНЫХ полей не
    затирают друг друга (read-modify-write гонка исключена).
    constraints приходят как list[ConstraintEntry] → конвертируются в dict
    до merge (UserProfile.constraints — dict).
    """
    async with session_factory() as session:
        current = await get_profile(session, job.user_id)
    patch: ProfilePatch = await provider.profile_update(job.instruction, current)
    data = patch.model_dump(exclude_unset=True, exclude_none=True)
    if "constraints" in data and data["constraints"] is not None:
        entries = data.pop("constraints")
        merged_constraints = dict(current.constraints)
        for entry in entries:
            merged_constraints[entry["key"]] = entry["value"]
        data["constraints"] = merged_constraints

    async with session_factory() as session:
        # одна транзакция: profile mutation + job DONE — крэш между ними
        # невозможен, повтор natural-language update исключён
        await session.execute(
            update(User)
            .where(User.id == job.user_id)
            .values(
                profile_json=func.json_patch(
                    func.coalesce(User.profile_json, "{}"), json.dumps(data)
                )
            )
        )
        await session.execute(
            update(ProfileUpdateJob).where(ProfileUpdateJob.id == job.id).values(status="DONE")
        )
        await session.commit()
        merged = await get_profile(session, job.user_id)
    return merged, list(data)
