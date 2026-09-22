# Personal AI Inbox — Product Contract

> Этот файл описывает **текущее поддерживаемое поведение**, а не историю реализации.
> Пользовательская справка: `docs/BOT_USAGE.md`.
> Операционная эксплуатация: `docs/RUNBOOK.md`.
> Архитектурные инварианты: `docs/DECISIONS.md`.

## 1. Цель проекта

Personal AI Inbox принимает входящий материал, извлекает его содержимое, анализирует,
сохраняет, приоритизирует и возвращает пользователю небольшой actionable список.

Основной сценарий:

```text
увидел полезное → отправил боту → забыл
                         ↓
             система сама организует,
             оценивает и возвращает позже
```

## 2. Главный продуктовый принцип

Это не bookmark manager и не ручной TODO. Система должна минимизировать внимание,
которое пользователь тратит на сортировку входящего потока.

## 3. Ключевые архитектурные требования

- single-process modular monolith;
- SQLite — canonical durable state;
- тяжёлая обработка только в background workers;
- внешние системы изолированы adapters;
- restart/retry не должны терять уже выполненную дорогую работу;
- никаких Redis/Celery/Kafka/PostgreSQL без доказанной необходимости.

### 3.1. Минимизация сложности

Предпочитать простой код, явные транзакции, bounded concurrency и текущие
библиотеки проекта. Не строить placeholder architecture для будущих клиентов.

## 4. Технологический стек

Текущий стек:

```text
Python 3.12
aiogram
SQLAlchemy 2 + aiosqlite
Alembic
Pydantic 2 / pydantic-settings
httpx + trafilatura
yt-dlp
ffmpeg / ffprobe
OpenAI SDK-compatible providers
pytest / pytest-asyncio
ruff
```

Playwright runtime отключён: безопасной browser network boundary в проекте нет.

## 5. Архитектура верхнего уровня

```text
Telegram → ingestion → SQLite queue → ProcessingWorker
                                 ↓
                         extraction/normalization
                                 ↓
                           LLM Analyzer
                                 ↓
                        PriorityEngine
                                 ↓
                         READY + outbox
                                 ↓
                             Telegram
```

Напоминания обслуживает `ReminderWorker`, immediate READY/FAILED/profile
notifications — durable `DeliveryWorker`.

## 6. Текущая поддерживаемая поверхность

Поддерживаются:

- plain text;
- web URL;
- YouTube URL;
- Telegram voice;
- Telegram audio;
- `/today`, `/inbox`, `/category`, `/search`;
- `/profile`, `/profile_update`;
- `/settings`;
- Done / Later / Archive / Retry;
- daily digest и snooze resurfacing.

Direct Telegram video, video note, image и document handlers отсутствуют.

## 7. Текущие non-goals

Не реализованы и не должны появляться без отдельной задачи:

- Android client / HTTP API;
- web frontend;
- embeddings / vector database / RAG;
- ML ranking;
- Ollama/local provider;
- Calendar/Notion integrations;
- browser fallback для сложных страниц;
- playlists, DRM/paywall/CAPTCHA bypass;
- direct Telegram image/video/document ingestion.

## 8. Структура проекта

`app/` разделён на bot adapters, extractors, LLM adapters, services, workers,
storage и domain. Business logic не должна зависеть от Telegram/OpenAI SDK напрямую.

## 9. Модель Item

`Item` хранит source identity, processing status/stage, lifecycle state,
analysis fields, priority, error state и timestamps. Длинное содержимое хранится
отдельно в `contents`.

## 10. Processing status и lifecycle state

Разделять:

```text
ProcessingStatus: QUEUED / PROCESSING / READY / FAILED
ItemState:        ACTIVE / SNOOZED / DONE / ARCHIVED
```

Retry меняет processing status; Done/Later/Archive — lifecycle state.

## 11. ItemType

Поддерживаются:

`ACTION`, `LEARN`, `READ`, `WATCH`, `IDEA`, `REFERENCE`, `SOMEDAY`.

## 12. Категории

Категория — динамическая строка из анализа, не enum.

## 13. Обработка Telegram сообщения

- plain text без URL → один TEXT Item;
- одно или несколько URL → отдельный Item на каждый нормализованный URL;
- окружающий URL текст сохраняется как `user_note`;
- URL дедуплицируются per user;
- YouTube URL определяется отдельно от WEB.

