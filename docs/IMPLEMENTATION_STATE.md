# Implementation State

Точка входа для новой сессии агента после обрыва предыдущей: файл показывает,
что сделано, что в работе, что заблокировано извне и какая проверка проходила
последней. История чата источником истины не является.

Статусы: `NOT_STARTED` / `IN_PROGRESS` / `IN_REVIEW` / `DONE` / `BLOCKED`.

Правило статусов (оркестрационный протокол §25): `IN_REVIEW` = реализация завершена, PR открыт и отправлен `REVIEW REQUEST` Orchestrator'у; `DONE` ставится только после явного `APPROVED` Orchestrator'а; `BLOCKED` — только реальный внешний блокер. Фаза в `IN_REVIEW` не расширяется по scope: исправления идут в ту же branch и PR.

Handshake APPROVED → DONE → merge: после `APPROVED @ HEAD A` агент делает
единственный status-finalization commit (`IN_REVIEW` → `DONE`, с записью approved
HEAD A), delta A..B — только статусная документация; затем объявляет `MERGE READY`
(Previous approved HEAD: A, New HEAD: B). Orchestrator проверяет delta и делает
squash merge с ожидаемым HEAD B. Вердикты фиксируются в `docs/REVIEWS.md`.

Архитектурные решения фиксируются отдельно — в `docs/DECISIONS.md`.

## Contract addendum — PR #1 — orchestration protocol adoption

Статус: APPROVED @ 5611be6b52fc546cd4dd060b985d24af8619ed88
(GitHub review: pullrequestreview-5187945174). Вердикты и история ревью —
в `docs/REVIEWS.md`. Squash merge выполняет Orchestrator; Phase 1 начинается
только после merge и sync main (протокол §9.2, §23).

## Phases

| # | Этап | Статус |
|---|------|--------|
| 0 | Project contract | DONE |
| 1 | Skeleton | DONE |
| 2 | Text end-to-end | DONE |
| 3 | Web ingestion | IN_REVIEW |
| 4 | Architecture checkpoint | NOT_STARTED |
| 5 | Voice/audio | NOT_STARTED |
| 6 | YouTube | NOT_STARTED |
| 7 | Video visual analysis | NOT_STARTED |
| 8 | User profile | NOT_STARTED |
| 9 | Today/inbox/search | NOT_STARTED |
| 10 | Item actions | NOT_STARTED |
| 11 | Notifications | NOT_STARTED |
| 12 | Production hardening | NOT_STARTED |
| 13 | Final acceptance | NOT_STARTED |

Post-MVP этапы (промпты 14–18: Ollama, LLM router, behaviour ranking, HTTP API,
semantic search) здесь не отслеживаются, пока MVP не принят (Phase 13).

## Phase details

Фаза в статусе IN_PROGRESS обязана иметь живой раздел ниже. После завершения фазы
раздел сохраняется как факт выполненного.

### Phase 0 — Project contract — DONE

Статус присвоен до введения оркестрационного протокола: контрактные документы
приняты тем, что работа перешла к следующим шагам. Дальнейшие фазы проходят
через `IN_REVIEW` и `DONE` только по `APPROVED` Orchestrator'а.

Completed:
✓ repository изучен: пустой greenfield, только два планировочных документа
✓ docs/PRODUCT_SPEC.md — ТЗ перенесено без изменений
✓ AGENTS.md — правила, архитектурные инварианты, resumable processing,
  правило продвижения без внешних зависимостей
✓ docs/DECISIONS.md — D-001 (resumable), D-002 (ядро/края)
✓ docs/RUNBOOK.md — каркас операций
✓ git инициализирован (branch main), .gitignore, первый commit
✓ внешний ревью-канал зафиксирован: ChatGPT через Browser Use (AGENTS.md §57)
✓ GitHub remote https://github.com/ShabanovBoris/aiinbox подключён, push работает
  (gh auth login + gh auth setup-git)
✓ branch protection для main включена (PR-only, no force push, linear history)

Remaining:
□ — нет

Last verification:
diff «ТЗ ↔ docs/PRODUCT_SPEC.md» — различие только в служебной шапке
branch protection: gh api .../branches/main/protection → PR required, force push
и deletions запрещены, linear history включена
(pytest/ruff неприменимы: кода ещё нет)

### Phase 1 — Skeleton — DONE

APPROVED @ 0ca1266678b6e902dd7f0d387bd4059efd69e27c (Orchestrator, GitHub review
pullrequestreview-5188149992). По ходу ревью закрыты 4 finding'а вердикта 522b2ce:
гонка создания User (concurrency-safe get_or_create_user), persist-before-ACK,
реальное включение SQLite FK (pragma на каждый connection), конкурентные тесты
atomic claim (D-004).

