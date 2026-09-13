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
| 3 | Web ingestion | DONE |
| 4 | Architecture checkpoint | DONE |
| 5 | Voice/audio | DONE |
| 6 | YouTube | DONE |
| 7 | Video visual analysis | DONE |
| 8 | User profile | DONE |
| 9 | Today/inbox/search | DONE |
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

### Phase 3 — Web ingestion — DONE

APPROVED @ 14f4804e5a2618f9c3a6f41cf52bf7374c92e382 (Orchestrator, GitHub review
pullrequestreview-5188406402). Ревью прошло два полных круга: SSRF-пиннинг
(PinningTransport + Connection: close), streamed byte-cap, transient retry,
полная реставрация NormalizedContent, минимальная URL normalization,
Playwright fallback жёстко отключён с удалением зависимости.

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
  даёт SSRF-изоляции); playwright-зависимость удалена из pyproject; вернётся
  с настоящим network boundary
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
ruff check . → pass; ruff format --check . → pass; pytest → 81 passed
(+ SSRF pinning/DNS/redirect, transient retry и permanent no-retry, oversized
streaming без Content-Length, кастомный timeout, normalization порт/trailing slash,
resume восстанавливает полный NormalizedContent, дедупликация URL, web pipeline e2e)
smoke: живой процесс — WEB item https://example.com прошёл SSRF → download →
trafilatura → WEB_TEXT → LLM-граница (401 → FAILED/LLM_FAILED); SIGINT graceful

### Phase 4 — Architecture checkpoint — DONE

APPROVED @ a1c0e1b1c7684b72fc4bf2ed6c8ad8345007f6c7 (Orchestrator, GitHub review
pullrequestreview-5188485235). Аудит подтвердил каркас; по ходу ревью закрыты
2 MAJOR: FAILED сохраняет durable checkpoint (retry без повторных download/LLM),
сходящаяся дедупликация URL. Урок: заявления в REVIEW REQUEST проверять фактическим
diff/запуском до отправки.

Completed:
✓ E2E control flow: один канонический пайплайн (Telegram → ingestion → Item →
  claim → pipeline.extract/analyze/prioritize → READY → notify); параллельных
  pipelines нет — WEB/TEXT расходятся только в extract-шаге
✓ Abstractions: LlmProvider и injectable-зависимости имеют реальных потребителей
  (Fake в тестах + OpenAI в prod); бесполезных интерфейсов/wrapper-ов не найдено
✓ Worker correctness: atomic claim (UPDATE...RETURNING + условие QUEUED),
  concurrent-тесты, requeue stale с сохранением стадии, FAILED с error_code —
  реализовано в Phase 1–3, подтверждено
✓ Database: FK pragma, unique (user_id, message_id, source_index) и
  (user_id, source_url), transactions на переходах, fresh+upgrade миграции
✓ Web security: SSRF pinning/redirects/byte-cap; prompt injection изоляция в
  system prompt; content-analysis модель без инструментов
✓ Error recovery: WEB_TEXT персистент, LLM retry не перекачивает страницу
✓ Dead code: удалена мёртвая ветка _render_with_playwright (всегда-raise метод
  после жёсткого отключения playwright) — упрощён _fallback
✓ MAJOR-fix: mark_failed больше не затирает processing_stage — FAILED сохраняет
  durable checkpoint; retry из ANALYZING переиспользует WEB_TEXT (extractor
  calls==1), retry из PRIORITIZING завершает priority без LLM (обе регрессии
  фактически в tests/test_web.py)
✓ MAJOR-fix: _ingest_web_urls — сходящаяся дедупликация URL (bounded re-resolve);
  регрессия конкурентных пересекающихся URL (tests/test_ingestion.py)
✓ Test quality: усилены слабые assert'ы; фактические правки проверены (все
  заявленные в аудит-записи изменения присутствуют в diff)

