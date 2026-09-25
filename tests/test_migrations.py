import json
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
            "feedback_callback_receipts",
            "reminders",
            "deliveries",
            "alembic_version",
        } <= tables
        search_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='item_search'"
        ).fetchone()[0]
        assert "fts5" in search_sql
        assert "idempotency_key" in {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        event_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(events)")}
        assert event_columns["item_id"][3] == 0
        assert event_columns["reminder_id"][3] == 0
        assert {
            "ix_events_reminder_id",
            "uq_events_user_idempotency_key",
            "uq_events_reminder_event_type",
        } <= {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        assert {
            (row[2], row[3], row[4]) for row in conn.execute("PRAGMA foreign_key_list(events)")
        } >= {
            ("items", "item_id", "id"),
            ("reminders", "reminder_id", "id"),
        }
        reminder_columns = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
        assert {"claimed_at", "claim_generation"} <= reminder_columns
        assert "uq_reminders_open_proactive_user" in {
            row[1] for row in conn.execute("PRAGMA index_list(reminders)")
        }
        assert {
            "uq_reminders_motivation_slot",
            "uq_reminders_open_motivation_user",
        } <= {row[1] for row in conn.execute("PRAGMA index_list(reminders)")}

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
        conn.execute(
            "INSERT INTO contents (item_id, kind, text) VALUES (?, 'ATTENTION_HOOK', 'hook')",
            (item_id,),
        )
        assert conn.execute(
            "SELECT kind, text FROM contents WHERE item_id = ? AND kind = 'ATTENTION_HOOK'",
            (item_id,),
        ).fetchone() == ("ATTENTION_HOOK", "hook")
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


def test_reminder_feedback_migration_preserves_events_and_enforces_references(tmp_path):
    db = tmp_path / "reminder-feedback-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e09")

    with sqlite3.connect(db) as conn:
        user_id = conn.execute(
            "INSERT INTO users (telegram_user_id) VALUES (42) RETURNING id"
        ).fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (user_id, source_index, processing_status, state, source_type, "
            "processing_stage, user_note, category, item_type) "
            "VALUES (?, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', '', 'AI', 'READ') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type, payload_json, idempotency_key, "
            "created_at) VALUES (?, ?, 'USEFUL', '{}', 'telegram-callback:old', "
            "'2026-09-20 12:34:56')",
            (user_id, item_id),
        )
        reminder_id = conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status, sent_at) "
            "VALUES (?, NULL, 'MOTIVATION_NUDGE', '2026-09-24 00:00:01', 'SENT', "
            "'2026-09-24 09:00:00') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute(
            "SELECT item_id, reminder_id, created_at FROM events WHERE event_type='USEFUL'"
        ).fetchone() == (item_id, None, "2026-09-20 12:34:56")
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE event_type='REMINDER_SENT'").fetchone()[
                0
            ]
            == 0
        )  # No fabricated historical delivery events.

        conn.execute(
            "INSERT INTO events (user_id, item_id, reminder_id, event_type, payload_json) "
            "VALUES (?, NULL, ?, 'REMINDER_DISLIKED', '{}')",
            (user_id, reminder_id),
        )
        conn.commit()
        for sql, values, reason in (
            (
                "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
                "VALUES (?, NULL, NULL, 'CUSTOM')",
                (user_id,),
                "both Event references NULL",
            ),
            (
                "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
                "VALUES (?, 999999, NULL, 'CUSTOM')",
                (user_id,),
                "unknown Item foreign key",
            ),
            (
                "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
                "VALUES (?, NULL, 999999, 'CUSTOM')",
                (user_id,),
                "unknown Reminder foreign key",
            ),
        ):
            try:
                conn.execute(sql, values)
            except sqlite3.IntegrityError:
                conn.rollback()
            else:
                raise AssertionError(f"migration allowed {reason}")

        conn.execute(
            "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
            "VALUES (?, NULL, ?, 'REMINDER_DONE')",
            (user_id, reminder_id),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
                "VALUES (?, NULL, ?, 'REMINDER_DISLIKED')",
                (user_id, reminder_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("duplicate reminder/event type was allowed")

        # A distinct PM-11 outcome for the same Reminder remains valid.
        conn.execute(
            "INSERT INTO events (user_id, item_id, reminder_id, event_type) "
            "VALUES (?, NULL, ?, 'REMINDER_OPENED')",
            (user_id, reminder_id),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO events (user_id, item_id, event_type, idempotency_key) "
                "VALUES (?, ?, 'ARCHIVED', 'telegram-callback:old')",
                (user_id, item_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("existing PM-05 Event idempotency was not preserved")


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


def test_motivation_migration_preserves_settings_history_and_unique_slots(tmp_path):
    db = tmp_path / "motivation-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e08")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO users (telegram_user_id, settings_json) VALUES (?, ?)",
            (42, '{"attention_enabled":true,"attention_intensity":4}'),
        )
        existing_user_id = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = 42"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO users (telegram_user_id, settings_json) VALUES (?, ?)",
            (1000, '{"generic_motivation_enabled":true,"attention_intensity":2}'),
        )
        explicit_user_id = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = 1000"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status, sent_at) "
            "VALUES (?, NULL, 'DAILY_DIGEST', '2026-09-23 00:00:00', 'SENT', "
            "'2026-09-23 09:00:00')",
            (existing_user_id,),
        )

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        existing = json.loads(
            conn.execute(
                "SELECT settings_json FROM users WHERE id = ?", (existing_user_id,)
            ).fetchone()[0]
        )
        explicit = json.loads(
            conn.execute(
                "SELECT settings_json FROM users WHERE id = ?", (explicit_user_id,)
            ).fetchone()[0]
        )
        assert existing == {
            "attention_enabled": True,
            "attention_intensity": 4,
            "generic_motivation_enabled": False,
        }
        assert explicit == {
            "generic_motivation_enabled": True,
            "attention_intensity": 2,
        }
        assert conn.execute(
            "SELECT type, status FROM reminders WHERE user_id = ?", (existing_user_id,)
        ).fetchone() == ("DAILY_DIGEST", "SENT")

        slot = "2026-09-24 00:00:01"
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
            "VALUES (?, NULL, 'MOTIVATION_NUDGE', ?, 'SENT')",
            (existing_user_id, slot),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
                "VALUES (?, NULL, 'MOTIVATION_NUDGE', ?, 'SENT')",
                (existing_user_id, slot),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate user-level motivation slot was allowed")
        conn.rollback()

        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
            "VALUES (?, NULL, 'MOTIVATION_NUDGE', '2026-09-24 00:00:02', 'SENT')",
            (existing_user_id,),
        )
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
            "VALUES (?, NULL, 'MOTIVATION_NUDGE', '2026-09-24 00:00:03', 'CLAIMED')",
            (existing_user_id,),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
                "VALUES (?, NULL, 'MOTIVATION_NUDGE', '2026-09-24 00:00:04', 'PENDING')",
                (existing_user_id,),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("second open motivation claim was allowed")


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