## 14. Работа Telegram handler

Handler обязан быстро:

1. проверить allowlist;
2. валидировать вход;
3. сохранить durable Item/job;
4. commit;
5. отправить ACK.

Extraction/LLM/ffmpeg в handler запрещены.

## 15. Фоновая обработка

`ProcessingWorker` атомарно claim'ит QUEUED Item, запускает pipeline и доводит
его до READY или FAILED. DB infrastructure failures выходят к process supervisor.

## 16. ContentExtractor API

Extractors переводят внешний source в единый `NormalizedContent`. Source-specific
детали не должны протекать в Analyzer/PriorityEngine.

## 17. NormalizedContent

Нормализованный контент содержит source type, text и опциональные title/url,
user note, author/language/duration/metadata.

## 18. Web page extraction

WEB использует SSRF-safe HTTP transport, bounded redirects/download, trafilatura.
Playwright fallback отключён.

## 19. Критерий успешного extraction

Результат должен содержать содержательный текст. Слишком короткая/неизвлекаемая
страница завершается контролируемым `EXTRACTION_FAILED`.

## 20. Защита web extractor от SSRF

Разрешены только HTTP(S) публичные адреса. Private/loopback/link-local и иные
запрещённые диапазоны блокируются. DNS валидируется и соединение pin'ится к
проверенному IP; каждый redirect проверяется заново.

## 21. Prompt injection protection

External content — данные, не инструкции. LLM prompt должен отделять task/system
instructions от web/transcript/user content. LLM provider не получает tools.

## 22. YouTube/video pipeline

YouTube: metadata → subtitles при наличии → иначе audio download + STT.
Vision — опциональный дополнительный проход по representative frames.

## 23. Анализ визуальной части видео

Frame extraction bounded до materialization и сохраняет temporal coverage.
Кандидаты periodic/scene разрежаются и deduplicate-ятся до configured limit.

## 24. Vision должен быть capability

Vision выполняется только если provider declares `capabilities.vision`.
Ошибка vision не должна ломать валидный transcript-only результат.

## 25. Временные файлы

Media/frames живут только в configured temp directory и удаляются после обработки.
В Docker temp directory — tmpfs.

## 26. Audio / Voice

Telegram voice/audio сохраняются как durable Item по `file_id`, скачиваются worker-ом,
транскрибируются provider adapter-ом и дальше проходят общий pipeline.

## 27. LLM abstraction

Domain/services работают через provider boundary. Model IDs, credentials и
base URLs принадлежат composition/config layer.

## 28. Providers

Поддерживаются:

- `LLM_PROVIDER=openai`;
- `LLM_PROVIDER=openrouter`.

OpenRouter использует OpenAI-compatible analysis/vision/STT adapters с отдельными
credentials/model IDs. Ollama не реализован.

## 29. Конфигурация модели

Все provider/model settings задаются environment variables.
Business code не содержит hardcoded model IDs.

## 30. Structured output

Analyzer обязан возвращать schema-validated `AnalysisResult`. Парсинг
произвольного prose регулярками запрещён.

## 31. Ограничения AnalysisResult

Структура включает title, summary, category, ItemType, tags, priority factors,
estimated action, next action, reason, language и confidence. Pydantic валидирует
shape/types.

## 32. Long content

Длинный текст анализируется через bounded chunking + intermediate summaries,
после чего выполняется aggregate/final analysis.

## 33. Chunking

Chunk boundaries paragraph-aware. Durable `CHUNK_SUMMARY` reuse разрешён только
при совпадении index, settings и SHA-256 exact chunk text.

## 34. Пользовательский профиль

Профиль хранится per user и участвует в анализе будущих Items.

## 35. UserProfile

Профиль может содержать profession, domains, weighted goals, interests,
constraints и free text.

## 36. /profile

`/profile` показывает текущий профиль.
`/profile_update <instruction>` создаёт durable background job.
Старые Items автоматически не re-analyze-ятся.

## 37. Priority Engine

Финальный `priority_score` считает deterministic code, а не LLM.

## 38. Базовая формула priority

Текущие веса:

