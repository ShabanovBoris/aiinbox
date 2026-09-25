import sqlite3

import pytest
from alembic import command
from alembic.config import Config


def _alembic_config(database) -> Config:
    """Use production migrations against an isolated file-backed SQLite database."""
    config = Config()
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    return config


def test_export_migration_preserves_old_deliveries_and_enforces_four_source_identity(tmp_path):
    database = tmp_path / "export-upgrade.db"
    config = _alembic_config(database)
    command.upgrade(config, "f5a7c2d91e11")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        user_id = connection.execute(
            "INSERT INTO users (telegram_user_id) VALUES (42) RETURNING id"
        ).fetchone()[0]
        item_id = connection.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 10, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', '') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        profile_job_id = connection.execute(
            "INSERT INTO profile_update_jobs (user_id, instruction, status) "
            "VALUES (?, 'change goals', 'DONE') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        ask_job_id = connection.execute(
            "INSERT INTO ask_jobs (user_id, telegram_message_id, question, status) "
            "VALUES (?, 11, 'what did I save?', 'DONE') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO deliveries (user_id, item_id, type, status, attempts, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (user_id, item_id, "ITEM_READY", "SENT", 2, '{"title":"old item"}'),
            ],
        )
        connection.execute(
            "INSERT INTO deliveries (user_id, profile_update_job_id, type, status, attempts, "
            "payload_json) VALUES (?, ?, 'PROFILE_UPDATED', 'PENDING', 1, ?)",
            (user_id, profile_job_id, '{"changed":["goals"]}'),
        )
        connection.execute(
            "INSERT INTO deliveries (user_id, ask_job_id, type, status, attempts, payload_json) "
            "VALUES (?, ?, 'ASK_RESULT', 'PENDING', 1, ?)",
            (user_id, ask_job_id, '{"answer":"old answer"}'),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        old_rows = connection.execute(
            "SELECT type, status, item_id, profile_update_job_id, ask_job_id, export_job_id, "
            "attempts, payload_json FROM deliveries ORDER BY id"
        ).fetchall()
        assert old_rows == [
            ("ITEM_READY", "SENT", item_id, None, None, None, 2, '{"title":"old item"}'),
            (
                "PROFILE_UPDATED",
                "PENDING",
                None,
                profile_job_id,
                None,
                None,
                1,
                '{"changed":["goals"]}',
            ),
            ("ASK_RESULT", "PENDING", None, None, ask_job_id, None, 1, '{"answer":"old answer"}'),
        ]
        export_job_id = connection.execute(
            "INSERT INTO export_jobs (user_id, telegram_message_id, mode, status) "
            "VALUES (?, 12, 'FULL', 'DONE') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO deliveries (user_id, export_job_id, type, status, payload_json) "
            "VALUES (?, ?, 'EXPORT_FILE', 'PENDING', '{\"artifact_name\":\"aiinbox-export.zip\"}')",
            (user_id, export_job_id),
        )
        connection.commit()

        # Delivery must reference exactly one business owner for all four variants.
        invalid_rows = [
            (None, None, None, None),
            (item_id, profile_job_id, None, None),
            (None, None, ask_job_id, export_job_id),
        ]
        for item_ref, profile_ref, ask_ref, export_ref in invalid_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO deliveries (user_id, item_id, profile_update_job_id, ask_job_id, "
                    "export_job_id, type) VALUES (?, ?, ?, ?, ?, 'INVALID')",
                    (user_id, item_ref, profile_ref, ask_ref, export_ref),
                )
                connection.commit()
            connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO deliveries (user_id, export_job_id, type) "
                "VALUES (?, ?, 'EXPORT_FILE')",
                (user_id, export_job_id),
            )
            connection.commit()
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO export_jobs (user_id, telegram_message_id, mode) "
                "VALUES (?, 12, 'COMPACT')",
                (user_id,),
            )
            connection.commit()
        connection.rollback()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RuntimeError, match="ExportJob rows or export deliveries"):
        command.downgrade(config, "f5a7c2d91e11")