def test_event_idempotency_migration_preserves_history_and_allows_legacy_nulls(tmp_path):
    db = tmp_path / "event-idempotency.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e04")

    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (telegram_user_id) VALUES (42)")
        first_user_id = conn.execute("SELECT id FROM users WHERE telegram_user_id = 42").fetchone()[
            0
        ]
        conn.execute("INSERT INTO users (telegram_user_id) VALUES (1000)")
        second_user_id = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = 1000"
        ).fetchone()[0]
        item_ids = []
        for user_id, message_id in ((first_user_id, 1), (first_user_id, 2), (second_user_id, 3)):
            item_ids.append(
                conn.execute(
                    "INSERT INTO items (user_id, telegram_message_id, source_index, "
                    "processing_status, state, source_type, processing_stage, user_note) "
                    "VALUES (?, ?, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', '') RETURNING id",
                    (user_id, message_id),
                ).fetchone()[0]
            )
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type, payload_json) "
            "VALUES (?, ?, 'DONE', '{}')",
            (first_user_id, item_ids[0]),
        )
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type, payload_json) "
            "VALUES (?, ?, 'SNOOZED', '{}')",
            (first_user_id, item_ids[1]),
        )

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        receipt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(feedback_callback_receipts)")
        }
        assert {"user_id", "idempotency_key", "created_at"} <= receipt_columns
        assert conn.execute(
            "SELECT event_type, payload_json, idempotency_key FROM events ORDER BY id"
        ).fetchall() == [("DONE", "{}", None), ("SNOOZED", "{}", None)]
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type) VALUES (?, ?, 'USEFUL')",
            (first_user_id, item_ids[0]),
        )
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type, idempotency_key) "
            "VALUES (?, ?, 'USEFUL', 'telegram-callback:one')",
            (first_user_id, item_ids[0]),
        )
        try:
            conn.execute(
                "INSERT INTO events (user_id, item_id, event_type, idempotency_key) "
                "VALUES (?, ?, 'NOT_INTERESTING', 'telegram-callback:one')",
                (first_user_id, item_ids[1]),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("duplicate per-user idempotency key was allowed")
        conn.execute(
            "INSERT INTO events (user_id, item_id, event_type, idempotency_key) "
            "VALUES (?, ?, 'USEFUL', 'telegram-callback:one')",
            (second_user_id, item_ids[2]),
        )
        conn.execute(
            "INSERT INTO feedback_callback_receipts (user_id, idempotency_key) "
            "VALUES (?, 'telegram-callback:noop')",
            (first_user_id,),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO feedback_callback_receipts (user_id, idempotency_key) "
                "VALUES (?, 'telegram-callback:noop')",
                (first_user_id,),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("duplicate callback receipt for one user was allowed")
        conn.execute(
            "INSERT INTO feedback_callback_receipts (user_id, idempotency_key) "
            "VALUES (?, 'telegram-callback:noop')",
            (second_user_id,),
        )


def test_attention_hook_migration_preserves_existing_content(tmp_path):
    """Upgrade a PM-08 database without rewriting or invalidating original Content rows."""
    db = tmp_path / "attention-hooks-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e07")

    previous_kinds = (
        "USER_TEXT",
        "WEB_TEXT",
        "TRANSCRIPT",
        "VISUAL_NOTES",
        "DESCRIPTION",
        "CHUNK_SUMMARY",
        "TRANSCRIPT_CHUNK",
        "DOCUMENT_TEXT",
    )
    with sqlite3.connect(db) as conn:
        user_id = conn.execute(
            "INSERT INTO users (telegram_user_id) VALUES (42) RETURNING id"
        ).fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 1, 0, 'READY', 'ACTIVE', 'WEB', 'READY', '') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.executemany(
            "INSERT INTO contents (item_id, kind, text) VALUES (?, ?, ?)",
            [(item_id, kind, f"existing {kind}") for kind in previous_kinds],
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT kind, text FROM contents WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall() == [(kind, f"existing {kind}") for kind in previous_kinds]
        conn.execute(
            "INSERT INTO contents (item_id, kind, text) VALUES (?, 'ATTENTION_HOOK', 'grounded')",
            (item_id,),
        )
        conn.commit()
        check_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='contents'"
        ).fetchone()[0]
        assert "ATTENTION_HOOK" in check_sql


