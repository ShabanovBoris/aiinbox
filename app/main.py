import asyncio
import logging
import signal

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app.config import Settings
from app.storage.database import make_engine, make_session_factory
from app.workers.processing import ProcessingWorker, requeue_stale

log = logging.getLogger(__name__)


def run_migrations(database_url: str) -> None:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(cfg, "head")


async def run(settings: Settings) -> None:
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        await requeue_stale(session_factory)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        worker_tasks = [
            asyncio.create_task(
                ProcessingWorker(session_factory, settings.processing_poll_seconds).run_forever(
                    stop
                ),
                name=f"processing-worker-{i}",
            )
            for i in range(settings.processing_concurrency)
        ]

        polling = None
        bot = None
        if settings.telegram_bot_token:
            # aiogram импортируется лениво: без токена приложение стартует чисто
            # воркерами — локальный smoke test не требует Telegram network.
            from aiogram import Bot, Dispatcher

            from app.bot.handlers import make_router

            bot = Bot(settings.telegram_bot_token)
            dispatcher = Dispatcher()
            dispatcher.include_router(make_router(settings, session_factory))
            polling = asyncio.create_task(
                dispatcher.start_polling(bot, handle_signals=False), name="telegram-polling"
            )
            log.info("telegram bot started")
        else:
            log.warning("TELEGRAM_BOT_TOKEN is empty — bot disabled, workers only")

        await stop.wait()
        log.info("shutdown: stopping background tasks")
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
            if bot is not None:
                await bot.session.close()
        # Воркеры завершают текущий Item и выходят по stop; отмена только как страховка.
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
    finally:
        await engine.dispose()
        log.info("shutdown complete")


def main() -> None:  # pragma: no cover — точка входа процесса
    settings = Settings()
    run_migrations(settings.database_url)
    # basicConfig(force=True) после миграций: fileConfig из alembic env.py ставит
    # root на WARNING и подменяет хендлеры — обычный basicConfig был бы no-op.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
