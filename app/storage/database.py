from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings


def enable_sqlite_fk(engine: AsyncEngine) -> AsyncEngine:
    """SQLite не enforce'ит foreign keys по умолчанию — PRAGMA включается на
    каждом connection (https://sqlite.org/foreignkeys.html). Без этого связи
    items.user_id → users.id существовали бы только формально."""

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(dbapi_connection, _record):
        # Без явного cursor.close(): адаптер aiosqlite создаёт при close
        # незавершённую корутину (RuntimeWarning); курсор закрывается GC.
        dbapi_connection.execute("pragma foreign_keys=ON")

    return engine


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        # SQLite-файл живёт в репозитории (data/); директория должна существовать
        # до первого подключения, иначе aiosqlite упадёт на open.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        enable_sqlite_fk(engine)
    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False: объекты остаются читаемыми после commit —
    # сервисы возвращают Item вызывающему коду без повторных запросов.
    return async_sessionmaker(engine, expire_on_commit=False)