def test_pm08_migration_preserves_settings_and_serializes_open_claims(tmp_path):
    db = tmp_path / "pm08-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e06")

    with sqlite3.connect(db) as conn:
        users = [
            (
                42,
                '{"daily_digest_enabled":false,"attention_intensity":5,'
                '"quiet_hours_start":"21:00"}',
            ),
            (
                1000,
                '{"attention_enabled":true,"attention_intensity":4,"daily_digest_enabled":false}',
            ),
            (2000, "{}"),
        ]
        user_ids = []
        for telegram_user_id, settings_json in users:
            user_ids.append(
                conn.execute(
                    "INSERT INTO users (telegram_user_id, settings_json) "
                    "VALUES (?, ?) RETURNING id",
                    (telegram_user_id, settings_json),
                ).fetchone()[0]
            )
        item_ids = []
        for user_id in user_ids:
            item_ids.append(
                conn.execute(
                    "INSERT INTO items (user_id, telegram_message_id, source_index, "
                    "processing_status, state, source_type, processing_stage, user_note) "
                    "VALUES (?, 1, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', '') RETURNING id",
                    (user_id,),
                ).fetchone()[0]
            )
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status, sent_at) "
            "VALUES (?, NULL, 'DAILY_DIGEST', '2026-09-24 00:00:00', 'SENT', "
            "'2026-09-24 09:00:00')",
            (user_ids[0],),
        )
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
            "VALUES (?, ?, 'SNOOZE_RESURFACE', '2026-09-25 00:00:00', 'PENDING')",
            (user_ids[0], item_ids[0]),
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        settings = conn.execute(
            "SELECT settings_json FROM users ORDER BY telegram_user_id"
        ).fetchall()
        assert [json.loads(row[0]) for row in settings] == [
            {
                "daily_digest_enabled": False,
                "attention_intensity": 5,
                "quiet_hours_start": "21:00",
                "attention_enabled": False,
                "generic_motivation_enabled": False,
            },
            {
                "attention_enabled": True,
                "attention_intensity": 4,
                "daily_digest_enabled": False,
                "generic_motivation_enabled": False,
            },
            {
                "attention_enabled": False,
                "attention_intensity": 3,
                "generic_motivation_enabled": False,
            },
        ]
        assert (
            conn.execute(
                "SELECT json_type(settings_json, '$.attention_enabled') FROM users "
                "WHERE telegram_user_id = 42"
            ).fetchone()[0]
            == "false"
        )
        assert {
            row[0]
            for row in conn.execute("SELECT type FROM reminders WHERE user_id = ?", (user_ids[0],))
        } == {"DAILY_DIGEST", "SNOOZE_RESURFACE"}
        assert {row[1] for row in conn.execute("PRAGMA table_info(reminders)")} >= {
            "claimed_at",
            "claim_generation",
        }

        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
            "VALUES (?, ?, 'PROACTIVE_ATTENTION', '2026-09-24 10:00:00', 'CLAIMED')",
            (user_ids[0], item_ids[0]),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
                "VALUES (?, ?, 'PROACTIVE_ATTENTION', '2026-09-24 10:01:00', 'PENDING')",
                (user_ids[0], item_ids[1]),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("two open proactive claims for one user were allowed")
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status, sent_at) "
            "VALUES (?, ?, 'PROACTIVE_ATTENTION', '2026-09-24 10:02:00', 'SENT', "
            "'2026-09-24 10:02:00')",
            (user_ids[0], item_ids[0]),
        )
        conn.execute(
            "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status, sent_at) "
            "VALUES (?, ?, 'PROACTIVE_ATTENTION', '2026-09-24 10:03:00', 'SENT', "
            "'2026-09-24 10:03:00')",
            (user_ids[0], item_ids[1]),
        )
        conn.commit()


