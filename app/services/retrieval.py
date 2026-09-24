"""Запросы пользовательского retrieval UI Phase 9.

Сервис оставляет ranking простым и объяснимым: Today использует только
зафиксированные поля Item, а FTS индекс контролируется приложением и включает
длинный контент из ``contents`` без SQLite trigger-магии.
"""

import re

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ACTIONABLE_ITEM_TYPES, ItemState, ProcessingStatus
from app.storage.models import Content, Item

_FTS_TABLE = "item_search"
_DEFAULT_INBOX_LIMIT = 20
_DEFAULT_SEARCH_LIMIT = 10
_MAX_SEARCH_LIMIT = 20


async def ensure_search_index(session: AsyncSession) -> None:
    """Создаёт FTS5 seam также для тестовых create_all-баз без Alembic."""
    await session.execute(
        text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS item_search USING fts5("
            "item_id UNINDEXED, user_id UNINDEXED, title, summary, user_note, tags, content)"
        )
    )


async def sync_item_search(session: AsyncSession, item_id: int) -> None:
    """Обновляет поисковую строку Item в текущей транзакции.

    Индекс не является каноническим состоянием: при отсутствии строки она
    восстанавливается из Item и contents, поэтому READY commit остаётся
    источником истины.
    """
    await ensure_search_index(session)
    item = await session.get(Item, item_id)
    if item is None:
        return
    contents = (
        await session.scalars(
            select(Content).where(Content.item_id == item_id).order_by(Content.id)
        )
    ).all()
    await session.execute(
        text("DELETE FROM item_search WHERE item_id = :item_id"), {"item_id": item_id}
    )
    tags = item.tags_json or []
    tags_text = " ".join(str(tag) for tag in tags)
    content_text = "\n".join(content.text for content in contents)
    await session.execute(
        text(
            "INSERT INTO item_search(item_id, user_id, title, summary, user_note, tags, content) "
            "VALUES (:item_id, :user_id, :title, :summary, :user_note, :tags, :content)"
        ),
        {
            "item_id": item.id,
            "user_id": item.user_id,
            "title": item.title or "",
            "summary": item.summary or "",
            "user_note": item.user_note or "",
            "tags": tags_text,
            "content": content_text,
        },
    )


async def rebuild_user_search_index(session: AsyncSession, user_id: int) -> None:
    """Синхронизирует все Items пользователя перед поиском.

    Это намеренно простая миграционно-безопасная стратегия: Items, созданные
    до FTS-миграции, тоже становятся searchable без отдельного backfill worker.
    """
    await ensure_search_index(session)
    items = (await session.scalars(select(Item).where(Item.user_id == user_id))).all()
    await session.execute(
        text("DELETE FROM item_search WHERE user_id = :user_id"), {"user_id": user_id}
    )
    for item in items:
        await sync_item_search(session, item.id)


class TodayService:
    """Выбирает небольшой actionable срез без отдельного recommendation engine."""

    def __init__(self, default_limit: int = 3, absolute_max: int = 5):
        self.default_limit = default_limit
        self.absolute_max = absolute_max

    async def list_items(
        self, session: AsyncSession, user_id: int, limit: int | None = None
    ) -> list[Item]:
        requested = self.default_limit if limit is None else limit
        limit = max(0, min(requested, self.absolute_max))
        result = await session.scalars(
            select(Item)
            .where(
                Item.user_id == user_id,
                Item.processing_status == ProcessingStatus.READY,
                Item.state == ItemState.ACTIVE,
                Item.item_type.in_(ACTIONABLE_ITEM_TYPES),
            )
            .order_by(Item.priority_score.desc(), Item.created_at.asc(), Item.id.asc())
            .limit(limit)
        )
        return list(result.all())


async def list_inbox(
    session: AsyncSession, user_id: int, limit: int = _DEFAULT_INBOX_LIMIT
) -> list[Item]:
    """Последние Items; lifecycle-фильтр отсутствует намеренно."""
    limit = max(0, min(limit, _MAX_SEARCH_LIMIT))
    result = await session.scalars(
        select(Item)
        .where(Item.user_id == user_id)
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return list(result.all())


async def list_categories(session: AsyncSession, user_id: int) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(Item.category, func.count(Item.id))
        .where(Item.user_id == user_id, Item.category.is_not(None))
        .group_by(Item.category)
        .order_by(Item.category.asc())
    )
    return [(category, count) for category, count in rows.all()]


async def list_category_items(
    session: AsyncSession, user_id: int, category: str, limit: int = _DEFAULT_INBOX_LIMIT
) -> list[Item]:
    limit = max(0, min(limit, _MAX_SEARCH_LIMIT))
    result = await session.scalars(
        select(Item)
        .where(Item.user_id == user_id, Item.category == category)
        .order_by(Item.priority_score.desc(), Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return list(result.all())


def _fts_query(query: str) -> str:
    # Цитируем токены, чтобы пользовательский синтаксис MATCH не превращался
    # в SQL/FTS exception; OR сохраняет результаты для частичных запросов.
    tokens = re.findall(r"[^\s]+", query.strip())
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


async def search_items(
    session: AsyncSession, user_id: int, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
) -> list[Item]:
    """Ищет по Item и extracted content, включая DONE/ARCHIVED Items."""
    match = _fts_query(query)
    if not match:
        return []
    limit = max(0, min(limit, _MAX_SEARCH_LIMIT))
    await rebuild_user_search_index(session, user_id)
    rows = await session.execute(
        text(
            "SELECT i.* FROM item_search AS s JOIN items AS i ON i.id = s.item_id "
            "WHERE s.user_id = :user_id AND item_search MATCH :match "
            "ORDER BY bm25(item_search), i.created_at DESC, i.id DESC LIMIT :limit"
        ),
        {"user_id": user_id, "match": match, "limit": limit},
    )
    item_ids = [row.id for row in rows]
    if not item_ids:
        return []
    items = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
    by_id = {item.id: item for item in items}
    return [by_id[item_id] for item_id in item_ids]
