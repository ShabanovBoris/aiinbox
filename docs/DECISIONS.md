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

Decision: каждый Done/Snooze/Archive/Retry меняет scoped Item и добавляет event
в одной SQLAlchemy-транзакции. Повтор уже выполненного действия становится
no-op без дублирования события; Retry не сбрасывает `processing_stage`.

Reason: Item остаётся каноническим состоянием, а events — durable auxiliary
журналом для будущего обучения без распределённых блокировок или event sourcing.

Consequences: callback повторяем и безопасен для restart; отмена Later меняет
только lifecycle state и не создаёт отдельного telemetry-события.

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
