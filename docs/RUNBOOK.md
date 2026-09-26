# Runbook

Операционный runbook текущего Personal AI Inbox. Product behavior описан в
`PRODUCT_SPEC.md`, пользовательская поверхность — в `BOT_USAGE.md`.

## Быстрый старт

Требования: Python 3.12+, `uv`, `ffmpeg`/`ffprobe`.
Для production off-host backup дополнительно нужны Docker Compose, `rsync` и
SSH; `rsync` должен быть установлен и на backup host.

```bash
uv venv --python 3.12
uv sync
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS
# и credentials/model ids выбранного LLM_PROVIDER
uv run python -m app.main
```

`app.main` автоматически выполняет `alembic upgrade head`.
Без `TELEGRAM_BOT_TOKEN` Telegram polling отключён, workers остаются активны;
export generation также работает без Telegram, а готовая Delivery ждёт включения бота.

`EXPORT_DIR` (по умолчанию `./exports`) отделён от `BACKUP_DIR` и содержит
временные пользовательские ZIP-артефакты. `EXPORT_RETENTION_SECONDS` задаёт
retention abandoned/terminal artifacts (по умолчанию 86400 секунд), а
`MAX_EXPORT_CONTENT_CHARS` ограничивает суммарный full Content snapshot
(по умолчанию 10000000 символов).

Документы используют `MAX_DOCUMENT_BYTES` (по умолчанию 20 MB) и
`MAX_DOCUMENT_TEXT_CHARS` (500 000 символов). URL PDF проходит через
`MAX_DOWNLOAD_BYTES` и существующую SSRF-safe web boundary; отдельный URL
загрузчик и OCR не используются.

Поддержаны `LLM_PROVIDER=openai` и `LLM_PROVIDER=openrouter`.
OpenRouter endpoint по умолчанию — `https://openrouter.ai/api/v1`.

Instagram Reels обрабатываются без авторизации по умолчанию. При необходимости
оператор может вручную положить yt-dlp cookies в файл вне репозитория и указать
`INSTAGRAM_COOKIES_FILE=/absolute/path/to/cookies.txt`. Ограничьте права файла
владельцем приложения; не добавляйте cookies в Git, SQLite backup или export.
Файл не читается из browser profile и его содержимое не логируется. Defaults:
`INSTAGRAM_MAX_DURATION_SECONDS=7200`, `INSTAGRAM_MAX_AUDIO_BYTES=50000000` и
`INSTAGRAM_MAX_VIDEO_BYTES=50000000`.

## Docker

```bash
docker build -t personal-ai-inbox .
docker run --rm --env-file .env \
  -e DATABASE_URL=sqlite+aiosqlite:////data/app.db \
  -e EXPORT_DIR=/exports \
  -e TEMP_DIR=/tmp/aiinbox \
  -v aiinbox_data:/data \
  -v aiinbox_exports:/exports \
  --tmpfs /tmp/aiinbox \
  personal-ai-inbox
```

Compose:

```bash
docker compose up --build -d
docker compose logs -f app
docker compose down
```

SQLite живёт в named volume `/data`; portable exports — в отдельном
`aiinbox_exports` volume `/exports`; temporary media — tmpfs `/tmp/aiinbox`.
Container запускается non-root.

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

Downgrade с `c2d4e6f8a0b1` теряет composite source rows. Если несколько Items
ссылаются на один URL пользователя, legacy-ограничение позволяет сохранить URL
только у первого Item; у остальных `source_url` будет обнулён. Это schema
rollback, а не полное восстановление прежней модели данных.

Migration `f5a7c2d91e04` добавляет `INSTAGRAM` в SQLite CHECK constraints и
является forward-only для БД, где уже появились реальные `INSTAGRAM` rows.
Прямой downgrade к предыдущей schema восстановит CHECK без `INSTAGRAM` и может
завершиться ошибкой, пока такие rows существуют. Штатный rollback production
делайте через verified backup, созданный до schema upgrade; не полагайтесь на
Alembic downgrade как на восстановление данных PM-04.

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
FAILED, pending deliveries, Ask PENDING/RUNNING/FAILED, unresolved `ASK_FAILED`
delivery rows, configured provider/model, processing concurrency и наличие Telegram
config. Счётчики читают только состояния очереди, не вопросы, ответы или Content.
Critical worker/polling task находится под fail-fast
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
получает `.sha256` sidecar и участвует в rotation.

