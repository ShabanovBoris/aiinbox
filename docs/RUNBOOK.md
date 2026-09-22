# Runbook

Операционный runbook текущего Personal AI Inbox. Product behavior описан в
`PRODUCT_SPEC.md`, пользовательская поверхность — в `BOT_USAGE.md`.

## Быстрый старт

Требования: Python 3.12+, `uv`, `ffmpeg`.

```bash
uv venv --python 3.12
uv sync
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS
# и credentials/model ids выбранного LLM_PROVIDER
uv run python -m app.main
```

`app.main` автоматически выполняет `alembic upgrade head`.
Без `TELEGRAM_BOT_TOKEN` Telegram polling отключён, workers остаются активны.

Поддержаны `LLM_PROVIDER=openai` и `LLM_PROVIDER=openrouter`.
OpenRouter endpoint по умолчанию — `https://openrouter.ai/api/v1`.

## Docker

```bash
docker build -t personal-ai-inbox .
docker run --rm --env-file .env \
  -e DATABASE_URL=sqlite+aiosqlite:////data/app.db \
  -e TEMP_DIR=/tmp/aiinbox \
  -v aiinbox_data:/data \
  --tmpfs /tmp/aiinbox \
  personal-ai-inbox
```

Compose:

```bash
docker compose up --build -d
docker compose logs -f app
docker compose down
```

SQLite живёт в named volume `/data`; temporary media — tmpfs
`/tmp/aiinbox`. Container запускается non-root.

## Миграции

Проверить head:

```bash
uv run alembic heads
```

Применить вручную:

```bash
uv run alembic upgrade head
```

Application startup делает это автоматически.

## Quality gate

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions job `quality` выполняет тот же gate и является required status
check для protected `main`.

## Operational status / health

Локально:

```bash
uv run python -m app.ops status
```

В deployment:

```bash
docker compose exec -T app python -m app.ops status
docker compose ps
```

`status` не печатает credentials. Он показывает размер DB, QUEUED/PROCESSING/
FAILED, pending deliveries, configured provider/model, processing concurrency и
наличие Telegram config. Critical worker/polling task находится под fail-fast
supervisor: его неожиданное завершение роняет основной process; Compose
`restart: unless-stopped` поднимает его снова.

Compose healthcheck каждые 30 секунд выполняет:

```bash
python -m app.ops health
```

Это дешёвая DB/status-проверка. Полная SQLite-проверка структуры и foreign keys
запускается отдельно, чтобы не сканировать всю БД на каждом health interval:

```bash
uv run python -m app.ops verify
# Docker:
docker compose exec -T app python -m app.ops verify
```

## Backup rotation

Backup создаётся из live SQLite через Online Backup API, поэтому останавливать
workers для обычного snapshot не требуется:

```bash
uv run python -m app.ops backup
# Docker пишет snapshot в отдельный aiinbox_backups volume:
docker compose exec -T app python -m app.ops backup
```

Defaults: `BACKUP_DIR=./backups`, `BACKUP_KEEP=14`; в Compose
`BACKUP_DIR=/backups`. Каждый snapshot сначала проходит
`PRAGMA integrity_check` и `PRAGMA foreign_key_check`, и только после этого
выполняется rotation.

Пример ежедневного cron на VPS:

```cron
17 3 * * * cd /opt/aiinbox && docker compose exec -T app python -m app.ops backup
```

Cron должен запускаться от пользователя, у которого есть доступ к Docker.

Отдельный volume `aiinbox_backups` защищает от логических ошибок и неудачного
upgrade, но обычно остаётся на том же VPS/disk. Это **не disaster-recovery
copy**. До `v1.0-mvp` минимум одно актуальное поколение backup должно регулярно
реплицироваться off-host (другой host или S3-compatible storage). Конкретный
механизм sync можно внедрить отдельно, без изменения backup format.

## Restore drill / recovery

Restore намеренно не перезаписывает существующий файл. Сначала создаётся и
проверяется новый DB, затем оператор явно меняет canonical file.

```bash
docker compose stop app

# Выбрать snapshot из /backups и ещё раз проверить его.
docker compose run --rm --no-deps app \
  python -m app.ops verify --database /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db

# Восстановить в новый файл.
docker compose run --rm --no-deps app \
  python -m app.ops restore \
  --backup /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db \
  --target /data/app-restored.db

# Архивировать весь старый SQLite recovery set и только потом выбрать restored DB.
# Это гарантирует, что старые WAL/SHM sidecars не переживут canonical swap.
docker compose run --rm --no-deps app sh -c \
  'set -eu
   stamp=$(date -u +%Y%m%dT%H%M%SZ)
   mv /data/app.db /data/app.db.pre-restore.$stamp
   if [ -e /data/app.db-wal ]; then
     mv /data/app.db-wal /data/app.db-wal.pre-restore.$stamp
   fi
   if [ -e /data/app.db-shm ]; then
     mv /data/app.db-shm /data/app.db-shm.pre-restore.$stamp
   fi
   test ! -e /data/app.db-wal
   test ! -e /data/app.db-shm
   mv /data/app-restored.db /data/app.db'

docker compose up -d app
docker compose exec -T app python -m app.ops status
docker compose exec -T app python -m app.ops smoke
```

