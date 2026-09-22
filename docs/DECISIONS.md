# Architecture Decisions

Только текущие неочевидные архитектурные инварианты. История реализации и review
хранится в GitHub PR/commits, а не в этом файле.

## D-001 — Resumable single-process pipeline

Item processing хранит durable stage/content/checkpoints в SQLite. Retry/restart
продолжают с compatible checkpoint, а не повторяют download/STT/LLM без причины.

## D-002 — Стабильное ядро, заменяемые adapters

Telegram, HTTP/YouTube и LLM — края. `NormalizedContent`, analysis, priority,
lifecycle и persistence не должны зависеть от конкретного provider/client.

## D-003 — GitHub PR как acceptance boundary

`main` защищён. Изменение идёт через scoped branch + PR + required `quality`.
Orchestrator review привязан к exact HEAD SHA. После APPROVED этот HEAD либо
squash-merge'ится с expected SHA, либо любое новое изменение требует re-review.

## D-004 — Atomic queue claim + startup recovery

Processing claim — conditional `UPDATE ... RETURNING` для oldest QUEUED.
Single-process runtime позволяет на startup безопасно вернуть оставшиеся
PROCESSING в QUEUED.

## D-005 — Application-controlled SQLite FTS5

`item_search` — производная проекция из Item/contents, не canonical state.
Приложение синхронизирует/rebuild'ит индекс явно; отдельный search service не нужен.

## D-006 — Lifecycle action и event атомарны

Done/Snooze/Archive/Retry выполняются conditional update'ом. Event создаётся только
если transition реально выигран; повторные callbacks — no-op.

## D-007 — SQLite scheduler для digest/snooze

`reminders` хранит durable schedule/idempotency. Для этих scheduled notifications
предпочтено избежать duplicate после restart даже ценой узкого окна silent loss
между durable claim и Telegram send.

## D-008 — Bounded processing/shutdown + fail-fast supervisor

Item имеет end-to-end processing deadline. Graceful shutdown даёт workers bounded
drain. Неожиданная смерть critical worker/polling или DB infrastructure failure
роняет процесс; внешний supervisor отвечает за restart.

## D-009 — Chunk summary identity

`CHUNK_SUMMARY` reuse разрешён только при совпадении chunk index/settings и
SHA-256 exact chunk text. Position-only checkpoint недостаточен.

## D-010 — OpenRouter через OpenAI-compatible boundary

OpenAI и OpenRouter используют общий compatible analysis/vision transport, но
раздельные credentials/model IDs. OpenRouter long STT режется на bounded mono
WAV PCM 16 kHz segments.

## D-011 — Durable immediate Telegram outbox

READY/FAILED/profile DONE создают delivery intent в той же business transaction.
`DeliveryWorker` обрабатывает PENDING→SENDING→SENT; interrupted SENDING
requeue'ится на startup. Семантика — at-least-once.

Retry отменяет obsolete pending/sending failure intent; перед failure send worker
дополнительно проверяет, что Item всё ещё FAILED.

## D-012 — STT segment identity

`TRANSCRIPT_CHUNK` reuse требует SHA-256 exact segment bytes + provider/model +
segmentation contract. Legacy/index-only or mismatched checkpoints пересчитываются.
После успешного final transcript segment checkpoints удаляются.

## D-013 — Online SQLite backup + verified restore

Backup создаётся через SQLite Online Backup API, а не копированием live `.db`
файла. Snapshot проходит `PRAGMA integrity_check` и
`PRAGMA foreign_key_check`, хранится в отдельном volume и ротируется bounded
числом поколений. Restore всегда создаёт новый файл. При canonical swap
остановленного приложения старые `.db`, `-wal` и `-shm` архивируются как
единый recovery set, чтобы sidecars старой БД не применились к restored DB.

## D-014 — Off-host backup через host-owned rsync/SSH

Каждое verified SQLite backup generation состоит из `.db` и стандартного
`.sha256` sidecar. Off-host transport принадлежит deployment host, а не
application runtime: `scripts/offsite_backup.sh` использует rsync/SSH и обычный
OpenSSH key/config, не добавляя cloud SDK или storage credentials в контейнер.
Успешная replication включает download-back того же generation,
checksum + SQLite/FK verification и restore drill во временную DB. Canonical
`/data/app.db` при таком drill не изменяется.