def test_ask_migration_preserves_existing_deliveries_and_enforces_source_identity(tmp_path):
    db = tmp_path / "ask-upgrade.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "f5a7c2d91e10")

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        user_id = conn.execute(
            "INSERT INTO users (telegram_user_id) VALUES (42) RETURNING id"
        ).fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (user_id, telegram_message_id, source_index, processing_status, "
            "state, source_type, processing_stage, user_note) "
            "VALUES (?, 1, 0, 'READY', 'ACTIVE', 'TEXT', 'READY', '') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        profile_job_id = conn.execute(
            "INSERT INTO profile_update_jobs (user_id, instruction, status) "
            "VALUES (?, 'change goals', 'DONE') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO deliveries (user_id, item_id, type, status, attempts, payload_json, "
            "last_error, sent_at) VALUES (?, ?, 'ITEM_READY', 'SENT', 2, ?, 'old transport error', "
            "'2026-09-24 12:00:00')",
            (user_id, item_id, '{"title":"old item"}'),
        )
        conn.execute(
            "INSERT INTO deliveries (user_id, profile_update_job_id, type, status, attempts, "
            "payload_json) VALUES (?, ?, 'PROFILE_UPDATED', 'PENDING', 1, ?)",
            (user_id, profile_job_id, '{"changed":["goals"]}'),
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        old_deliveries = conn.execute(
            "SELECT type, status, item_id, profile_update_job_id, attempts, payload_json, "
            "last_error, sent_at FROM deliveries ORDER BY id"
        ).fetchall()
        assert old_deliveries == [
            (
                "ITEM_READY",
                "SENT",
                item_id,
                None,
                2,
                '{"title":"old item"}',
                "old transport error",
                "2026-09-24 12:00:00",
            ),
            (
                "PROFILE_UPDATED",
                "PENDING",
                None,
                profile_job_id,
                1,
                '{"changed":["goals"]}',
                None,
                None,
            ),
        ]
        ask_job_id = conn.execute(
            "INSERT INTO ask_jobs (user_id, telegram_message_id, question, status) "
            "VALUES (?, 77, 'what did I save?', 'PENDING') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO deliveries (user_id, ask_job_id, type, status, payload_json) "
            "VALUES (?, ?, 'ASK_RESULT', 'PENDING', ?)",
            (user_id, ask_job_id, '{"answer":"answer","references":[]}'),
        )
        conn.commit()
        assert conn.execute(
            "SELECT type, ask_job_id FROM deliveries WHERE ask_job_id = ?",
            (ask_job_id,),
        ).fetchone() == ("ASK_RESULT", ask_job_id)

        invalid_rows = [
            (
                "INSERT INTO deliveries (user_id, type) VALUES (?, 'INVALID_NONE')",
                (user_id,),
            ),
            (
                "INSERT INTO deliveries (user_id, item_id, ask_job_id, type) "
                "VALUES (?, ?, ?, 'INVALID_TWO')",
                (user_id, item_id, ask_job_id),
            ),
            (
                "INSERT INTO deliveries (user_id, ask_job_id, type) VALUES (?, ?, 'ASK_RESULT')",
                (user_id, ask_job_id),
            ),
        ]
        for statement, parameters in invalid_rows:
            try:
                conn.execute(statement, parameters)
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
            else:
                raise AssertionError("invalid or duplicate Ask delivery was allowed")


def test_fresh_ask_schema_contains_job_and_delivery_constraints(tmp_path):
    db = tmp_path / "ask-fresh.db"
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "ask_jobs" in tables
        assert "ask_job_id" in {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
        assert {"ix_ask_jobs_status", "ix_deliveries_ask_job_id"} <= {
            row[1]
            for table in ("ask_jobs", "deliveries")
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
        ask_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ask_jobs'"
        ).fetchone()[0]
        delivery_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'"
        ).fetchone()[0]
        assert "uq_ask_jobs_user_telegram_message" in ask_sql
        assert "uq_deliveries_ask_job_type" in delivery_sql
        assert ("ask_jobs", "ask_job_id", "id") in {
            (row[2], row[3], row[4]) for row in conn.execute("PRAGMA foreign_key_list(deliveries)")
        }
        user_id = conn.execute(
            "INSERT INTO users (telegram_user_id) VALUES (42) RETURNING id"
        ).fetchone()[0]
        ask_job_id = conn.execute(
            "INSERT INTO ask_jobs (user_id, telegram_message_id, question) "
            "VALUES (?, 77, 'fresh database question') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO deliveries (user_id, ask_job_id, type, payload_json) "
            "VALUES (?, ?, 'ASK_RESULT', ?)",
            (user_id, ask_job_id, '{"answer":"fresh schema"}'),
        )
        assert conn.execute(
            "SELECT status FROM ask_jobs WHERE id = ?", (ask_job_id,)
        ).fetchone() == ("PENDING",)