Пример ежедневного cron на VPS:

```cron
17 3 * * * cd /opt/aiinbox && docker compose exec -T app python -m app.ops backup
```

Cron должен запускаться от пользователя, у которого есть доступ к Docker.

Отдельный volume `aiinbox_backups` защищает от логических ошибок и неудачного
upgrade, но обычно остаётся на том же VPS/disk. Это **не disaster-recovery
copy**.

## Off-host replication + recovery drill

Минимальная production-схема использует отдельный SSH host/failure domain.
На backup host заранее создайте каталог, доступный отдельному backup-user, и
настройте non-interactive SSH key через обычный OpenSSH config. Приложению этот
ключ и remote credentials не передаются.

На deployment host:

```bash
cd /opt/aiinbox
OFFSITE_BACKUP_TARGET='backup@example:/srv/aiinbox' ./scripts/offsite_backup.sh
```

`OFFSITE_BACKUP_TARGET` должен быть rsync-over-SSH target вида
`user@host:/absolute/path`; remote directory должен существовать. Скрипт:

1. создаёт новый verified SQLite backup + `.sha256`;
2. копирует exact generation из `/backups` во временный host directory;
3. отправляет `.db` и `.sha256` на backup host;
4. скачивает **тот же** generation обратно;
5. выполняет `verify-copy`: SHA-256 → SQLite integrity → foreign keys;
6. делает `restore` скачанной копии в `/tmp/aiinbox`, не меняя canonical DB.

Успех заканчивается строкой:

```text
offsite_backup=ok generation=aiinbox-...db recovery_drill=ok
```

Пример ежедневного production cron вместо local-only backup cron:

```cron
17 3 * * * cd /opt/aiinbox && OFFSITE_BACKUP_TARGET='backup@example:/srv/aiinbox' ./scripts/offsite_backup.sh >> /var/log/aiinbox-backup.log 2>&1
```

Remote retention настраивается на backup host независимо; deployment script
намеренно не удаляет remote generations.

Для ручной проверки уже скачанной off-host копии:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/recovery:/recovery:ro" \
  app python -m app.ops verify-copy \
  --database /recovery/aiinbox-YYYYMMDDTHHMMSSffffffZ.db \
  --checksum-file /recovery/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256
```

До `v1.0-mvp` production gate требует хотя бы одного успешного запуска
`offsite_backup.sh` против реального удалённого host.

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

# Если старая DB есть, архивировать весь SQLite recovery set.
# Это гарантирует, что старые WAL/SHM sidecars не переживут canonical swap.
docker compose run --rm --no-deps app sh -c \
  'set -eu
   stamp=$(date -u +%Y%m%dT%H%M%SZ)
   if [ -e /data/app.db ]; then
     mv /data/app.db /data/app.db.pre-restore.$stamp
   fi
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
`aiinbox_backups`, restart policy из Compose и cron для off-host backup.
Перед обновлением, меняющим schema, сделайте verified backup. Перед
`v1.0-mvp` на production должны успешно пройти `status`, `smoke` и
`scripts/offsite_backup.sh`; последний включает recovery drill скачанной
off-host копии.

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

Source-local extraction state одного composite Item:

```bash
sqlite3 data/app.db \
  "SELECT id, item_id, source_index, source_type, extraction_status, error_code, source_url
   FROM item_sources WHERE item_id=<id> ORDER BY source_index"
```

На restart все stale `PROCESSING` автоматически возвращаются в `QUEUED`.
Обычный пользовательский retry делается кнопкой `🔁 Retry`.

Ручной fallback только для диагностики:

```sql
UPDATE item_sources
SET extraction_status='PENDING', error_code=NULL, error_message=NULL
WHERE item_id=<id> AND extraction_status='FAILED';

