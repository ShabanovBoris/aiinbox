import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.storage.database import enable_sqlite_fk
from app.storage.models import Base


@pytest.fixture
def settings(tmp_path):
    # _env_file=None: тесты не зависят от реального .env; init kwargs приоритетнее окружения
    return Settings(
        _env_file=None,
        telegram_bot_token="",
        allowed_telegram_user_ids="42,1000",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
    )


@pytest.fixture
async def engine(settings):
    # FK-pragma как в продовом make_engine: тесты проверяют те же инварианты БД
    engine = enable_sqlite_fk(create_async_engine(settings.database_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
