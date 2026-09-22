import argparse
import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import build_provider
from app.services.retrieval import rebuild_user_search_index
from app.storage.database import make_engine, make_session_factory
from app.storage.models import User


def sqlite_database_path(database_url: str) -> Path:
    """Resolve the canonical SQLite file used by operational commands.

    Operations intentionally reject non-file databases: backup/restore tooling must
    act on the same durable SQLite source of truth as the application.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("DATABASE_URL must point to a file-backed SQLite database")
    return Path(url.database)


def verify_database(path: Path) -> None:
    """Validate a SQLite file before it is trusted as a backup or restore source.

    Full integrity_check is kept out of the hot application path and belongs to
    explicit maintenance, where a slower exhaustive check is acceptable.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity_check failed for {path}: {result!r}")


def backup_database(source: Path, destination: Path) -> None:
    """Create a consistent online SQLite snapshot without stopping workers.

    sqlite3.Connection.backup() is the storage boundary designed for live SQLite
    databases and includes committed WAL state in one consistent destination.
    """
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    failed = False
    try:
        source_connection.backup(destination_connection)
    except BaseException:
        failed = True
        raise
    finally:
        destination_connection.close()
        source_connection.close()
        if failed:
            destination.unlink(missing_ok=True)
    try:
        verify_database(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def restore_database(backup: Path, target: Path) -> None:
    """Restore into a new SQLite file so production replacement stays explicit.

    The target must not exist; operators can verify the restored file before
    swapping it into the canonical DATABASE_URL path while the app is stopped.
    """
    verify_database(backup)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_connection = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    failed = False
    try:
        backup_connection.backup(target_connection)
    except BaseException:
        failed = True
        raise
    finally:
        target_connection.close()
        backup_connection.close()
        if failed:
            target.unlink(missing_ok=True)
    try:
        verify_database(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def rotate_backups(directory: Path, keep: int) -> list[Path]:
    """Retain only the newest verified backup generations.

    Rotation is intentionally filename-scoped to aiinbox-*.db so maintenance
    cannot delete unrelated files placed in the same volume.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1")
    backups = sorted(
        directory.glob("aiinbox-*.db"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
    return removed


def database_status(path: Path) -> dict[str, int]:
    """Read cheap operational counters directly from canonical SQLite state.

    This is deliberately independent from Telegram and worker objects so the same
    status command remains useful during deployment and recovery.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        queued, processing, failed = connection.execute(
            """
            SELECT
              COALESCE(SUM(processing_status = 'QUEUED'), 0),
              COALESCE(SUM(processing_status = 'PROCESSING'), 0),
              COALESCE(SUM(processing_status = 'FAILED'), 0)
            FROM items
            """
        ).fetchone()
        pending_deliveries = connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE status IN ('PENDING', 'SENDING')"
        ).fetchone()[0]
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()
    return {
        "queued": int(queued),
        "processing": int(processing),
        "failed": int(failed),
        "pending_deliveries": int(pending_deliveries),
        "bytes": path.stat().st_size,
    }


async def rebuild_search(settings: Settings) -> int:
    """Rebuild derived FTS rows from canonical Item/Content data.

    The operation reuses the production retrieval service, so maintenance cannot
    drift from the indexing semantics used by /search.
    """
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        async with session_factory() as session:
            user_ids = list((await session.scalars(select(User.id))).all())
            for user_id in user_ids:
                await rebuild_user_search_index(session, user_id)
            await session.commit()
        return len(user_ids)
    finally:
        await engine.dispose()


async def smoke_external(settings: Settings) -> tuple[str, str]:
    """Exercise the configured LLM and Telegram adapters against real services.

    This command is intentionally opt-in because it performs live network calls
    and may incur a small provider charge; it is meant to run after deployment.
    """
    provider = build_provider(settings)
    summary = (await provider.summarize_chunk("Return a one-word acknowledgement.")).strip()
    if not summary:
        raise RuntimeError("LLM smoke returned an empty response")
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    from aiogram import Bot

    bot = Bot(settings.telegram_bot_token)
    try:
        identity = await bot.get_me()
    finally:
        await bot.session.close()

    model = (
        settings.openrouter_analysis_model
        if settings.llm_provider == "openrouter"
        else settings.openai_analysis_model
    )
    return model, identity.username or str(identity.id)


def build_parser() -> argparse.ArgumentParser:
    """Define one discoverable operational entry point for deployment tooling."""
    parser = argparse.ArgumentParser(prog="python -m app.ops")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create and verify an online SQLite backup")
    backup.add_argument("--output-dir", type=Path)
    backup.add_argument("--keep", type=int)

    verify = subparsers.add_parser("verify", help="run SQLite integrity_check")
    verify.add_argument("--database", type=Path)

    restore = subparsers.add_parser("restore", help="restore a backup into a new database file")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)

    subparsers.add_parser("status", help="show DB/queue/provider status without secrets")
    subparsers.add_parser("health", help="exit non-zero when the local DB/config is unhealthy")
    subparsers.add_parser("rebuild-search", help="rebuild derived FTS rows from canonical data")
    subparsers.add_parser("smoke", help="call the configured LLM and Telegram APIs")
    return parser


def main() -> None:
    """Route operator commands while keeping application startup unchanged."""
    args = build_parser().parse_args()
    settings = Settings()
    database = sqlite_database_path(settings.database_url)

    if args.command == "backup":
        output_dir = args.output_dir or Path(settings.backup_dir)
        keep = settings.backup_keep if args.keep is None else args.keep
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = output_dir / f"aiinbox-{stamp}.db"
        backup_database(database, destination)
        removed = rotate_backups(output_dir, keep)
        print(f"backup={destination} integrity=ok rotated={len(removed)}")
        return

    if args.command == "verify":
        verify_database(args.database or database)
        print(f"database={args.database or database} integrity=ok")
        return

    if args.command == "restore":
        restore_database(args.backup, args.target)
        print(f"restored={args.target} integrity=ok")
        return

    if args.command in {"status", "health"}:
        status = database_status(database)
        model = (
            settings.openrouter_analysis_model
            if settings.llm_provider == "openrouter"
            else settings.openai_analysis_model
        )
        print(
            f"database=ok bytes={status['bytes']} queued={status['queued']} "
            f"processing={status['processing']} failed={status['failed']} "
            f"pending_deliveries={status['pending_deliveries']} "
            f"provider={settings.llm_provider} model={model or 'not-configured'} "
            f"processing_workers={settings.processing_concurrency} "
            f"worker_supervision=fail-fast "
            f"telegram={'configured' if settings.telegram_bot_token else 'disabled'}"
        )
        return

    if args.command == "rebuild-search":
        users = asyncio.run(rebuild_search(settings))
        print(f"fts=rebuilt users={users}")
        return

    if args.command == "smoke":
        model, telegram = asyncio.run(smoke_external(settings))
        print(f"smoke=ok provider={settings.llm_provider} model={model} telegram={telegram}")


if __name__ == "__main__":
    main()
