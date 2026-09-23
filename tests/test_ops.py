import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.enums import ProcessingStatus, SourceType
from app.ops import (
    backup_database,
    create_backup_generation,
    database_status,
    rebuild_search,
    restore_database,
    rotate_backups,
    smoke_external,
    sqlite_database_path,
    verify_backup_copy,
    verify_database,
)
from app.storage.database import make_engine
from app.storage.models import Item, User


def test_sqlite_database_path_handles_container_absolute_path():
    """Operational tooling must resolve the same SQLite path syntax used by Docker."""
    assert sqlite_database_path("sqlite+aiosqlite:////data/app.db") == Path("/data/app.db")


async def test_application_sqlite_engine_uses_bounded_lock_wait(tmp_path):
    """The production engine waits briefly for another worker's SQLite write."""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'database.db'}",
    )
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA busy_timeout")
            assert result.scalar_one() == 30_000
    finally:
        await engine.dispose()


def test_backup_restore_roundtrip_includes_committed_wal(tmp_path):
    """Online backup must preserve committed data even while the source DB stays open."""
    source = tmp_path / "source.db"
    backup = tmp_path / "backups" / "snapshot.db"
    restored = tmp_path / "restored.db"

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        writer.execute("INSERT INTO notes(body) VALUES ('durable')")
        writer.commit()

        backup_database(source, backup)
        restore_database(backup, restored)
    finally:
        writer.close()

    verify_database(restored)
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("SELECT body FROM notes").fetchone() == ("durable",)
    finally:
        connection.close()


def test_restore_refuses_to_overwrite_existing_database(tmp_path):
    """Recovery stays reviewable by restoring only into a new target file."""
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    connection = sqlite3.connect(backup)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.commit()
    connection.close()
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        restore_database(backup, target)

    assert target.read_bytes() == b"existing"


def test_backup_generation_checksum_detects_transfer_corruption(tmp_path):
    """Off-host copies must fail before restore when transferred bytes changed."""
    source = tmp_path / "source.db"
    backup = tmp_path / "aiinbox-test.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.execute("INSERT INTO sample(value) VALUES ('durable')")
    connection.commit()
    connection.close()

    checksum = create_backup_generation(source, backup)
    verify_backup_copy(backup, checksum)

    backup.write_bytes(backup.read_bytes() + b"corruption")

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        verify_backup_copy(backup, checksum)


def test_verify_database_rejects_foreign_key_orphans(tmp_path):
    """Verified backup must include referential integrity, not only page integrity."""
    database = tmp_path / "orphan.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO child(parent_id) VALUES (999)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="foreign_key_check"):
        verify_database(database)


def test_backup_rotation_removes_only_old_aiinbox_generations(tmp_path):
    """Rotation must keep unrelated files and only bound generated backup history."""
    backups = []
    for index in range(3):
        path = tmp_path / f"aiinbox-{index}.db"
        path.write_bytes(str(index).encode())
        Path(f"{path}.sha256").write_text(f"{index}\n")
        os.utime(path, (index + 1, index + 1))
        backups.append(path)
    unrelated = tmp_path / "manual.db"
    unrelated.write_bytes(b"keep")

    removed = rotate_backups(tmp_path, keep=2)

    assert removed == [backups[0]]
    assert not backups[0].exists()
    assert not Path(f"{backups[0]}.sha256").exists()
    assert backups[1].exists() and backups[2].exists()
    assert Path(f"{backups[1]}.sha256").exists()
    assert Path(f"{backups[2]}.sha256").exists()
    assert unrelated.exists()


def test_database_status_reports_queue_failures_and_size(tmp_path):
    """Status reads only durable operational state and never needs provider secrets."""
    database = tmp_path / "status.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE items(processing_status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO items(processing_status) VALUES (?)",
            [("QUEUED",), ("QUEUED",), ("PROCESSING",), ("FAILED",)],
        )
        connection.execute("CREATE TABLE deliveries(status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO deliveries(status) VALUES (?)",
            [("PENDING",), ("SENDING",), ("SENT",)],
        )
        connection.commit()
    finally:
        connection.close()

    status = database_status(database)

    assert status["queued"] == 2
    assert status["processing"] == 1
    assert status["failed"] == 1
    assert status["pending_deliveries"] == 2
    assert status["bytes"] > 0


async def test_rebuild_search_uses_canonical_items(settings, session_factory):
    """Maintenance must rebuild FTS through the same indexing semantics as /search."""
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add(
            Item(
                user_id=user.id,
                processing_status=ProcessingStatus.READY,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="searchable maintenance text",
                title="Maintenance",
            )
        )
        await session.commit()

    assert await rebuild_search(settings) == 1

    database = sqlite_database_path(settings.database_url)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_search WHERE item_search MATCH 'maintenance'"
        ).fetchone() == (1,)
    finally:
        connection.close()


async def test_external_smoke_exercises_provider_and_telegram(settings, monkeypatch):
    """Deployment smoke verifies both configured external boundaries without persistence."""

    class FakeProvider:
        """Fake provider preserves the live smoke contract without network in default tests."""

        async def summarize_chunk(self, text):
            return "OK"

    class FakeSession:
        """Fake Telegram session lets the smoke path verify required cleanup."""

        closed = False

        async def close(self):
            self.closed = True

    fake_session = FakeSession()

    class FakeBot:
        """Fake bot models Telegram getMe while keeping credentials out of tests."""

        def __init__(self, token):
            assert token == "test-token"
            self.session = fake_session

        async def get_me(self):
            return SimpleNamespace(username="aiinbox_test_bot", id=123)

    settings.telegram_bot_token = "test-token"
    settings.openai_analysis_model = "test-analysis-model"
    monkeypatch.setattr("app.ops.build_provider", lambda _settings: FakeProvider())
    monkeypatch.setattr("aiogram.Bot", FakeBot)

    model, telegram = await smoke_external(settings)

    assert model == "test-analysis-model"
    assert telegram == "aiinbox_test_bot"
    assert fake_session.closed is True
