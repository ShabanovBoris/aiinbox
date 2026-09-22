import sqlite3

import pytest
from alembic import command
from alembic.config import Config


def _alembic_config(db) -> Config:
    """Build the same migration entry point used by production for a temp DB."""
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def _insert_item(conn: sqlite3.Connection, note: str = "before PM-01") -> int:
    """Create a pre-PM-01 Item so the migration's backfill is observable."""
    conn.execute("INSERT INTO users (telegram_user_id, telegram_chat_id) VALUES (42, 42)")
    user_id = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute(
        "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
        "state, source_type, processing_stage, user_note) "
        "VALUES (?, 7, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', ?)",
        (user_id, note),
    )
    conn.commit()
    return conn.execute("SELECT id FROM items").fetchone()[0]


def test_interest_migration_backfills_existing_item(tmp_path):
    db = tmp_path / "interest-upgrade.db"
    cfg = _alembic_config(db)
    command.upgrade(cfg, "5d8e9a1b2c3d")

    with sqlite3.connect(db) as conn:
        item_id = _insert_item(conn)

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT user_note, processing_status, interest_level FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        assert row == ("before PM-01", "READY", 2)


def test_interest_migration_default_and_check_constraint(tmp_path):
    db = tmp_path / "interest-fresh.db"
    cfg = _alembic_config(db)
    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        item_id = _insert_item(conn, "fresh")
        assert conn.execute(
            "SELECT interest_level FROM items WHERE id = ?", (item_id,)
        ).fetchone() == (2,)

        for invalid_level in (0, 4):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE items SET interest_level = ? WHERE id = ?",
                    (invalid_level, item_id),
                )