Если используется SQLite WAL, после остановки app не копируйте только `.db`
вручную как backup-процедуру: штатный `app.ops backup` делает консистентный
snapshot через SQLite API. Старый canonical recovery set
`app.db` + `app.db-wal` + `app.db-shm` храните вместе до завершения
проверки restored deployment.

## FTS maintenance

`item_search` — производная проекция. Для полного rebuild из canonical
`items/contents`:

```bash
uv run python -m app.ops rebuild-search
# Docker:
docker compose exec -T app python -m app.ops rebuild-search
```

## Live provider smoke

После deployment:

```bash
docker compose exec -T app python -m app.ops smoke
```

Команда использует текущий `LLM_PROVIDER`: выполняет короткий реальный запрос
через configured OpenAI/OpenRouter analysis model и Telegram `getMe`. Она
возвращает provider/model и bot username, но не credentials. Live smoke не входит
в default pytest/CI, чтобы тесты не зависели от внешней сети и production secrets.

## Production deploy recipe

Минимальный single-host deployment:

```bash
cd /opt/aiinbox
docker compose build
docker compose up -d
docker compose ps
docker compose exec -T app python -m app.ops status
docker compose exec -T app python -m app.ops smoke
docker compose exec -T app python -m app.ops backup
```

Для production обязательны persistent volumes `aiinbox_data` и
`aiinbox_backups`, restart policy из Compose и внешний cron для backup.
Перед обновлением, меняющим schema, сделайте verified backup.

## Диагностика Items

Default local DB: `data/app.db`.

```bash
sqlite3 data/app.db \
  "SELECT id, processing_status, processing_stage, state, source_type, error_code
   FROM items ORDER BY id DESC LIMIT 50"
```

FAILED:

```bash
sqlite3 data/app.db \
  "SELECT id, processing_stage, error_code, error_message
   FROM items WHERE processing_status='FAILED' ORDER BY id DESC"
```

На restart все stale `PROCESSING` автоматически возвращаются в `QUEUED`.
Обычный пользовательский retry делается кнопкой `🔁 Retry`.

Ручной fallback только для диагностики:

```sql
UPDATE items
SET processing_status='QUEUED', error_code=NULL, error_message=NULL
WHERE id=<id> AND processing_status='FAILED';
```

Не очищать `processing_stage`/contents: это resume checkpoints.

## Immediate deliveries

```bash
sqlite3 data/app.db \
  "SELECT id, kind, status, item_id, profile_update_job_id, attempts, updated_at
   FROM deliveries ORDER BY id DESC LIMIT 50"
```

`PENDING/SENDING` восстанавливаются delivery worker-ом/startup recovery.
`SENT` — зафиксированная успешная delivery state; transport semantics
at-least-once, поэтому crash сразу после Telegram send может дать дубль.

## Reminders / digest

```bash
sqlite3 data/app.db \
  "SELECT id, user_id, item_id, type, scheduled_at, status, sent_at
   FROM reminders ORDER BY scheduled_at DESC LIMIT 50"
```

Digest/snooze используют отдельную reminder semantics. Quiet hours и timezone
берутся из user settings.

## Profile updates

```bash
sqlite3 data/app.db \
  "SELECT id, user_id, status, error_code, created_at, updated_at
   FROM profile_update_jobs ORDER BY id DESC LIMIT 50"
```

Stale `RUNNING` job requeue'ится при startup.

## OpenRouter long STT

Файлы >25 MB или аудио >5 минут режутся `ffmpeg` на 5-минутные mono WAV PCM
16 kHz сегменты и транскрибируются с bounded concurrency. Segment checkpoints
содержат SHA-256 input + provider/model identity.

Если STT падает, сначала проверять:

- доступность `ffmpeg`;
- `OPENROUTER_API_KEY`;
- `OPENROUTER_TRANSCRIPTION_MODEL`;
- provider response в logs;
- наличие compatible `TRANSCRIPT_CHUNK` checkpoints.

## Web extraction / SSRF

Playwright fallback отключён. Слишком короткая страница может завершиться
`EXTRACTION_FAILED`; это ожидаемое поведение, а не повод включать unrestricted
browser.

`SECURITY_REJECTED` означает, что URL/IP/redirect нарушил SSRF policy.

## Shutdown / restart

SIGTERM/SIGINT:

1. stop new work;
2. workers получают bounded drain по `SHUTDOWN_TIMEOUT_SECONDS`;
3. зависшие tasks отменяются;
4. Telegram session и DB engine закрываются.

Critical worker/polling failure завершает процесс. Docker Compose
`restart: unless-stopped` поднимает его снова; durable state восстанавливается.
