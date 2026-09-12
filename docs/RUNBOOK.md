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

Локальный git-репозиторий инициализирован 2026-09-12 (branch `main`).
GitHub remote — пока не создан: требует GitHub-аутентификации на машине
(`gh auth login`) либо ручного создания репозитория пользователем.
После создания:

```bash
gh repo create aiinbox --private --source=. --remote=origin --push
```

## Внешняя ревью-проверка (ChatGPT через Browser Use)

Каждый этап отправляется на верификацию в фиксированную беседу ChatGPT
(правило: `AGENTS.md` §57). Вложение — основной diff и архив проекта:

```bash
git diff <last_reviewed_sha>..HEAD > temp/review.diff
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
