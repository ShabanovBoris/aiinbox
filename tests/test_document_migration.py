import sqlite3

from alembic import command
from alembic.config import Config


def _config(db) -> Config:
    """Run the production migration graph against an isolated SQLite database."""
    config = Config()
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return config


def test_existing_document_related_data_survives_enum_constraint_migration(tmp_path):
    db = tmp_path / "document-migration.db"
    config = _config(db)
    command.upgrade(config, "c2d4e6f8a0b1")
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO users (telegram_user_id, telegram_chat_id) VALUES (42, 42)")
        user_id = connection.execute("SELECT id FROM users").fetchone()[0]
        connection.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 7, 0, 'READY', 'ACTIVE', 'WEB', 'READY', 'open the source')",
            (user_id,),
        )
        item_id = connection.execute("SELECT id FROM items").fetchone()[0]
        connection.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type, extraction_status) "
            "VALUES (?, 0, 'WEB', 'READY')",
            (item_id,),
        )
        source_id = connection.execute("SELECT id FROM item_sources").fetchone()[0]
        connection.execute(
            "INSERT INTO contents (item_id, source_id, kind, text) "
            "VALUES (?, ?, 'WEB_TEXT', 'existing source checkpoint')",
            (item_id, source_id),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(db) as connection:
        item = connection.execute(
            "SELECT source_type, user_note FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        source = connection.execute(
            "SELECT source_type, extraction_status FROM item_sources WHERE id = ?", (source_id,)
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT kind, text, source_id FROM contents WHERE item_id = ?", (item_id,)
        ).fetchone()
        assert item == ("WEB", "open the source")
        assert source == ("WEB", "READY")
        assert checkpoint == ("WEB_TEXT", "existing source checkpoint", source_id)

        # The new enum values and all previous ones must be accepted by migrated checks.
        connection.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 8, 0, 'QUEUED', 'ACTIVE', 'DOCUMENT', 'INGESTED', '')",
            (connection.execute("SELECT id FROM users WHERE telegram_user_id = 42").fetchone()[0],),
        )
        new_item_id = connection.execute(
            "SELECT id FROM items WHERE telegram_message_id = 8"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO item_sources (item_id, source_index, source_type) VALUES (?, 0, 'VIDEO')",
            (new_item_id,),
        )
        new_source_id = connection.execute(
            "SELECT id FROM item_sources WHERE item_id = ?", (new_item_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO contents (item_id, source_id, kind, text) "
            "VALUES (?, ?, 'DOCUMENT_TEXT', 'new checkpoint')",
            (new_item_id, new_source_id),
        )
        assert connection.execute(
            "SELECT source_type FROM item_sources WHERE id = ?", (new_source_id,)
        ).fetchone() == ("VIDEO",)
        assert connection.execute(
            "SELECT kind FROM contents WHERE item_id = ?", (new_item_id,)
        ).fetchone() == ("DOCUMENT_TEXT",)
