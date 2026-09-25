"""Запросы пользовательского retrieval UI Phase 9.

Сервис оставляет ranking простым и объяснимым: Today использует только
зафиксированные поля Item, а FTS индекс контролируется приложением и включает
длинный контент из ``contents`` без SQLite trigger-магии.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.category_tokens import category_token
from app.domain.enums import ACTIONABLE_ITEM_TYPES, ContentKind, ItemState, ProcessingStatus
from app.storage.models import Content, Item, ItemSource

_FTS_TABLE = "item_search"
_DEFAULT_SEARCH_LIMIT = 10
_MAX_SEARCH_RESULTS = 20
_DEFAULT_PAGE_SIZE = 10
_MAX_PAGE_SIZE = 12
_CATEGORY_RESOLUTION_PAGE_SIZE = 100
_DEFAULT_ASK_LIMIT = 8
_MAX_ASK_LIMIT = 10


@dataclass(frozen=True, slots=True)
class ItemPage:
    """One bounded owner-scoped slice; the page boundary is not a storage quota."""

    items: tuple[Item, ...]
    page: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class CategoryPage:
    """A bounded category chooser slice with a stable caller-selected ordering."""

    categories: tuple[tuple[str, int], ...]
    page: int
    has_previous: bool
    has_next: bool


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
            select(Content)
            .where(
                Content.item_id == item_id,
                Content.kind != ContentKind.ATTENTION_HOOK,
            )
            .order_by(Content.id)
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


# ❌ Удалены list_inbox/list_category_items с общим max=20: Items остаются доступны
# через SQL-ограниченные страницы, а page size ограничивает один запрос, не Inbox.
async def list_inbox_page(
    session: AsyncSession,
    user_id: int,
    *,
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> ItemPage:
    """Return a page in Inbox order without imposing a total accessible-item cap."""
    size = _page_size(page_size)
    total = int(
        await session.scalar(select(func.count(Item.id)).where(Item.user_id == user_id)) or 0
    )
    current_page = _page_for_total(page, total, size)
    rows = list(
        (
            await session.scalars(
                select(Item)
                .where(Item.user_id == user_id)
                .order_by(Item.created_at.desc(), Item.id.desc())
                .limit(size + 1)
                .offset(current_page * size)
            )
        ).all()
    )
    return ItemPage(
        items=tuple(rows[:size]),
        page=current_page,
        has_previous=current_page > 0,
        has_next=len(rows) > size,
    )


async def list_categories_page(
    session: AsyncSession,
    user_id: int,
    *,
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
    by_frequency: bool = False,
) -> CategoryPage:
    """Return bounded category choices; each grouped query emits at most 13 rows."""
    size = _page_size(page_size)
    grouped = (
        select(Item.category.label("category"))
        .where(Item.user_id == user_id, Item.category.is_not(None))
        .group_by(Item.category)
    )
    total = int(await session.scalar(select(func.count()).select_from(grouped.subquery())) or 0)
    current_page = _page_for_total(page, total, size)
    ordering = (
        (func.count(Item.id).desc(), Item.category.asc())
        if by_frequency
        else (Item.category.asc(),)
    )
    rows = await session.execute(
        select(Item.category, func.count(Item.id))
        .where(Item.user_id == user_id, Item.category.is_not(None))
        .group_by(Item.category)
        .order_by(*ordering)
        .limit(size + 1)
        .offset(current_page * size)
    )
    categories = list(rows.all())
    return CategoryPage(
        categories=tuple((name, int(count)) for name, count in categories[:size]),
        page=current_page,
        has_previous=current_page > 0,
        has_next=len(categories) > size,
    )


async def resolve_category_token(session: AsyncSession, user_id: int, token: str) -> str | None:
    """Resolve a current owner category in bounded batches and reject token collisions."""
    if not re.fullmatch(r"[0-9a-f]{20}", token):
        return None
    matches: list[str] = []
    offset = 0
    while True:
        rows = await session.scalars(
            select(Item.category)
            .where(Item.user_id == user_id, Item.category.is_not(None))
            .group_by(Item.category)
            .order_by(Item.category.asc())
            .limit(_CATEGORY_RESOLUTION_PAGE_SIZE)
            .offset(offset)
        )
        categories = list(rows.all())
        matches.extend(name for name in categories if category_token(name) == token)
        if len(matches) > 1 or len(categories) < _CATEGORY_RESOLUTION_PAGE_SIZE:
            break
        offset += _CATEGORY_RESOLUTION_PAGE_SIZE
    return matches[0] if len(matches) == 1 else None


async def list_category_items_page(
    session: AsyncSession,
    user_id: int,
    category: str,
    *,
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> ItemPage:
    """Return a bounded priority-ordered page for one exact owner category."""
    size = _page_size(page_size)
    condition = (Item.user_id == user_id, Item.category == category)
    total = int(await session.scalar(select(func.count(Item.id)).where(*condition)) or 0)
    current_page = _page_for_total(page, total, size)
    rows = list(
        (
            await session.scalars(
                select(Item)
                .where(*condition)
                .order_by(Item.priority_score.desc(), Item.created_at.desc(), Item.id.desc())
                .limit(size + 1)
                .offset(current_page * size)
            )
        ).all()
    )
    return ItemPage(
        items=tuple(rows[:size]),
        page=current_page,
        has_previous=current_page > 0,
        has_next=len(rows) > size,
    )


async def load_item_sources_by_item(
    session: AsyncSession, item_ids: Sequence[int]
) -> dict[int, list[ItemSource]]:
    """Batch the page's source metadata so title projections never issue N+1 reads."""
    grouped = {item_id: [] for item_id in item_ids}
    if not grouped:
        return grouped
    sources = (
        await session.scalars(
            select(ItemSource)
            .where(ItemSource.item_id.in_(tuple(grouped)))
            .order_by(ItemSource.item_id, ItemSource.source_index, ItemSource.id)
        )
    ).all()
    for source in sources:
        grouped[source.item_id].append(source)
    return grouped


