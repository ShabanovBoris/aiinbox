import sqlite3

from alembic import command
from alembic.config import Config


def _config(db) -> Config:
    """Use the production Alembic entry point against an isolated SQLite file."""
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def test_source_metadata_migration_preserves_existing_items(tmp_path):
    db = tmp_path / "forwarded-upgrade.db"
    cfg = _config(db)
    command.upgrade(cfg, "f4c1a9d2e7b0")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (telegram_user_id, telegram_chat_id) VALUES (42, 42)")
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 7, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', 'existing')",
            (user_id,),
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        assert "source_metadata_json" in columns
        assert conn.execute("SELECT user_note, source_metadata_json FROM items").fetchone() == (
            "existing",
            None,
        )
