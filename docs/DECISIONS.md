# Architecture Decisions

Журнал значимых архитектурных решений, чей рационал иначе потеряется.
Формат записи: Context / Decision / Reason / Consequences (см. AGENTS.md, раздел про DECISIONS.md).
Фиксируются только реально принятые решения, с указанием этапа.
Изменение ядра системы — только через новую запись здесь, а не молча в очередной задаче.

## D-001 — Resumable modular monolith (этап 0)

Context: тяжёлые этапы обработки (download страницы, ffmpeg, transcription, extraction)
дороги и медленны. PRODUCT_SPEC §59 требует продолжения обработки с последнего
доступного результата, §60 — хранение текущего stage.

Decision: Item — единица, накапливающая промежуточные результаты по стадиям:

```text
Item
 ├── source
 ├── processing_stage
 ├── extracted_content
 ├── transcript
 ├── visual_notes
 ├── analysis
 └── resulting state
```

Retry и перезапуск процесса продолжают с последнего готового результата, а не с нуля.

Reason: сбой позднего этапа (analysis) не должен повторять
download → ffmpeg → transcription; надёжность продукта и продолжаемость работы
новой сессией агента — одно и то же свойство системы.

Consequences: `processing_status` (QUEUED/PROCESSING/READY/FAILED) не отражает
глубину прогресса — глубину отражает набор сохранённых промежуточных результатов
плюс `processing_stage`.

## D-002 — Стабильное ядро, заменяемые края (этап 0)

Context: ТЗ требует возможности Android-клиента и сменного LLM provider
без переписывания доменной части.

Decision: каркас фиксирован:

```text
Telegram ─┐
          ├→ Ingestion → Extract → NormalizedContent
Future API┘                         ↓
                              Analyzer
                                  ↓
                            PriorityEngine
                                  ↓
                               SQLite
```

- Края системы — заменяемые, изолируются в handlers / extractors / llm adapters:
  Telegram, YouTube/yt-dlp, OpenAI, Playwright.
- Стабильное ядро — не переписывается при добавлении края: `NormalizedContent`,
  `AnalysisResult`, `PriorityEngine`, состояния Item.

Reason: каждую задачу выполняет новая сессия агента; без фиксированных инвариантов
каркас будет дрейфовать от задачи к задаче.

Consequences: новый край (HTTP API, Ollama, новый extractor) добавляется как adapter
поверх ядра и не требует переписывания проекта.

## D-003 — Внешний Orchestrator и PR-workflow (принято 2026-09-12)

Context: проект реализуется сессиями агента; без внешнего контроля acceptance
дрейфует (self-approval, scope creep, прямые коммиты в основную ветку).

Decision: принят `Оркестрационный протокол Codex → Reviewer.md`. Агент —
implementation agent; Orchestrator (ChatGPT, фиксированная беседа, открывается
через Browser Use) принимает acceptance, merge, архитектурные pivots, scope
change и переходы между фазами. Реализация фаз — в ветках `phase/NN-*`, PR →
`main`, `REVIEW REQUEST` → `APPROVED / CHANGES REQUIRED / BLOCKED`, squash merge.
`main` защищён: без direct commit, force push, self-merge.

Reason: протокол задан пользователем как высшая инструкция после его явных
решений; он же устраняет self-approval и делает историю фаз проверяемой через
GitHub (PR metadata, diff, SHA).

Consequences: `DONE` в IMPLEMENTATION_STATE достигается только через
`IN_REVIEW` + `APPROVED`; между `REVIEW REQUEST` и ответом Orchestrator'а —
никакого scope expansion (§12). AGENTS.md §2/§48/§57 и RUNBOOK приведены
в соответствие протоколу.

## D-004 — Атомарный claim и restart recovery воркера (этап 1)

Context: несколько asyncio-воркеров берут работу из SQLite-очереди; процесс может
умереть в любой момент (PRODUCT_SPEC §15, §17; D-001 resumable).

Decision: claim Item'а — один UPDATE с подзапросом oldest QUEUED и условием
`processing_status='QUEUED'` (UPDATE...RETURNING); двойная обработка физически
невозможна без внешних блокировок. Restart recovery: при старте процесса все
`PROCESSING` возвращаются в `QUEUED` (`requeue_stale`) — процесс один, поэтому
любой PROCESSING в БД на старте остался от умершего процесса.

Reason: SQLite сериализует запись — условный UPDATE даёт атомарность без Redis/locks;
time-based stale detection не нужен при single-process инварианте (проще и
детерминированнее).

