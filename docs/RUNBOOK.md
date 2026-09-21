# Runbook

Как запускать, проверять и диагностировать Personal AI Inbox.
Заполняется по мере появления реальных команд и проблем; не выдумывать команды заранее.

Статус: Phase 11 — Notifications завершена; Phase 12 — Production hardening
реализована в ветке `phase/12-production-hardening` и ожидает external review.

После squash merge PR #12 Phase 11 завершена. Текущая разработка Phase 12
идёт в `phase/12-production-hardening`; запуск и проверка ниже описывают
актуальный `main`/phase-код.

FTS5 индекс `item_search` контролируется приложением: после обработки Item он
синхронизируется вместе с READY, а перед поиском индекс пользователя
пересобирается. Это позволяет искать также в `contents` и автоматически
подхватывать Items, созданные до FTS-миграции.

Item actions выполняются callback-кнопками Telegram и сохраняются транзакционно:
Done, Later (завтра/неделя/месяц), Archive и Retry для FAILED. События лежат в
таблице `events`; lifecycle state и timestamps — в `items`. При сбое обработки
пользователь получает кнопку Retry, а повтор действия не создаёт дубликат
события. Retry сохраняет `processing_stage`, поэтому уже извлечённый контент не
обрабатывается заново.

Уведомления Phase 11 используют SQLite-таблицу `reminders` как durable
идемпотентный журнал. `/settings` показывает настройки и принимает минимальные
изменения: `timezone Europe/Moscow`, `time 09:00`, `quiet 22:30-08:00`.
Daily digest отправляется один раз за локальный день через `TodayService`, а
отложенные Items возвращаются после `snoozed_until`, кроме quiet hours.
`ReminderWorker` запускается вместе с Telegram bot и не требует внешнего
scheduler.

## Production hardening

`ProcessingWorker` применяет `PROCESSING_TIMEOUT_SECONDS` к одной полной
обработке Item. При превышении Item получает `PROCESSING_TIMEOUT` и остаётся
доступным для Retry; обычный restart дополнительно возвращает все
`PROCESSING` Items в `QUEUED` через `requeue_stale`. При SIGTERM/SIGINT сначала
подаётся stop-сигнал и воркерам даётся `SHUTDOWN_TIMEOUT_SECONDS` на завершение
текущей операции, после чего зависшие задачи отменяются. Неожиданное завершение
processing/profile/reminder worker или Telegram polling валит весь процесс после
того же cleanup; DB infrastructure error также выходит наружу вместо маскировки
как обычный FAILED Item. В Docker Compose процесс поднимается снова через
`restart: unless-stopped`, а незавершённый PROCESSING Item requeue-ится на старте.

Для Docker см. корневой `README.md`: образ содержит Python 3.12 и ffmpeg,
SQLite должен быть вынесен в volume `/data`. Playwright fallback отключён по
решению безопасности, поэтому Chromium-зависимости в образ не устанавливаются.
В контейнере обязательно используйте абсолютный URL
`sqlite+aiosqlite:////data/app.db`, иначе relative SQLite path окажется под
`/app`, а не в persistent volume.

## Контракты репозитория

Проверить, что PRODUCT_SPEC не разошёлся с исходным ТЗ (ожидаемое различие —
только служебная шапка в начале файла):

```bash
diff "ТЗ_ Personal AI Inbox - интеллектуальный Telegram TODO.md" docs/PRODUCT_SPEC.md
```

- Состояние этапов: `docs/IMPLEMENTATION_STATE.md`
- Архитектурные решения и инварианты: `docs/DECISIONS.md`
- Журнал вердиктов ревью: `docs/REVIEWS.md`
- Правила реализации: `AGENTS.md`

## Быстрый старт

