# Implementation State

Точка входа для новой сессии агента после обрыва предыдущей: файл показывает,
что сделано, что в работе, что заблокировано извне и какая проверка проходила
последней. История чата источником истины не является.

Статусы: `NOT_STARTED` / `IN_PROGRESS` / `IN_REVIEW` / `DONE` / `BLOCKED`.

Правило статусов (оркестрационный протокол §25): `IN_REVIEW` = реализация завершена, PR открыт и отправлен `REVIEW REQUEST` Orchestrator'у; `DONE` ставится только после явного `APPROVED` Orchestrator'а (или непосредственно перед разрешённым им merge); `BLOCKED` — только реальный внешний блокер. Фаза в `IN_REVIEW` не расширяется по scope: исправления идут в ту же branch и PR.

Архитектурные решения фиксируются отдельно — в `docs/DECISIONS.md`.

## Phases

| # | Этап | Статус |
|---|------|--------|
| 0 | Project contract | DONE |
| 1 | Skeleton | NOT_STARTED |
| 2 | Text end-to-end | NOT_STARTED |
| 3 | Web ingestion | NOT_STARTED |
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

Remaining:
□ GitHub remote + push: создание репозитория заблокировано отсутствием
  GitHub-аутентификации на машине (gh не установлен, SSH-ключ не привязан,
  HTTPS-креденшелов нет). Нужен `gh auth login` либо ручное создание
  репозитория пользователем; после этого — add remote + push.

Last verification:
diff «ТЗ ↔ docs/PRODUCT_SPEC.md» — различие только в служебной шапке
(pytest/ruff неприменимы: кода ещё нет)

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