UPDATE items
SET processing_status='QUEUED', error_code=NULL, error_message=NULL
WHERE id=<id> AND processing_status='FAILED';
```

Не очищать `processing_stage`/contents и не переводить `READY` ItemSource обратно
в `PENDING`: это resume checkpoints уже успешно извлечённых частей сообщения.

### Original capture недоступен

Проверить только идентичность Telegram capture и текущий chat владельца:

~~~sql
SELECT i.id, i.user_id, i.telegram_message_id, u.telegram_chat_id
FROM items AS i
JOIN users AS u ON u.id = i.user_id
WHERE i.id = ?;
~~~

Если один из Telegram ID равен `NULL`, кнопка Original не показывается. Если Bot
API сообщает, что сообщение удалено или недоступно, восстановить его только по ID
нельзя. Сам Item и извлечённые данные остаются в SQLite (`contents` и
`item_sources`); пользователю предлагаются сохранённые source actions, если они есть.

## Event history and explicit feedback

Events remain auxiliary history; inspect a single Item without changing its
canonical state:

~~~sql
SELECT event_type, payload_json, idempotency_key, created_at
FROM events
WHERE item_id = <id>
ORDER BY id;
~~~

Telegram callback retries reuse the same namespaced idempotency key. Later user
clicks have a different key and remain separate history entries.
No-op category/type corrections create no Event; their consumed callback keys
are stored in `feedback_callback_receipts` so a delayed replay cannot become a
later change. Recognized feedback callbacks rejected because an owned Item or
category target is stale are also recorded there without adding Event history.

## Immediate deliveries

```bash
sqlite3 data/app.db \
  "SELECT id, type, status, item_id, profile_update_job_id, ask_job_id, export_job_id,
          attempts, last_error, updated_at
   FROM deliveries ORDER BY id DESC LIMIT 50"
```

`PENDING/SENDING` восстанавливаются delivery worker-ом/startup recovery.
`SENT` — зафиксированная успешная delivery state; transport semantics
at-least-once, поэтому crash сразу после Telegram send может дать дубль.
`ITEM_VIDEO:<source_id>` rows означают, что пользователь запросил конкретный
YouTube/Reel source. Для Telegram video `file_id` сохраняется в delivery payload
для повторного нажатия; generic document `file_id` не кэшируется, так как он не
подтверждает, что отправленный файл содержит видеодорожку. Локальный media-файл удаляется.
Если именно отправка видео превышает лимит размера Telegram, delivery worker
скачивает аудиодорожку в пределах того же лимита и отправляет её с подписью,
объясняющей, что видео слишком большое. Если аудиодорожка тоже не проходит лимит
или её не удалось получить, бот отправляет отдельное пояснение. Ошибки скачивания
или анализа до пользовательского запроса сами по себе этот fallback не запускают.
После исчерпания попыток `FAILED` video delivery снова ставится в очередь при
нажатии соответствующей кнопки.

`ASK_RESULT` и `ASK_FAILED` относятся к `ask_jobs`. `ASK_RESULT` хранит короткий
ответ только в outbox, пока Telegram не примет сообщение; после успешной отправки
поле answer очищается. Повторная Telegram delivery не повторяет retrieval/LLM.
Доставка остаётся at-least-once: процесс может завершиться после принятия сообщения
Telegram, но до фиксации `SENT`.

## Ask My Inbox

```bash
sqlite3 data/app.db \
  "SELECT id, user_id, status, error_code, created_at, updated_at
   FROM ask_jobs ORDER BY id DESC LIMIT 50"
```

`RUNNING` AskJob возвращается в `PENDING` при startup recovery. `DONE` означает,
что synthesis завершён и durable Delivery создан; успешная Telegram отправка
отражается отдельно в `deliveries.status`. `FAILED` означает сбой вычисления;
бот ставит короткое `ASK_FAILED` уведомление, а повторный `/ask` создаёт новый
самостоятельный job. Ошибки SQLite выходят к critical-task supervisor, а не
превращаются в обычный provider failure.

Для `/ask` не нужны отдельные credentials: `AskWorker` использует выбранные
`LLM_PROVIDER` и analysis model. Если provider не настроен, приложение не сможет
обрабатывать Ask запросы; после настройки используйте штатный restart и проверку
`python -m app.ops smoke`.

Операторские ошибки можно сгруппировать без чтения приватного вопроса:

```sql
SELECT error_code, COUNT(*)
FROM ask_jobs
WHERE status = 'FAILED'
GROUP BY error_code
ORDER BY COUNT(*) DESC;

SELECT id, user_id, status, error_code, created_at, updated_at
FROM ask_jobs
WHERE status = 'FAILED'
ORDER BY id DESC
LIMIT 50;

