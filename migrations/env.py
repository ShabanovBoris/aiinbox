import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import Settings
from app.storage.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: programmatic запуск из app.main не должен
    # глушить настроенное логирование приложения.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# URL: главное — то, что передал вызывающий (main.py / тесты), иначе конфиг приложения.
url = config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # render_as_batch: SQLite не умеет большинство ALTER — batch mode пересоздаёт
    # таблицы; обязателен для будущих миграций схемы.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
