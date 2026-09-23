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
        assert {
            "users",
            "items",
            "item_sources",
            "contents",
            "item_search",
            "events",
            "reminders",
            "deliveries",
            "alembic_version",
        } <= tables
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
        item_id = conn.execute("SELECT id FROM items").fetchone()[0]
        content_columns = {row[1] for row in conn.execute("PRAGMA table_info(contents)")}
        assert "source_id" in content_columns
        # Новый resumable STT checkpoint должен быть совместим именно с
        # production Alembic schema, а не только с Base.metadata.create_all.
        conn.execute(
            "INSERT INTO contents (item_id, kind, text) VALUES (?, 'TRANSCRIPT_CHUNK', 'part')",
            (item_id,),
        )
        assert conn.execute(
            "SELECT kind, text FROM contents WHERE item_id = ?",
            (item_id,),
        ).fetchone() == ("TRANSCRIPT_CHUNK", "part")
        document_item_id = conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 3, 0, 'QUEUED', 'ACTIVE', 'DOCUMENT', 'INGESTED', '') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        source_id = conn.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type) "
            "VALUES (?, 0, 'DOCUMENT') RETURNING id",
            (document_item_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO contents (item_id, source_id, kind, text) "
            "VALUES (?, ?, 'DOCUMENT_TEXT', 'durable document text')",
            (document_item_id, source_id),
        )
        assert conn.execute(
            "SELECT source_type FROM item_sources WHERE id = ?", (source_id,)
        ).fetchone() == ("DOCUMENT",)
        assert conn.execute(
            "SELECT kind, text FROM contents WHERE item_id = ?", (document_item_id,)
        ).fetchone() == ("DOCUMENT_TEXT", "durable document text")
        instagram_item_id = conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 4, 0, 'QUEUED', 'ACTIVE', 'INSTAGRAM', 'INGESTED', '') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        instagram_source_id = conn.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type, source_url) "
            "VALUES (?, 0, 'INSTAGRAM', 'https://www.instagram.com/reel/ABC/') RETURNING id",
            (instagram_item_id,),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT source_type FROM item_sources WHERE id = ?", (instagram_source_id,)
        ).fetchone() == ("INSTAGRAM",)
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

        # URL identity is now message-local: the same URL in another Telegram
        # message must not collapse two Items with different surrounding context.
        conn.execute(
            "UPDATE items SET source_url = 'https://example.com/shared' WHERE id = ?",
            (item_id,),
        )
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status,"
            " state, source_type, source_url, processing_stage, user_note)"
            " VALUES (?, 2, 0, 'QUEUED', 'ACTIVE', 'WEB',"
            " 'https://example.com/shared', 'INGESTED', '')",
            (user_id,),
        )
    finally:
        conn.close()


def test_instagram_source_migration_preserves_existing_items_and_sources(tmp_path):
    db = tmp_path / "instagram-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "d9f3b1a7c5e2")

    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (telegram_user_id) VALUES (42)")
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, source_url, processing_stage, user_note) "
            "VALUES (?, 1, 0, 'READY', 'ACTIVE', 'YOUTUBE', ?, 'READY', '') RETURNING id",
            (user_id, "https://www.youtube.com/watch?v=old"),
        ).fetchone()[0]
        source_id = conn.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type, source_url, "
            "extraction_status) VALUES (?, 0, 'YOUTUBE', ?, 'READY') RETURNING id",
            (item_id, "https://www.youtube.com/watch?v=old"),
        ).fetchone()[0]
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_type, source_url FROM items WHERE id = ?", (item_id,)
        ).fetchone() == ("YOUTUBE", "https://www.youtube.com/watch?v=old")
        assert conn.execute(
            "SELECT source_type, source_url, extraction_status FROM item_sources WHERE id = ?",
            (source_id,),
        ).fetchone() == ("YOUTUBE", "https://www.youtube.com/watch?v=old", "READY")
        conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 2, 0, 'QUEUED', 'ACTIVE', 'INSTAGRAM', 'INGESTED', '')",
            (user_id,),
        )
        new_item_id = conn.execute("SELECT max(id) FROM items").fetchone()[0]
        conn.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type) "
            "VALUES (?, 0, 'INSTAGRAM')",
            (new_item_id,),
        )


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