SELECT id, ask_job_id, status, attempts, updated_at
FROM deliveries
WHERE ask_job_id IS NOT NULL
ORDER BY id DESC
LIMIT 50;
```

| error_code | Likely layer | Operator action |
|---|---|---|
| `LLM_TIMEOUT` | Provider/network | Check provider health and outbound network; Ask retries once immediately. |
| `LLM_RATE_LIMITED` | Provider quota/rate policy | Check provider quota and rate limits. |
| `LLM_AUTH_FAILED` | Credentials/account | Verify configured secret and provider account, then restart workers. |
| `LLM_CONFIG_FAILED` | Model/request configuration | Verify provider/model compatibility and Structured Outputs support. |
| `INVALID_LLM_OUTPUT` | Model/schema compatibility | Verify the configured model supports the required structured response. |
| `LLM_FAILED` | Connection or provider server | Check safe application logs and provider status; transient failures retry once. |

SQLite/FTS errors are not stored as ordinary `SEARCH_FAILED` Ask outcomes. They
escape to the fail-fast supervisor; inspect database health and restart recovery.

`AskJob DONE` means synthesis is complete even if its `ASK_RESULT` Delivery is
still `PENDING`, `SENDING`, or terminal `FAILED`. Delivery retries use the
persisted outbox payload and never run FTS or the LLM again. Resolve the Telegram
transport problem first. For a terminal `FAILED` delivery, requeue only that
durable delivery row after confirming the matching AskJob state; this retains the
answer and does not create a new AskJob:

```sql
UPDATE deliveries
SET status = 'PENDING', attempts = 0, last_error = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :delivery_id
  AND ask_job_id = :ask_job_id
  AND type IN ('ASK_RESULT', 'ASK_FAILED')
  AND status = 'FAILED'
  AND EXISTS (
      SELECT 1 FROM ask_jobs
      WHERE ask_jobs.id = deliveries.ask_job_id
        AND ask_jobs.status = CASE deliveries.type
            WHEN 'ASK_RESULT' THEN 'DONE'
            WHEN 'ASK_FAILED' THEN 'FAILED'
        END
  );
```

Telegram delivery is at-least-once: if the Bot API accepted a message immediately
before a process/database failure, recovery may send it again. Never reset the
AskJob to `PENDING` to repair delivery.

Для OpenRouter strict JSON Schema запросы требуют upstream provider, который
обрабатывает `response_format`; adapter включает `require_parameters=true`, чтобы
маршрутизатор не выбрал upstream, молча игнорирующий параметр. При
`INVALID_LLM_OUTPUT` Ask log показывает только finish reason, наличие refusal,
тип/длину ответа и количество schema validation errors. Имена полей и их пути
не логируются: модель может поместить приватный текст в лишнее имя поля. Сам ответ,
вопрос и контекст тоже не логируются. Если модель вернёт числовой `source_id` как
JSON-строку, adapter нормализует только десятичное значение в целое число; Ask
service затем сверяет его с источниками, включёнными в конкретный запрос. Другие
нарушения схемы проходят обычный retry и controlled failure.

## Export / ownership

```bash
sqlite3 data/app.db \
  "SELECT id, user_id, mode, status, error_code, created_at, updated_at
   FROM export_jobs ORDER BY id DESC LIMIT 50"

sqlite3 data/app.db \
  "SELECT id, user_id, export_job_id, type, status, attempts, last_error, updated_at
   FROM deliveries WHERE type IN ('EXPORT_FILE', 'EXPORT_FAILED')
   ORDER BY id DESC LIMIT 50"
