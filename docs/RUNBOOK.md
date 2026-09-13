# Runbook

Как запускать, проверять и диагностировать Personal AI Inbox.
Заполняется по мере появления реальных команд и проблем; не выдумывать команды заранее.

Статус: Phase 9 — Today/inbox/search реализуется в ветке
`phase/09-today-inbox-search`; PR ещё не открыт.

FTS5 индекс `item_search` контролируется приложением: после обработки Item он
синхронизируется вместе с READY, а перед поиском индекс пользователя
пересобирается. Это позволяет искать также в `contents` и автоматически
подхватывать Items, созданные до FTS-миграции.

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
                       # OPENAI_API_KEY, OPENAI_ANALYSIS_MODEL
uv run python -m app.main
```

`app.main` сам применяет миграции (`alembic upgrade head`), затем запускает
Telegram polling и processing workers. Без `TELEGRAM_BOT_TOKEN` приложение
стартует в headless-режиме (только воркеры) — локальный smoke без Telegram network.
Для анализа нужен реальный `OPENAI_API_KEY`; без него (или с невалидным ключом)
Item'ы уходят в FAILED с error_code=LLM_FAILED — happy path LLM проверяется
FakeLlmProvider'ом в тестах, live-проверка требует ключа.

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

- Зависшие `PROCESSING` после падения процесса возвращаются в `QUEUED`
  автоматически при следующем старте (`requeue_stale`).
- Retry из Telegram появится в Phase 10; до этого повторную обработку FAILED
  можно запустить вручную (stage сохраняется, retry продолжит с durable
  checkpoint'а; error-поля очищаются, чтобы не остаться на успешном READY):
  `UPDATE items SET processing_status='QUEUED', error_code=NULL, error_message=NULL WHERE id=...`.