Consequences: воркеры не требуют координации в памяти; переход на multi-process
потребует пересмотра (новая запись здесь). Внешние блокировки не вводятся до
реального потребления.

## D-005 — Application-controlled FTS5 index (этап 9)

Context: `/search` должен находить title, summary, user note, tags и тексты из
`contents`, включая DONE/ARCHIVED Items. SQLite triggers добавили бы скрытую
магическую синхронизацию между несколькими таблицами.

Decision: использовать отдельную SQLite FTS5 virtual table `item_search` с
`item_id` и `user_id` как UNINDEXED columns. Приложение обновляет строку в том
же commit, что и READY; перед пользовательским поиском пересобирает индекс
конкретного пользователя.

Reason: каноническими остаются `items` и `contents`, старые записи безопасно
индексируются без отдельного backfill worker, а синхронизация остаётся явной и
тестируемой.

Consequences: поиск делает небольшой rebuild для одного пользователя; это
приемлемо для личного MVP. Embeddings, vector search и recommendation ML не
добавляются.

## D-006 — Actions и events в одной транзакции (этап 10)

Context: Telegram callback может прийти повторно или после перезапуска; Item
state и feedback event не должны расходиться.

Decision: каждый Done/Snooze/Archive/Retry выполняет scoped conditional
`UPDATE ... RETURNING` и добавляет event в той же SQLAlchemy-транзакции только
если UPDATE действительно выиграл переход. DONE и ARCHIVED — terminal states:
конкурирующие terminal callbacks не перезаписывают друг друга. Повтор уже
выполненного действия становится no-op; Retry не сбрасывает `processing_stage`.

Reason: Item остаётся каноническим состоянием, а events — durable auxiliary
журналом для будущего обучения без распределённых блокировок или event sourcing.

Consequences: callback повторяем, безопасен для restart и concurrency; один
lifecycle transition создаёт максимум одно соответствующее событие. Отмена
Later меняет только lifecycle state и не создаёт отдельного telemetry-события.
Telegram callback отображает фактический persisted state/status, поэтому
проигравший concurrent CAS не подтверждает пользователю несостоявшееся действие.

## D-007 — SQLite как durable scheduler уведомлений (этап 11)

Context: daily digest и snooze resurfacing должны переживать restart и не
дублироваться, но MVP остаётся одним процессом на SQLite.

Decision: хранить настройки в `users.timezone`/`users.settings_json`, а
идемпотентные delivery claims — в `reminders`; запускать один periodic
`ReminderWorker` на asyncio. Digest claim фиксируется в SQLite до Telegram
отправки, а snooze claim и переход Item в ACTIVE фиксируются транзакционно.

Reason: это удовлетворяет restart/idempotency требованиям без Celery,
APScheduler, Redis или отдельной блокировки; partial unique index закрывает
особенность SQLite, где `NULL item_id` не участвует в обычной UNIQUE-проверке.

Consequences: авария после durable claim, но до фактической отправки, может
потерять одно уведомление, зато не создаёт повторную доставку после restart;
ошибка Telegram записывается как `FAILED` и не ломает worker.

## D-008 — bounded processing deadline и graceful shutdown (этап 12)

Context: один внешний provider или subprocess может зависнуть, а SIGTERM не
должен немедленно прерывать текущий Item без шанса на checkpoint.

Decision: `ProcessingWorker` ограничивает полную обработку Item через
`PROCESSING_TIMEOUT_SECONDS`; при штатном завершении сначала выставляется stop,
воркерам даётся `SHUTDOWN_TIMEOUT_SECONDS`, и только затем оставшиеся задачи
отменяются. Неожиданное завершение любого processing/profile/reminder worker
или Telegram polling считается process-level failure: после того же bounded
cleanup исключение выходит из `run()`, а restart принадлежит внешнему runtime.
DB/SQLAlchemy failure не маскируется как обычный FAILED Item.
То же правило действует для `ProfileUpdateWorker`: SQLAlchemyError выходит к
supervisor, а RUNNING job восстанавливается startup recovery.

Reason: timeout provider-а оставляет Item в контролируемом FAILED/retryable
состоянии, но потеря критического worker-а означает, что процесс больше не
гарантирует обработку. Fail-fast позволяет штатному внешнему supervisor-у
перезапустить весь single-process runtime; distributed queue не требуется.