```

`PENDING` ExportJob ждёт `ExportWorker`; `RUNNING` автоматически возвращается в
очередь при startup. `DONE` означает, что ZIP готов и `EXPORT_FILE` Delivery
durably создана; факт Telegram отправки виден отдельно в `deliveries.status`.
Для `FAILED` job создаётся `EXPORT_FAILED` Delivery. Ошибки превышения Telegram
лимита или full-content guard рекомендуют `/export compact`; полный экспорт не
понижается молча до compact.

Локальный каталог — `EXPORT_DIR` (по умолчанию `./exports`), в Compose он
монтируется как отдельный persistent `aiinbox_exports:/exports` volume. Файлы
создаются с mode `0600`, каталог — `0700`; временный файл атомарно переименовывается
в готовый ZIP. В процессе доставки `PENDING/SENDING` артефакт защищён от retention.
После durable `SENT` файл удаляется; если удаление не удалось, cleanup удалит его
после истечения retention. Orphan ZIP и `.tmp` файлы тоже удаляются по mtime после
retention; файлы с посторонними именами не затрагиваются.

Export не является backup: он не включает SQLite, backup-файлы или внутренние
worker/outbox данные. `aiinbox_exports` не добавляется в `aiinbox_backups` и не
реплицируется через `scripts/offsite_backup.sh`.

## Reminders / digest

```bash
sqlite3 data/app.db \
  "SELECT id, user_id, item_id, type, scheduled_at, status, sent_at, payload_json
   FROM reminders ORDER BY scheduled_at DESC LIMIT 50"