Completed:
✓ pyproject.toml (hatchling; deps: aiogram, SQLAlchemy 2 async + aiosqlite, alembic,
  pydantic-settings; dev: pytest, pytest-asyncio, ruff); uv venv на Python 3.12
✓ конфигурация pydantic-settings: TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS,
  DATABASE_URL, PROCESSING_CONCURRENCY, DEFAULT_TIMEZONE, PROCESSING_POLL_SECONDS
✓ SQLite + SQLAlchemy async. Item: id, user_id, telegram_message_id, source_index,
  processing_status, state, source_type, processing_stage, user_note, error_code,
  error_message, created_at/updated_at; unique (user_id, telegram_message_id, source_index)
✓ User: telegram identity (telegram_user_id unique, telegram_chat_id, timestamps)
✓ Alembic async-миграции (initial schema 4cbfde82e2e8, render_as_batch для SQLite)
✓ Telegram-хендлеры: /start, текст → Item QUEUED (source_type=TEXT); allowlist,
  неавторизованные молча игнорируются; тяжёлой обработки в handler нет —
  бизнес-логика в services/ingestion, aiogram не проникает в сервисы
✓ ProcessingWorker: атомарный claim oldest QUEUED (UPDATE...RETURNING, D-004),
  PROCESSING → READY / FAILED(error_code, error_message); requeue stale PROCESSING
  при старте; PROCESSING_CONCURRENCY воркеров
✓ Startup/shutdown: миграции при старте, SIGINT/SIGTERM graceful, headless-режим
  без токена (локальный smoke без Telegram network)
✓ tests: 18 passed — ingestion (QUEUED/TEXT, duplicate → тот же Item, unique на
  уровне БД, source_index, один user), worker (claim atomic, QUEUED→READY,
  exception→FAILED, oldest first, requeue stale), handlers (allowlist),
  миграции (fresh DB → head, unique enforced схемой)
✓ e2e smoke: живой процесс обрабатывает QUEUED → READY; SIGINT graceful

Remaining:
□ — нет

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 23 passed
(+5 регрессий на review findings: user race, persist→ACK, FK enforcement,
конкурентный claim); fresh DB → alembic upgrade head → users/items, duplicate и
orphan user_id запрещены схемой; smoke: `uv run python -m app.main` без токена →
bot disabled; INSERT QUEUED-Item → READY за <2 c; SIGINT → shutdown complete

### Phase 2 — Text end-to-end — DONE

APPROVED @ cffa2da8a430258c07db60cd28d08030c4c0ca7b (Orchestrator, GitHub review
pullrequestreview-5188249594). Ревью прошло два круга: strict Structured Outputs
(исправлен сломанный трансформер properties/$defs) и resumable checkpoint
(claim не затирает стадию, LLM-результат атомарен с PRIORITIZING, resume без
повторного вызова).

Completed:
✓ NormalizedContent / AnalysisResult / UserProfile (pydantic, строгие лимиты
  полей; priority_score LLM не отдаёт)
✓ ItemType enum: ACTION/LEARN/READ/WATCH/IDEA/REFERENCE/SOMEDAY
✓ LlmProvider (Protocol) + OpenAiProvider: SDK только в адаптере, model ids из
  конфига, strict Structured Outputs (json_schema, схема из AnalysisResult:
  все поля required, additionalProperties=false, extra="forbid"), Pydantic-валидация
  (regex-парсинга нет), ошибки → LlmError(LLM_FAILED / INVALID_LLM_OUTPUT);
  system prompt с untrusted-content изоляцией (PRODUCT_SPEC §21)
✓ TextExtractor → NormalizedContent (без registry framework)
✓ Analyzer: content + профиль + существующие категории (DISTINCT-запрос,
  динамические строки, не enum) → AnalysisResult
✓ PriorityEngine: детерминированная формула с весами в domain/priority.py;
  quick_win считается кодом (None→0.5, max(0, 1-min/60)); clamp 0..100
✓ ProcessingPipeline resumable: стадии коммитятся до внешних вызовов, дорогой
  LLM-результат персистится атомарно с checkpoint'ом PRIORITIZING; resume с
  durable-стадии не повторяет успешный LLM-вызов; claim не затирает стадию;
  requeue сохраняет содержательную стадию; результат анализа персистится
  (миграция b07bbcab9a72)
✓ Результат в Telegram: persist → ACK; on_result-колбэк воркера (auxiliary, сбой
  доставки не портит READY) → bot/formatting + bot/notify