```text
goal_fit        0.30
importance      0.20
urgency         0.15
long_term_value 0.15
interest_fit    0.10
quick_win       0.10
```

Результат clamp'ится в 0..100.

## 39. Quick win

`quick_win = max(0, 1 - estimated_action_minutes / 60)`.
Если duration неизвестна — нейтральное значение 0.5.

## 40. Приоритет ≠ тип

ItemType описывает характер материала; priority — персональную полезность/срочность.
Они не заменяют друг друга.

## 41. Today selection

`TodayService` выбирает только READY + ACTIVE Items типов ACTION/LEARN/READ/WATCH,
сортирует по priority desc и ограничивает результат.

## 42. /today

По умолчанию возвращает 3 Items, hard max — 5.

## 43. /inbox

Возвращает последние Items без lifecycle-фильтра, максимум 20.

## 44. /category

Без аргумента — категории + counts; с аргументом — Items категории, максимум 20.

## 45. /search

SQLite FTS5 ищет title, summary, user note, tags и persisted content.
DONE/ARCHIVED остаются searchable. Default limit — 10.

## 46. Почему хранить extracted content

Сохранять именно тот текст/transcript, который анализировался: source может
измениться или исчезнуть; persisted content нужен для resume/search/re-analysis.

## 47. Database schema

Canonical SQLite tables:

- `users`;
- `items`;
- `contents`;
- `events`;
- `reminders`;
- `profile_update_jobs`;
- `deliveries`;
- FTS5 virtual table `item_search`.

Schema changes — только Alembic migrations.

## 48. contents

Поддерживаемые kinds:

`USER_TEXT`, `WEB_TEXT`, `TRANSCRIPT`, `TRANSCRIPT_CHUNK`,
`VISUAL_NOTES`, `DESCRIPTION`, `CHUNK_SUMMARY`.

`TRANSCRIPT_CHUNK` — retry checkpoint и удаляется после успешной сборки final transcript.

## 49. events

Lifecycle/user feedback events пишутся в той же транзакции, что выигравший state
transition. Повторный callback не должен создавать второй event.

## 50. reminders

`reminders` хранит daily digest/snooze scheduling.
`deliveries` — отдельный durable outbox для READY/FAILED/profile notifications.

## 51. Daily digest

Настройки: enabled, local time, quiet hours + user timezone.
Digest создаётся не чаще одного раза за локальный день и использует TodayService.

## 52. Snooze

Later предлагает tomorrow/week/month. Item становится SNOOZED.
При due time возвращается ACTIVE и получает reminder notification вне quiet hours.

## 53. Done

Atomic transition → `DONE` + `completed_at` + event.

## 54. Archive

Atomic transition → `ARCHIVED` + `archived_at` + event.
Archived Item остаётся searchable.

## 55. Retry

Только FAILED → QUEUED. Error fields очищаются, processing checkpoints сохраняются.
Pending stale `ITEM_FAILED` delivery отменяется атомарно.

## 56. Error handling

Пользователь получает короткое сообщение без stacktrace; техническая причина —
в structured logs и durable error fields. Item не исчезает.

## 57. Error codes

Основные коды: `UNSUPPORTED_SOURCE`, `DOWNLOAD_FAILED`, `TOO_LARGE`,
`EXTRACTION_FAILED`, `TRANSCRIPTION_FAILED`, `LLM_FAILED`,
`INVALID_LLM_OUTPUT`, `TIMEOUT`, `PROCESSING_TIMEOUT`,
`SECURITY_REJECTED`, `UNKNOWN`.

## 58. Retry policy

Transient external failures: bounded attempts + exponential backoff.
Permanent 4xx/security/unsupported/invalid input не retry-ятся.

## 59. LLM failure

LLM failure переводит Item в FAILED, не удаляя extraction/transcript/checkpoints.
Retry продолжает с максимально глубокого compatible durable checkpoint.

## 60. Processing stages

`processing_stage` отражает глубину прогресса отдельно от status.
Resume обязан использовать persisted content/analysis вместо повторения дорогих calls.

## 61. Content deduplication

URL dedup — per user + normalized URL. Semantic duplicate detection отсутствует.

## 62. URL normalization

Удалять fragment и tracking-параметры, сохранять meaningful query.
Нормализованный URL участвует в dedup.

