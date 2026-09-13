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
        assert {"users", "items", "item_search", "events", "reminders", "alembic_version"} <= tables
        search_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='item_search'"
        ).fetchone()[0]
        assert "fts5" in search_sql

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


def test_existing_phase1_db_upgrades_with_data_intact(tmp_path):
    # Регрессия (Phase 2 review): существующая БД Phase 1 должна мигрировать на head
    # без потери данных и получить analysis-колонки.
    db = tmp_path / "upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")

    command.upgrade(cfg, "4cbfde82e2e8")  # schema Phase 1
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO users (telegram_user_id, telegram_chat_id) VALUES (42, 42)")
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status,"
            " state, source_type, processing_stage, user_note)"
            " VALUES (?, 7, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', 'Изучить AI agents')",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        assert {
            "title",
            "summary",
            "category",
            "item_type",
            "priority_score",
            "confidence",
            "completed_at",
            "archived_at",
            "snoozed_until",
        } <= columns
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert {"timezone", "settings_json", "daily_digest_enabled_at"} <= user_columns
        row = conn.execute(
            "SELECT u.telegram_user_id, i.user_note, i.processing_status FROM items i"
            " JOIN users u ON u.id = i.user_id"
        ).fetchone()
        assert row == (42, "Изучить AI agents", "READY")
    finally:
        conn.close()