def _page_size(value: int) -> int:
    """Enforce the service's fixed upper bound before constructing any SQL page query."""
    if not isinstance(value, int) or isinstance(value, bool):
        return _DEFAULT_PAGE_SIZE
    return max(1, min(value, _MAX_PAGE_SIZE))


def _page_for_total(requested: int, total: int, page_size: int) -> int:
    """Clamp stale or huge callback pages to the last reachable offset for this owner."""
    last_page = max(0, (total - 1) // page_size)
    if not isinstance(requested, int) or isinstance(requested, bool):
        return 0
    return max(0, min(requested, last_page))


def _fts_query(query: str) -> str:
    # Цитируем токены, чтобы пользовательский синтаксис MATCH не превращался
    # в SQL/FTS exception; OR сохраняет результаты для частичных запросов.
    tokens = re.findall(r"[^\s]+", query.strip())
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Small ranked retrieval result; Ask separately loads trusted persisted evidence."""

    item_id: int
    fts_rank: float
    matched_snippet: str | None


async def search_item_hits(
    session: AsyncSession,
    user_id: int,
    query: str,
    *,
    limit: int = _DEFAULT_ASK_LIMIT,
) -> list[SearchHit]:
    """Return bounded user-scoped lexical hits without changing `/search` results."""
    match = _fts_query(query)
    if not match:
        return []
    limit = max(0, min(limit, _MAX_ASK_LIMIT))
    await rebuild_user_search_index(session, user_id)
    rows = await session.execute(
        text(
            "SELECT i.id AS item_id, bm25(item_search) AS fts_rank, "
            "snippet(item_search, 6, '', '', ' … ', 18) AS matched_snippet "
            "FROM item_search AS s JOIN items AS i ON i.id = CAST(s.item_id AS INTEGER) "
            "WHERE s.user_id = :user_id AND i.user_id = :user_id "
            "AND item_search MATCH :match "
            "ORDER BY bm25(item_search), i.created_at DESC, i.id DESC LIMIT :limit"
        ),
        {"user_id": user_id, "match": match, "limit": limit},
    )
    return [
        SearchHit(
            item_id=row.item_id,
            fts_rank=float(row.fts_rank),
            matched_snippet=(row.matched_snippet[:1200] if row.matched_snippet else None),
        )
        for row in rows
    ]


async def search_items(
    session: AsyncSession, user_id: int, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
) -> list[Item]:
    """Ищет по Item и extracted content, включая DONE/ARCHIVED Items."""
    match = _fts_query(query)
    if not match:
        return []
    limit = max(0, min(limit, _MAX_SEARCH_RESULTS))
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