Consequences: принудительная отмена после shutdown deadline или infrastructure
failure может оставить PROCESSING Item, который будет безопасно requeue при
следующем старте. Docker Compose использует `restart: unless-stopped`.

## D-009 — Durable chunk summary identity (post-MVP review)

Context: paragraph-aware chunking может изменить текст chunk'а при тех же
`chunk_index`, max size и overlap; сохранённый summary нельзя переиспользовать
только по позиционным параметрам.

Decision: каждый `CHUNK_SUMMARY` хранит SHA-256 точного текста chunk'а, а resume
переиспользует summary только при совпадении index, chunk settings и hash.
Legacy rows без hash считаются несовместимыми и пересчитываются.

Reason: durable resume должен переиспользовать summary только для того же
входного текста независимо от эволюции chunking-алгоритма.

Consequences: первый retry после обновления может пересчитать старые summaries,
зато не смешивает результаты разных chunk boundaries.

## D-010 — OpenRouter через OpenAI-compatible adapters (post-MVP)

Context: для дешёвых live/E2E проверок нужен второй LLM provider, при этом
OpenRouter предоставляет OpenAI-compatible chat, vision и transcription endpoints.

Decision: `LLM_PROVIDER=openrouter` выбирает отдельные `OPENROUTER_*` credentials
и model ids. Analysis/vision переиспользуют OpenAI-compatible adapter с
конфигурируемым `base_url=https://openrouter.ai/api/v1`. Transcription расширяет
тот же transport отдельным OpenRouter adapter: файлы >25 MB или аудио >5 минут
режутся ffmpeg на mono WAV PCM 16 kHz сегменты и отправляются bounded-concurrent
multipart-запросами (до 4 одновременно).

Reason: chat/vision transport contract совпадает с уже изолированной provider
boundary, но у OpenRouter STT есть отдельные operational limits: multipart до
25 MB и upstream processing timeout около 60 секунд. Provider-specific
segmentation держит эти ограничения внутри adapter boundary.

Consequences: OpenAI path остаётся без изменений, а OpenRouter-модели можно
менять конфигом. Конкретная analysis-модель обязана поддерживать structured JSON
Schema, vision-модель — изображения, transcription-модель — STT endpoint.
Long-audio OpenRouter STT требует доступный `ffmpeg`.

## D-011 — Durable immediate Telegram outbox (post-MVP hardening)

Context: READY/FAILED Item и завершённый `/profile_update` раньше коммитились до
best-effort Telegram callback. Crash между business commit и send безвозвратно
терял пользовательское уведомление.

Decision: immediate delivery intent хранится в отдельной `deliveries` outbox и
создаётся в той же транзакции, что READY/FAILED/profile DONE. `DeliveryWorker`
атомарно claim'ит PENDING → SENDING, отправляет Telegram и фиксирует SENT;
startup recovery возвращает прерванные SENDING → PENDING. Старые callback paths
удалены, чтобы side effect имел один канонический владелец.

Reason: canonical business state не должен откатываться из-за transport failure,
а требование restart recovery должно переживать crash после commit.

Consequences: семантика доставки at-least-once. Crash после фактического Telegram
send, но до SENT может дать дубль после restart; это предпочтительнее silent loss.
Telegram failure ретраится bounded независимо от Item/ProfileUpdateJob state.
Пользовательский Retry атомарно переводит ещё не завершённый `ITEM_FAILED`
delivery в `CANCELLED`; если worker уже забрал delivery, перед send он проверяет,
что Item всё ещё `FAILED`. Следующий реальный failure может reopen тот же durable
delivery row, поэтому отмена stale intent не ломает повторные циклы Retry → FAILED.

## D-012 — Durable STT segment checkpoint identity (PR #18 review)

Context: позиционный `segment_index` не доказывает, что retry видит тот же media
segment или ту же transcription model. YouTube media stream и provider config
могут измениться при неизменном Item/URL.

Decision: каждый `TRANSCRIPT_CHUNK` хранит SHA-256 точных segment bytes, provider,
model, segment duration и versioned ffmpeg output contract. Resume переиспользует
текст только при полном совпадении identity. Legacy index-only rows и mismatch
пересчитываются; новый результат становится последним checkpoint для index.

Reason: resumable STT может экономить уже выполненную работу только если вход и
семантика транскрипции эквивалентны, аналогично D-009 для `CHUNK_SUMMARY`.

Consequences: после изменения model/segmentation или media bytes первый retry
перетранскрибирует затронутые сегменты, но никогда не смешивает stale transcript
с новым источником.
