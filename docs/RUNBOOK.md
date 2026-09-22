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