Remaining:
□ — нет (ждёт вердикта Orchestrator'а)

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 84 passed
(+ Phase 4 регрессии фактически в suite: FAILED сохраняет checkpoint и retry
без повторного download/LLM; конкурентная дедупликация пересекающихся URL)

### Phase 5 — Voice/audio — DONE

APPROVED @ 05bb0ee1bbe2a8370a9d5e2df37b64bcc4c84ed7 (Orchestrator, GitHub review
pullrequestreview-5188651120). Ревью прошло три круга: temp cleanup/byte-cap,
durable oversized Item (atomic), Telegram retry policy + permanent-классификация
aiogram errors, STT TIMEOUT маппинг, token redaction в логах.

Completed:
✓ Voice/audio ingestion: ingest_voice → Item QUEUED с source_file_id
  (идемпотентно по (user_id, message_id, source_index)); handler persist→ACK
✓ Размер-лимит в handler (до очереди) и в downloader (после get_file/скачивания)
✓ TranscriptionProvider (отдельный Protocol): whisper-эндпоинт OpenAI — другой
  API/модель, отдельный adapter (OpenAiTranscriptionProvider)
✓ AudioExtractor: downloader → temp файл → STT → NormalizedContent; partial/temp
  файлы удаляются при ошибке/отмене в каждом раунде; byte-cap инкрементально
  при скачивании (работает без file_size); TRANSCRIPT персистится атомарно с
  checkpoint'ом ANALYZING — resume не повторяет download+STT (resume-тест с
  полным равенством NormalizedContent, включая duration_seconds)
✓ Ошибки: TOO_LARGE (permanent) / DOWNLOAD_FAILED / TRANSCRIPTION_FAILED / TIMEOUT
  (APITimeoutError от STT SDK маппится отдельно)
✓ Retry policy на Telegram boundary: transient 3 attempts с backoff, permanent
  (TOO_LARGE/4xx/not-found/BadRequest) — одна попытка
✓ TokenRedactionFilter: токен не попадает в httpx/httpcore INFO-логи
  (regression: caplog INFO при media download без TESTTOKEN)
✓ Oversized media: атомарный durable FAILED/TOO_LARGE Item с file_id/duration
  (ТЗ §66), пользователю — реальный лимит из конфига; ingest_voice(too_large=...)
  создаёт его сразу, без claimable QUEUED-состояния (race-fix по вердикту adb43fe)
✓ Resume-тест: pydantic equality initial/resumed NormalizedContent, включая
  duration_seconds; user_note пустая строка нормализуется в None
✓ Миграция d1b5bdba99a3 (source_file_id, content_duration_seconds)

Remaining:
□ Blocked (external): live STT/Telegram download — нет токена/ключа; путь покрыт
  fakes (FakeDownloader/FakeTranscriber)

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 100 passed
(+ downloader: transient 503→success (реальные 2 HTTP-попытки), not-found →
1 attempt, TelegramBadRequest → 1 attempt, oversized streaming без file_size,
partial cleanup; атомарный oversized Item; resume с полным equality
NormalizedContent включая duration; STT TIMEOUT маппинг; token redaction —
TESTTOKEN отсутствует в INFO-логах при media download)
smoke: headless старт без токена — bot disabled, SIGINT graceful

### Phase 6 — YouTube — DONE

APPROVED @ eeeb6cbe9c97a7c3d25899f36599ef0b74aa0fc8 (Orchestrator, GitHub review
pullrequestreview-5188879730). Ревью прошло три круга: composition root wiring,
resource invariants (owned temp subdir, byte-cap, retry), checkpoint equivalence
(canonical url/cues/duration), VTT header case-фикс, nocookie классификация,
malformed track regression.

Completed:
✓ Разбор YouTube URL: source_type=YOUTUBE (youtube.com/youtu.be/music/nocookie
  hosts); WEB-классификация остальных
✓ YoutubeExtractor: yt-dlp Python API (без shell/subprocess), noplaylist=True,
  playlist URL → UNSUPPORTED_SOURCE (один URL — максимум одно видео)