## 63. Security Telegram

Только IDs из `ALLOWED_TELEGRAM_USER_IDS`. Неавторизованные updates молча игнорируются.

## 64. Secrets

Tokens/API keys только environment/.env; `.env` не коммитится и не логируется.

## 65. Subprocess security

yt-dlp вызывается Python API. ffmpeg запускается argv-list без shell interpolation.

## 66. Ограничения файлов

Defaults:

- Telegram voice/audio: 20 MB;
- web download: 5 MB;
- YouTube duration: 7200 s;
- YouTube audio/video: 50 MB;
- subtitles: 2 MB.

Все значения задаются config и валидируются на startup.

## 67. Логи

Логировать IDs/source/stage/duration/result/error code, но не secrets и не полный
private content.

## 68. Performance

Background concurrency bounded. Один slow Item не должен блокировать Telegram handler.
No unbounded in-memory/media fan-out.

## 69. UX обработки длинного видео

Ingestion отвечает быстро. Длинная работа идёт в фоне; результат приходит после READY.
Если vision недоступен, UI должен честно обозначить transcript-only analysis.

## 70. Open button

URL Item получает Telegram `🔗 Открыть`, ведущую на source URL.

## 71. Персонализация

Текущая персонализация: profile factors + deterministic priority.
Events собираются, но не обучают модель автоматически.

## 72. Будущая персонализация

Behavior-aware ranking возможен только отдельной задачей после наличия реальных данных.

## 73. Testing strategy

Default suite offline: fake providers/local fixtures, без OpenAI/Telegram/YouTube network.
Concurrency, restart, security и persistence paths покрываются regression tests.

## 74. Integration tests

Проверять pipeline end-to-end с fake adapters, migrations и transactional semantics.

## 75. Extractor tests

Проверять limits, retries, SSRF, subtitle/STT fallback, ffmpeg command contracts и cleanup.

## 76. Quality gate

Перед merge required:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions job `quality` — required status check для `main`.

## 77. Code style

Прямолинейный typed Python, минимальные abstractions, без premature generalization
и скрытой инфраструктуры.

## 78. README

README содержит setup/run/Docker/config/quality обзор и ссылки на подробные docs.

## 79. .env.example

Все поддерживаемые runtime settings должны иметь понятный пример без secrets.

## 80. Docker

Production image:

- pinned Python 3.12 base;
- dependencies из `uv.lock` через frozen install;
- `ffmpeg`;
- non-root user;
- SQLite volume `/data`;
- temp media в `/tmp/aiinbox` tmpfs;
- без Chromium/Playwright dependencies.

## 81. Graceful shutdown

SIGTERM/SIGINT останавливает приём новой работы, даёт workers bounded drain,
затем отменяет оставшиеся tasks и закрывает Telegram/DB resources.
Infrastructure failure критического task приводит к process exit; restart
восстанавливает PROCESSING/SENDING/RUNNING durable state.

## 82. Operational health

`python -m app.ops status` показывает размер DB, QUEUED/PROCESSING/FAILED,
pending deliveries, configured provider/model, worker concurrency и Telegram
configuration без credentials. Docker healthcheck использует тот же локальный
DB/config boundary; unexpected critical worker exit завершает основной процесс,
после чего внешний supervisor выполняет restart.

## 83. SQLite backup / restore

Live SQLite backup создаётся через Online Backup API и после создания проходит
`PRAGMA integrity_check`. Backup generations хранятся отдельно от live DB и
ротируются bounded числом. Restore создаёт новый DB-файл и тоже проверяет его;
замена canonical database выполняется только после остановки приложения.

## 84. Deployment smoke

`python -m app.ops smoke` — явная live-проверка configured analysis provider
(OpenAI или OpenRouter) и Telegram API. Default test suite остаётся полностью
offline; live smoke запускается оператором после deployment с production env.

## 99. Основной критерий успеха продукта

После нескольких недель бессистемного сохранения материалов `/today` должен
выдавать небольшой, адекватный и персонально полезный список того, чем стоит
заняться сейчас.

## 100. Главная продуктовая идея

Строить не AI bookmark manager, а **Personal Attention Manager**: система сама
понимает входящий материал, оценивает его, предлагает действие и возвращает в
подходящий момент.
