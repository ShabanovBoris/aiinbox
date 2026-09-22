import sqlite3

from alembic import command
from alembic.config import Config


def _config(db) -> Config:
    """Run the production migration graph against an isolated SQLite database."""
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def test_composite_source_migration_backfills_legacy_web_checkpoint(tmp_path):
    db = tmp_path / "composite-upgrade.db"
    cfg = _config(db)
    command.upgrade(cfg, "7a6f2d1c9b84")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (telegram_user_id, telegram_chat_id) VALUES (42, 42)")
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, source_url, processing_stage, user_note) "
            "VALUES (?, 7, 0, 'READY', 'ACTIVE', 'WEB', ?, 'READY', 'прочитать')",
            (user_id, "https://example.com/article"),
        )
        item_id = conn.execute("SELECT id FROM items").fetchone()[0]
        conn.execute(
            "INSERT INTO contents (item_id, kind, text) VALUES (?, 'WEB_TEXT', 'article body')",
            (item_id,),
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        source = conn.execute(
            "SELECT id, source_type, source_url, extraction_status "
            "FROM item_sources WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        assert source is not None
        source_id, source_type, source_url, status = source
        assert (source_type, source_url, status) == (
            "WEB",
            "https://example.com/article",
            "READY",
        )
        assert conn.execute(
            "SELECT source_id FROM contents WHERE item_id = ? AND kind = 'WEB_TEXT'",
            (item_id,),
        ).fetchone() == (source_id,)