✓ Метаданные: title, description (excerpt в contents DESCRIPTION), duration,
  canonical webpage_url
✓ Транскрипт: human subtitles → automatic captions → аудио+STT fallback;
  пригодность субтитров ≥ 40 символов; при пригодных субтитрах STT не вызывается
✓ VTT/SRT парсер: теги/заголовки/дубликаты реплик, timestamps в metadata_json
✓ Лимиты: duration cap (TOO_LARGE permanent), max_filesize аудио (TOO_LARGE),
  max_subtitle_bytes (инкрементальный cap при streamed загрузке субтитров),
  socket_timeout — всё через Settings/composition root
✓ Fallback chain: перебор ВСЕХ кандидатов субтитров (human → auto, config langs →
  любые) до первого пригодного; ЛЮБАЯ ошибка кандидата (включая TOO_LARGE и
  malformed track без url) делает его непригодным и цепочка продолжается;
  STT только после исчерпания кандидатов
✓ Строгий порядок human → auto: валидные human на любом языке предпочтительнее
  auto на preferred-языке (регрессия: human de + auto ru → выбран human de,
  auto endpoint не запрашивался)
✓ Malformed subtitle track (без url) не роняет fallback chain (регрессия)
✓ Cues нормализуются к list[list] при extract — JSON round-trip сохраняет
  равенство initial/resumed NormalizedContent
✓ TRANSCRIPT + DESCRIPTION персистятся атомарно с checkpoint'ом ANALYZING;
  resume из ANALYZING без повторного yt-dlp/STT (D-001)
✓ Temp audio удаляется после STT (успех/ошибка)

Remaining:
□ Blocked (external): live yt-dlp против реального YouTube — отложен до Phase 12
  (rate-limits/geo); путь покрыт фейковым ydl_factory + MockTransport

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 122 passed
(+ subtitles VTT/SRT/dedup + Kind:/Language: case-фикс; fallback chain: human
unusable → auto → STT; subtitle oversize/transient retry; playlist reject;
duration cap; TRANSCRIPT/DESCRIPTION persistence + resume с полным equality
NormalizedContent; youtube URL классификация + www.youtube-nocookie; production
composition test)

### Phase 7 — Video visual analysis — DONE

APPROVED @ 0082e965471eb4e420046d4b460bfafdabc3d0bc (Orchestrator, GitHub review
pullrequestreview-5189044462). Ревью прошло два круга: durable visual notes
(persist до Analyzer, resume без повторного vision), ffmpeg off event loop,
graceful mkdir, 720p bound, 800-char output bound.

Completed:
✓ LlmCapabilities (structured_output/vision) — vision доступен только при
  сконфигурированной OPENAI_VISION_MODEL; LlmProvider protocol расширен
  describe_images (ТЗ §24, §27)
✓ YoutubeExtractor.download_video (best<=720p, filesize cap, noplaylist)
✓ frames.py: ffmpeg periodic sampling (args list, без shell, timeout), лимит
  VIDEO_MAX_FRAMES, дедупликация идентичных кадров по хэшу
✓ Pipeline: visual enrichment после персистенции TRANSCRIPT (graceful —
  vision failure/ffmpeg missing/отмена не роняют Item с транскриптом, ТЗ §39);
  VISUAL_NOTES персистится; analysis_completeness =
  TRANSCRIPT_AND_VISUAL / TRANSCRIPT_ONLY / FULL_TEXT
✓ VISUAL NOTES передаются анализатору как untrusted input
✓ Миграция 1eb65025a8b6 (items.analysis_completeness)
✓ Конфиг: VIDEO_FRAME_INTERVAL_SECONDS, VIDEO_MAX_FRAMES, OPENAI_VISION_MODEL