```

Digest, snooze, proactive Attention и motivation nudge используют разные типы
Reminder. PM-08/PM-10 настройки доступны в кнопках Настроек и через
`/settings attention`;
существующие пользователи после PM-08 получают Attention OFF, а после PM-10 —
Generic motivation OFF, если ключ отсутствовал. Новые пользователи получают
Attention ON / Normal (3) и Generic motivation ON.
Настройки живут в `users.settings_json`; бюджет, cooldown и minimum gap
вычисляются из Reminder history и не требуют сбрасываемых счётчиков.
`REMINDER_POLL_SECONDS` задаёт отдельную частоту опроса ReminderWorker (по
умолчанию 30 секунд); она не меняет расписание digest или правила eligibility.
Кнопка статуса Attention использует те же gates и candidate projection, что и
worker, но выполняет только SELECT-запросы: она не создаёт claim, Reminder,
Event или изменение Item.

Digest переходит в `CLAIMED` до Telegram-вызова и в `SENT` только после
успешного ответа. Interrupted `CLAIMED` сохраняет текущую защиту digest от
повторной отправки, но не считается расходом дневного бюджета.

Snooze Reminder также становится `CLAIMED` до отправки и фиксируется как
`SENT` только после ответа Telegram. Неудачная доставка получает `FAILED` и
не запускает PM-08 minimum gap.

`CLAIMED` proactive Reminder — durable intent. Worker проверяет настройки,
quiet hours, Item lifecycle и актуальный PM-07 rank перед доставкой. Оставленный
claim повторяется после пяти минут; утративший актуальность получает статус
`CANCELLED`. Успешная Telegram delivery фиксируется короткой транзакцией как
`SENT` вместе с `ATTENTION_SHOWN`; `sent_at` фиксируется по успешному возврату
Telegram и служит временем для локального дневного бюджета и minimum gap. Перед
отправкой worker под SQLite write-lock заново проверяет текущее время, quiet
hours, PM-07 rank и same-Item cooldown. Если процесс остановится после принятия
сообщения Telegram, но до SQLite finalization, повторная попытка после lease
может отправить дубль: точно объединить транзакции Telegram и SQLite нельзя.

`MOTIVATION_NUDGE` хранит bounded snapshot `kind`, integer `facts`,
`focus_item_id`, `policy_level`, `local_date` и `slot`; `Reminder.item_id`
остаётся NULL как ключ пользовательского claim.
Посмотреть слоты и claims можно тем же запросом выше. `scheduled_at` у этого
типа — identity локального дня + ordinal слота, а не время будущей доставки.
Partial indexes `uq_reminders_motivation_slot` и
`uq_reminders_open_motivation_user` обеспечивают slot idempotency при NULL
`item_id` и максимум один открытый claim на пользователя.

Worker разделяет commit claim, Telegram send и финальный commit; SQLite write
lock не удерживается на сетевом вызове. Перед отправкой он заново вычисляет
факты и проверяет настройки, quiet hours, общий дневной budget, отдельный
generic cap, minimum gap и историю отправки того же kind. Только успешный
Telegram ответ становится `SENT`; `FAILED`/`CANCELLED` не расходуют budget или gap.
После recovery generation fencing не позволяет старому владельцу изменить
новый claim. Между принятым Telegram сообщением и SQLite финализацией остаётся
узкое at-least-once окно с возможным дублем; exactly-once не гарантируется.
Generic nudge не пишет `ATTENTION_SHOWN`. Его сообщение показывает выбранное
сохранение с Original/source actions и поддерживаемыми lifecycle actions.
В More доступны «Отложить», «Сделано» и «Меньше таких»; «Не сейчас» остаётся
только у proactive reminder, где работает same-Item dismissal cooldown. Для
нового focused nudge `REMINDER_SENT` и callbacks указывают на
сохранение из `focus_item_id`; исторические focusless Reminder остаются
валидными. Успешная доставка создаёт `REMINDER_SENT` в той же транзакции,
которая фиксирует `SENT`; `Меньше таких` ссылается на этот Reminder.
Proactive reminder, digest и snooze resurfacing также получают
`REMINDER_SENT` только после успешного ответа Telegram. У Telegram URL-кнопки
нет наблюдаемого callback, поэтому она не создаёт `REMINDER_OPENED`.

Проверить отправки и реакции без чтения содержимого Item можно так:

```sql
SELECT id, user_id, item_id, reminder_id, event_type, created_at, payload_json
FROM events
WHERE event_type LIKE 'REMINDER_%'
ORDER BY created_at DESC
LIMIT 100;
```

`payload_json` содержит только bounded snapshot Reminder (например, тип,
категорию, Item type или MotivationKind), без исходного текста. Dismissal даёт
дополнительные 24 часа scheduler cooldown; PM-08 same-Item cooldown продолжает
действовать, выбирается более позднее время. Item-specific dislike и недельная
fatigue поправка вычисляются из Event history при ranking/send preparation, не
хранятся отдельными счётчиками. Generic dislike подавляет тот же MotivationKind
семь суток.

PM-09 hooks хранятся в `contents` как `ATTENTION_HOOK`. Проверить attribution
можно без вывода полного исходного текста:

```sql
SELECT id, item_id, source_id, metadata_json
FROM contents
WHERE kind = 'ATTENTION_HOOK'
ORDER BY id DESC
LIMIT 50;
```

Generation выполняется только после proactive claim и не делает web fetch.
Provider/model, evidence Content ID и excerpt находятся в metadata hook; для
hook attribution Reminder payload добавляет только `hook_content_id` и
`template_id` к существующим PM-08 полям. Ошибка/timeout видны в log по
`item_id`, `reminder_id` и fallback reason; полный source context не логируется.
FTS исключает эти derived rows и продолжает индексировать исходный Content.
Migration `f5a7c2d91e08` добавляет kind через SQLite check rebuild; downgrade
останавливается, пока в базе остаются derived hook rows, чтобы не потерять их.

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

## Instagram extraction

Instagram поддерживает только URL вида `https://www.instagram.com/reel/<id>/` и
`https://instagram.com/reel/<id>/`. Один Reel становится одним ItemSource внутри
исходного Telegram Item. yt-dlp сначала получает metadata, затем скачивает только
нужное media; длительность и фактический размер файла проверяются до STT/frame
analysis. yt-dlp и ffprobe работают в изолированной process group: timeout и
shutdown завершают процессы до очистки частного каталога загрузки. Временные
файлы удаляются после обработки.

`AUTH_REQUIRED` означает, что Instagram не выдал media без авторизации. Если
оператор настроил cookie file, проверьте доступность указанного файла и нажмите
Retry у Item. `RATE_LIMITED` — временное ограничение платформы; повторите Retry
позже. Не используйте browser-cookie harvesting, private API или обход защиты.

## YouTube media downloads

`YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS` defaults to 300 seconds. Production yt-dlp
media downloads run in a killable process group; timeout or shutdown stops the
group before the temporary directory can be removed. A timed-out delivery uses
the normal bounded outbox retry policy and can be requested again after a
terminal failure. The worker resolves the completed output and checks its actual
streams with `ffprobe`: visual analysis requires video, while Telegram delivery
requires both video and audio. A separate audio/video component or `.part` file
cannot be sent as the requested full video.

## Shutdown / restart

SIGTERM/SIGINT:

1. stop new work;
2. workers получают bounded drain по `SHUTDOWN_TIMEOUT_SECONDS`;
3. зависшие tasks отменяются;
4. Telegram session и DB engine закрываются.

Critical worker/polling failure завершает процесс. Docker Compose
`restart: unless-stopped` поднимает его снова; durable state восстанавливается.
