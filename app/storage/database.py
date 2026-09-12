from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    if url.startswith("sqlite"):
        # SQLite-файл живёт в репозитории (data/); директория должна существовать
        # до первого подключения, иначе aiosqlite упадёт на open.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False: объекты остаются читаемыми после commit —
    # сервисы возвращают Item вызывающему коду без повторных запросов.
    return async_sessionmaker(engine, expire_on_commit=False)
