"""Персональный профиль пользователя: persistence + LLM patch (Phase 8).

Профиль хранится JSON'ом в users.profile_json (ТЗ §34 допускает);
patch через LLM валидируется Pydantic и применяется field-level merge —
незаполненные поля профиля не теряются (ТЗ §35: merge, не replacement).
"""

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import UserProfile
from app.llm.base import LlmProvider
from app.storage.models import User

log = logging.getLogger(__name__)


async def get_profile(session: AsyncSession, user_id: int) -> UserProfile:
    user = await session.get(User, user_id)
    if user is not None and user.profile_json:
        try:
            return UserProfile.model_validate(user.profile_json)
        except ValidationError:
            log.warning("corrupted profile_json for user %s — using default", user_id)
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


async def update_profile_from_text(
    session_factory,
    provider: LlmProvider,
    *,
    telegram_user_id: int,
    instruction: str,
) -> tuple[UserProfile, list[str]]:
    """/profile_update: natural language → LLM patch → валидация → field-level merge.
    Возвращает (обновлённый профиль, список изменённых полей)."""
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session, telegram_user_id=telegram_user_id, chat_id=telegram_user_id
        )
        current = await get_profile(session, user.id)
        patch = await provider.profile_update(instruction, current)
        data = patch.model_dump(exclude_unset=True, exclude_none=True)
        merged = current.model_copy(update=data)
        await save_profile(session, user.id, merged)
    log.info(
        "profile updated user_id=%s changed_fields=%s",
        telegram_user_id,
        list(data),
    )
    return merged, list(data)