Требования: Python 3.12+, [uv](https://docs.astral.sh/uv/). macOS/Linux.

```bash
uv venv --python 3.12
uv sync
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS,
                       # и credentials/model ids выбранного LLM_PROVIDER
uv run python -m app.main
```

`app.main` сам применяет миграции (`alembic upgrade head`), затем запускает
Telegram polling и processing workers. Без `TELEGRAM_BOT_TOKEN` приложение
стартует в headless-режиме (только воркеры) — локальный smoke без Telegram network.
Для анализа нужен реальный ключ выбранного provider-а. Для OpenRouter задайте
`LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, analysis/transcription model ids
и при необходимости vision model; endpoint по умолчанию —
`https://openrouter.ai/api/v1`. Без валидного ключа Item'ы уходят в FAILED с
error_code=LLM_FAILED — happy path LLM проверяется FakeLlmProvider'ом в тестах,
live-проверка требует ключа.

OpenRouter STT использует OpenAI-compatible multipart только для коротких файлов.
Файлы больше 25 MB или аудио длиннее 5 минут сначала режутся `ffmpeg` на
5-минутные mono WAV PCM 16 kHz сегменты и транскрибируются последовательно.
Для long-audio/YouTube STT fallback `ffmpeg` должен быть доступен в `PATH`.

## Quality gate

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Git workflow (оркестрационный протокол §2–4, §22–23)

`main` — защищённая integration branch. Реализация фаз в `main` запрещена.

```bash
# перед началом фазы
git checkout main
git pull --ff-only
git status
git checkout -b phase/NN-short-description

# commits на ветке (формат feat:/test:/fix:/docs:), затем
git push -u origin phase/NN-short-description
# PR phase/NN-... → main, title "Phase NN: <short description>"
# REVIEW REQUEST Orchestrator'у (формат протокола §7)
```

- Merge — только Orchestrator, squash, заголовок `Phase NN: <description>`.
- Handshake APPROVED → merge: см. `AGENTS.md` §57 — после `APPROVED @ HEAD A`
  агент делает единственный status-finalization commit (HEAD B, delta
  docs-status-only) и объявляет `MERGE READY`; Orchestrator проверяет delta
  и squash-merges с ожидаемым HEAD B.
- После merge: `git checkout main && git pull --ff-only`, затем новая ветка.
- Запрещены: direct commit в `main`, force push, merge собственного PR,
  старт следующей фазы до `APPROVED`, несколько фаз в одном PR.

## Внешняя ревью-проверка (Orchestrator: ChatGPT через Browser Use)

Механизм — протокол §5–9: PR + `REVIEW REQUEST` в фиксированную беседу ChatGPT
(правило: `AGENTS.md` §57). Orchestrator проверяет GitHub напрямую (PR, diff, SHA).

- Каждый вердикт (`APPROVED` / `CHANGES REQUIRED` / `BLOCKED`) агент немедленно
  фиксирует в `docs/REVIEWS.md` (PR + reviewed HEAD SHA) — durable record,
  пережидающий смену сессий. Ограничение GitHub: автор PR не может оставить
  `REQUEST_CHANGES` на собственный PR, поэтому формальный PR review от identity
  Orchestrator'а может быть недоступен.
- Сопроводительный материал (когда Orchestrator попросит):

```bash
git diff main..HEAD > temp/review.diff
git archive --format=zip -o temp/project.zip HEAD
```

`git archive` упаковывает только git-tracked файлы, поэтому `.env`, `data/`,
`temp/` и прочие игнорируемые пути физически не покидают машину.
Сами `temp/review.diff` и `temp/project.zip` игнорируются git'ом.

## Диагностика

Инспекция базы (по умолчанию `data/app.db`):

```bash
sqlite3 data/app.db "SELECT id, processing_status, processing_stage, state, source_type FROM items ORDER BY created_at"
```

Провалившиеся Item'ы — с кодом и причиной (стек-трейсы только в логах):

```bash
sqlite3 data/app.db "SELECT id, error_code, error_message FROM items WHERE processing_status='FAILED'"
```

Состояние уведомлений и неудачные доставки:

```bash
sqlite3 data/app.db "SELECT id, user_id, item_id, type, scheduled_at, status, sent_at FROM reminders ORDER BY scheduled_at"
```

`SENT` означает, что delivery claim зафиксирован до отправки Telegram и после
перезапуска не будет создан повторно. `FAILED` означает, что Telegram-вызов
завершился ошибкой; техническая причина остаётся в логах приложения.

- Зависшие `PROCESSING` после падения процесса возвращаются в `QUEUED`
  автоматически при следующем старте (`requeue_stale`).
- Retry из Telegram доступен для FAILED Items; он сохраняет stage и продолжает
  с durable checkpoint'а, очищая error-поля. Ручной fallback при диагностике:
  `UPDATE items SET processing_status='QUEUED', error_code=NULL, error_message=NULL WHERE id=...`.
