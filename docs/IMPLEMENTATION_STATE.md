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