Remaining:
□ Blocked (external): live vision (реальный OpenAI) и реальный ffmpeg — нет
  ключа/бинарника; путь покрыт фейками (FakeLlmProvider.describe_images,
  injectable frames runner)

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 132 passed
(+ visual persistence до Analyzer и reuse на retry (describe/frames calls == 1),
frames off event loop, mkdir failure → READY TRANSCRIPT_ONLY, notes truncation 800,
720p bound без /best, video byte limit enforced отдельно от audio,
malformed-track regression)
smoke: headless старт без токена — bot disabled, SIGINT graceful

### Phase 8 — User profile — DONE

APPROVED @ 118ebe58b0ae8b5bc3dc9d7c0e6267727131b0d9 (Orchestrator, GitHub review
pullrequestreview-5192117648). Finalization commit after approval; approved HEAD
was product-code-clean and the preceding delta was docs-only.

Completed:
✓ users.profile_json (миграция 220d7ae6d4f1); profile seed через YAML
  (profile.example.yaml, load_profile_seed, apply_profile_seed на старте,
  ленивый seed для пользователей, созданных после старта)
✓ ProfilePatch (extra=forbid) + ConstraintEntry(key, value): natural language →
  strict Structured Outputs → валидация; field-level merge — незатронутые поля
  не теряются
✓ OpenAiProvider.profile_update: strict schema + валидация, ошибки
  LLM_FAILED/INVALID_LLM_OUTPUT; FakeLlmProvider.profile_update в тестах
✓ /profile_update: durable ProfileUpdateJob + быстрый ACK; фоновый
  ProfileUpdateWorker — LLM + DB-side атомарный json_patch merge, job DONE
  той же транзакцией; startup recovery RUNNING → PENDING; уведомление на
  telegram_chat_id
✓ /profile (format_profile, показывает constraints)
✓ Pipeline: analyzer получает профиль пользователя из БД (не default)
✓ Регрессии: persistence across sessions, merge без потери полей, constraints
  entries → dict, invalid patch rejected, seed yaml + missing file no-op,
  lazy seed, /profile показ, /profile_update enqueue, analyzer-from-DB,
  recovery idempotency, concurrent disjoint merge

Remaining:
□ Blocked (external): live LLM profile_update — нет ключа; путь покрыт фейками

Last verification:
ruff check . → pass; ruff format --check . → pass; pytest → 152 passed
smoke: headless старт без токена — bot disabled, SIGINT graceful

### Phase 9 — Today/inbox/search — DONE

APPROVED @ 591cd2e8e7279b725977341caa56dd31fa43899f (Orchestrator, GitHub review
5192209948). Status finalized after external approval; PR #10 is ready for
merge with the protocol handshake.

PR #10 открыт: `phase/09-today-inbox-search` → `main`.

Completed:
✓ `TodayService`: READY + ACTIVE actionable Items (ACTION/LEARN/READ/WATCH),
  sorted by priority descending and creation time ascending; default 3,
  absolute maximum 5; DONE/SNOOZED/REFERENCE and other ineligible Items excluded
✓ `/today`, `/inbox`, `/category`, `/search` handlers and compact formatting
✓ SQLite FTS5 `item_search` migration with application-controlled indexing;
  title, summary, user note, tags, and all `contents` text are searchable
✓ search results are user-scoped, ranked by FTS relevance, and include
  DONE/ARCHIVED Items; old Items are rebuilt into the user's index on search
✓ category counts/items and inbox latest-items retrieval without a categories
  table or pagination framework

Remaining:
□ ждать вердикта Orchestrator'а; исправления при необходимости выполнять в
  этой же ветке и PR

Last verification:
ruff check . → pass; ruff format --check . → pass;
pytest → 159 passed; fresh migration creates `item_search` and retrieval tests
cover Today filtering/sorting/limits, FTS title/transcript/web/archived search,
inbox/category scoping, and READY-to-FTS projection synchronization; headless
smoke with empty Telegram token → startup, migration, and graceful SIGINT pass.

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
