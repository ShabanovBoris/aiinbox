import sqlite3

from alembic import command
from alembic.config import Config


def test_fresh_database_migrates_to_latest_schema(tmp_path):
    db = tmp_path / "fresh.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "items", "alembic_version"} <= tables

        # Идемпотентность Telegram-источника enforced схемой, не логикой:
        # дубль (user_id, telegram_message_id, source_index) запрещён на уровне БД.
        conn.execute("INSERT INTO users (telegram_user_id) VALUES (42)")
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status,"
            " state, source_type, processing_stage, user_note)"
            " VALUES (?, 1, 0, 'QUEUED', 'ACTIVE', 'TEXT', 'INGESTED', 'a')",
            (user_id,),
        )
        try:
            conn.execute(
                "INSERT INTO items (user_id, telegram_message_id, source_index,"
                " processing_status, state, source_type, processing_stage, user_note)"
                " VALUES (?, 1, 0, 'QUEUED', 'ACTIVE', 'TEXT', 'INGESTED', 'b')",
                (user_id,),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (user_id, telegram_message_id, source_index) allowed")
    finally:
        conn.close()