✓ config: LLM_PROVIDER, OPENAI_API_KEY, OPENAI_ANALYSIS_MODEL, LLM_TIMEOUT_SECONDS

Remaining:
□ Blocked (external): live-проверка happy path с реальным OpenAI — нет ключа;
  пайплайн покрыт FakeLlmProvider, путь ошибки проверен live (401 → FAILED/LLM_FAILED)

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 45 passed
(+ PriorityEngine exact-value тесты, e2e с FakeLlmProvider, invalid/garbage JSON →
INVALID_LLM_OUTPUT, extra priority_score → INVALID_LLM_OUTPUT, strict-схема:
все поля + $defs.ItemType + resolvable $ref, полный resumable-сценарий без второго
LLM-вызова, delivery-failure не портит READY, upgrade Phase 1 DB → head)
smoke: живой процесс с dummy-ключом → Item дошёл до FAILED/LLM_FAILED через
реальный OpenAI SDK (ошибка смаппирована); SIGINT graceful

### Phase 3 — Web ingestion — IN_REVIEW

Completed:
✓ Разбор сообщения: text+URL / несколько URL → Item на каждый URL (source_index),
  общий текст — user_note; повторы URL внутри сообщения дедуплицируются
✓ URL normalization: fragment, lowercase host, tracking params (utm_*/gclid/fbclid),
  значимые query сохраняются
✓ Дедупликация URL per-user: unique (user_id, source_url) на уровне БД + race-safe
  resolve; повторный URL не создаёт второй Item
✓ SSRF: http/https only; localhost (по имени) и IP-литералы частных адресов
  отвергаются до DNS; DNS pinning — соединение на проверенный IP
  (PinningTransport, Host/SNI оригинальные); каждый redirect-хоп ревалидируется;
  лимит redirect'ов; IPv4-mapped IPv6 и CGNAT покрыты
✓ Streamed download с инкрементальным byte-cap (работает без Content-Length);
  transient retry (3 attempts, exponential backoff), permanent — ровно одна
  попытка; WEB_TIMEOUT_SECONDS управляет клиентом
✓ Playwright fallback: жёстко отключён без production opt-in (route-deny не
  даёт SSRF-изоляции); вернётся с настоящим network boundary
✓ PinningTransport: aclose() делегируется внутреннему транспорту; Connection:
  close — переиспользование соединений по IP-origin исключено
✓ WebPageExtractor: httpx (timeout, max size) → trafilatura (в thread) →
  недостаточно текста → Playwright fallback → trafilatura; лимит извлечения
  MIN_EXTRACTED_TEXT_LENGTH
✓ Contents storage: таблица contents (kind WEB_TEXT и др.); WEB_TEXT персистится
  атомарно с checkpoint'ом ANALYZING — переживает restart, retry не перекачивает
  страницу (resume из персистеного WEB_TEXT)
✓ Ошибки: AppError(code) — DOWNLOAD_FAILED / EXTRACTION_FAILED / TOO_LARGE /
  SECURITY_REJECTED / TIMEOUT (+ LLM_FAILED/INVALID_LLM_OUTPUT унаследованы);
  retry transient, permanent не ретраятся
✓ user_note передаётся анализатору (NormalizedContent.user_note, в prompt —
  как untrusted intent signal); WEB_TEXT metadata хранит
  title/author/language/user_note — resume восстанавливает эквивалентный
  NormalizedContent (тест на полное равенство)

Remaining:
□ Blocked (external): live-проверка с реальным OpenAI — нет ключа; web-путь до
  LLM-границы проверен live (example.com → 401 → FAILED/LLM_FAILED)

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 78 passed
(+ SSRF pinning/DNS/redirect, transient retry и permanent no-retry, oversized
streaming без Content-Length, кастомный timeout, normalization порт/trailing slash,
resume восстанавливает полный NormalizedContent, дедупликация URL, web pipeline e2e)
smoke: живой процесс — WEB item https://example.com прошёл SSRF → download →
trafilatura → WEB_TEXT → LLM-граница (401 → FAILED/LLM_FAILED); SIGINT graceful

### Шаблон фазы в работе

```text
Phase N — <название> — IN_PROGRESS

Completed:
✓ <завершённые под-задачи>

Remaining:
□ <следующие под-задачи>

Blocked (external):   # раздел добавляется только при реальном блокере
□ <точная live-проверка> — блокер: <чего именно не хватает>

Last verification:
pytest <пути> — <N passed>
ruff check . — pass
ruff format --check . — pass
```
