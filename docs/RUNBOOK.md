# Runbook

Как запускать, проверять и диагностировать Personal AI Inbox.
Заполняется по мере появления реальных команд и проблем; не выдумывать команды заранее.

Статус: этап 0 — кода ещё нет, существуют только контракты.

## Контракты репозитория (доступно уже сейчас)

Проверить, что PRODUCT_SPEC не разошёлся с исходным ТЗ (ожидаемое различие —
только служебная шапка в начале файла):

```bash
diff "ТЗ_ Personal AI Inbox - интеллектуальный Telegram TODO.md" docs/PRODUCT_SPEC.md
```

- Состояние этапов: `docs/IMPLEMENTATION_STATE.md`
- Архитектурные решения и инварианты: `docs/DECISIONS.md`
- Правила реализации: `AGENTS.md`

## Repository

- GitHub: https://github.com/ShabanovBoris/aiinbox (public), remote `origin`, default branch `main`
- Локальный git инициализирован 2026-09-12 (branch `main`, первый commit `f0448e6`)
- Аутентификация GitHub CLI (воспроизводимая процедура):

```bash
gh auth status      # если не аутентифицирован:
gh auth login       # device flow
gh auth setup-git   # git credential helper для push по HTTPS
```

- Защита `main` (server-side): branch protection включена — только через PR,
  force push и deletion запрещены, linear history обязательна.

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

## Quality gate (появится в Phase 1 вместе с кодом)

```bash
ruff check .
ruff format --check .
pytest
```

## Запуск приложения (появится в Phase 1)

Заполняется на Phase 1: локальный запуск, переменные окружения, миграции.

## Диагностика

Записывается по мере возникновения реальных эксплуатационных проблем.
