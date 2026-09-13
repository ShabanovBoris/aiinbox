import asyncio
import logging
import signal
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app.bot.files import TelegramFileDownloader
from app.config import Settings
from app.domain.priority import PriorityEngine
from app.extractors.audio import AudioExtractor
from app.extractors.web import WebPageExtractor
from app.extractors.youtube import YoutubeExtractor
from app.llm.openai import OpenAiProvider
from app.llm.transcription import OpenAiTranscriptionProvider
from app.services.analysis import Analyzer
from app.services.processing import ProcessingPipeline
from app.services.profile import (
    apply_profile_seed,
    configure_profile_seed,
    requeue_running_profile_jobs,
)
from app.storage.database import make_engine, make_session_factory
from app.storage.models import User
from app.workers.processing import ProcessingWorker, requeue_stale
from app.workers.profile import ProfileUpdateWorker

log = logging.getLogger(__name__)


def run_migrations(database_url: str) -> None:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(cfg, "head")


def build_provider(settings: Settings) -> OpenAiProvider:
    # Точка единственной сборки провайдера; Ollama добавляется post-MVP своей веткой.
    if settings.llm_provider != "openai":
        raise SystemExit(f"Unsupported LLM_PROVIDER={settings.llm_provider!r}")
    if not settings.openai_api_key or not settings.openai_analysis_model:
        raise SystemExit("OPENAI_API_KEY and OPENAI_ANALYSIS_MODEL must be configured")
    return OpenAiProvider(
        settings.openai_api_key,
        settings.openai_analysis_model,
        settings.llm_timeout_seconds,
        vision_model=settings.openai_vision_model or None,
    )


def build_transcriber(settings: Settings) -> OpenAiTranscriptionProvider:
    # Whisper-эндпоинт — другой API/модель, поэтому отдельный adapter.
    if not settings.openai_api_key or not settings.openai_transcription_model:
        raise SystemExit("OPENAI_TRANSCRIPTION_MODEL must be configured for voice/audio")
    return OpenAiTranscriptionProvider(
        settings.openai_api_key,
        settings.openai_transcription_model,
        settings.transcription_timeout_seconds,
    )


def build_extractors(settings: Settings, bot) -> tuple:
    """Сборка экстракторов (composition root). audio требует живого Telegram bot;
    youtube STT-fallback использует transcriber, но сам extractor не требует bot."""
    web_extractor = WebPageExtractor(
        min_text_length=settings.min_extracted_text_length,
        timeout_seconds=settings.web_timeout_seconds,
        max_download_bytes=settings.max_download_bytes,
        max_redirects=settings.max_redirects,
        max_attempts=settings.web_max_attempts,
        backoff_seconds=settings.web_backoff_seconds,
    )
    youtube_extractor = YoutubeExtractor(
        transcriber=build_transcriber(settings),
        temp_dir=Path(settings.temp_dir) / "youtube",
        max_duration_seconds=settings.youtube_max_duration_seconds,
        max_audio_bytes=settings.youtube_max_audio_bytes,
        max_video_bytes=settings.youtube_max_video_bytes,
        max_subtitle_bytes=settings.youtube_max_subtitle_bytes,
        subtitle_langs=tuple(
            lang.strip() for lang in settings.subtitle_langs.split(",") if lang.strip()
        ),
        timeout_seconds=settings.web_timeout_seconds,
    )
    audio_extractor = None
    if bot is not None:
        downloader = TelegramFileDownloader(bot, settings.max_audio_bytes)
        audio_extractor = AudioExtractor(
            build_transcriber(settings), downloader, Path(settings.temp_dir)
        )
    return web_extractor, audio_extractor, youtube_extractor


def _profile_done_notifier(bot, session_factory):
    """Уведомление о завершении /profile_update: адресат — users.telegram_chat_id
    (job.user_id — внутренний PK). Возвращает coroutine или None (headless)."""

    async def notify(job, profile, changed):
        async with session_factory() as session:
            user = await session.get(User, job.user_id)
            chat_id = user.telegram_chat_id if user else None
        if chat_id is None:
            return
        await bot.send_message(chat_id, "Профиль обновлён: " + ", ".join(changed))

    return notify


def _item_failure_notifier(bot, session_factory):
    """Уведомляет о FAILED Item и оставляет пользователю кнопку Retry."""

    async def notify(item):
        from app.bot.notify import send_item_failure

        await send_item_failure(bot, session_factory, item)

    return notify


async def run(settings: Settings) -> None:
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        await requeue_stale(session_factory)
        await requeue_running_profile_jobs(session_factory)
        await apply_profile_seed(session_factory, settings.profile_seed_file)
        configure_profile_seed(settings.profile_seed_file)

        analyzer = Analyzer(build_provider(settings))
        polling = None
        bot = None
        on_result = None
        if settings.telegram_bot_token:
            # aiogram импортируется лениво: без токена приложение стартует чисто
            # воркерами — локальный smoke test не требует Telegram network.
            from aiogram import Bot, Dispatcher

            from app.bot.handlers import make_router
            from app.bot.notify import send_item_result

            bot = Bot(settings.telegram_bot_token)
            downloader = TelegramFileDownloader(bot, settings.max_audio_bytes)
            audio_extractor = AudioExtractor(
                build_transcriber(settings), downloader, Path(settings.temp_dir)
            )
            dispatcher = Dispatcher()
            dispatcher.include_router(
                make_router(settings, session_factory, settings.max_audio_bytes)
            )
            polling = asyncio.create_task(
                dispatcher.start_polling(bot, handle_signals=False), name="telegram-polling"
            )
            on_result = lambda item: send_item_result(bot, session_factory, item)  # noqa: E731
            log.info("telegram bot started")
        else:
            log.warning("TELEGRAM_BOT_TOKEN is empty — bot disabled, workers only")

        web_extractor, audio_extractor, youtube_extractor = build_extractors(settings, bot)
        pipeline = ProcessingPipeline(
            analyzer,
            PriorityEngine(),
            web_extractor,
            audio_extractor,
            youtube_extractor,
            visual_frame_interval_seconds=settings.video_frame_interval_seconds,
            visual_max_frames=settings.video_max_frames,
        )

        on_profile_done = _profile_done_notifier(bot, session_factory) if bot is not None else None
        on_failure = _item_failure_notifier(bot, session_factory) if bot is not None else None

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        profile_worker = ProfileUpdateWorker(
            session_factory,
            build_provider(settings),
            poll_seconds=settings.processing_poll_seconds,
            on_done=on_profile_done,
        )
        profile_tasks = [
            asyncio.create_task(profile_worker.run_forever(stop), name=f"profile-worker-{i}")
            for i in range(1)
        ]
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        worker_tasks = [
            asyncio.create_task(
                ProcessingWorker(
                    session_factory,
                    pipeline,
                    settings.processing_poll_seconds,
                    on_result,
                    on_failure,
                ).run_forever(stop),
                name=f"processing-worker-{i}",
            )
            for i in range(settings.processing_concurrency)
        ]

        await stop.wait()
        log.info("shutdown: stopping background tasks")
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
            if bot is not None:
                await bot.session.close()
        # Воркеры завершают текущий Item и выходят по stop; отмена только как страховка.
        for task in worker_tasks + profile_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, *profile_tasks, return_exceptions=True)
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
