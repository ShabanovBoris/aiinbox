# ORCHESTRATION PROTOCOL

С этого момента разработка проекта управляется внешним reviewer/orchestrator.

Ты являешься implementation agent.

Ты НЕ определяешь самостоятельно, когда переходить к следующему Phase.

## 1. Authority

Приоритет инструкций проекта:

1. Последнее решение Orchestrator.
2. Текущий утверждённый Phase prompt.
3. `AGENTS.md`.
4. `docs/PRODUCT_SPEC.md`.
5. `docs/DECISIONS.md`.
6. `docs/IMPLEMENTATION_STATE.md`.
7. Existing tests / implementation.

Если инструкции конфликтуют — останови только конфликтующую часть и сообщи Orchestrator.

Для обычных технических решений разрешение не требуется.

---

# 2. Git workflow

`main` является стабильной integration branch.

Никогда не реализовывай Phase непосредственно в `main`.

Для каждого Phase:

```text
main
 ↓
phase/NN-short-description
 ↓
implementation
 ↓
tests
 ↓
commit(s)
 ↓
push
 ↓
Pull Request → main
 ↓
Orchestrator review
```

Примеры:

```text
phase/01-skeleton
phase/02-text-pipeline
phase/03-web-ingestion
phase/04-architecture-review
phase/05-voice
```

Перед созданием branch:

```bash
git checkout main
git pull --ff-only
git status
```

Рабочее дерево должно быть понятным.

Не уничтожай чужие uncommitted changes.

---

# 3. main protection policy

Запрещено:

```text
direct commit to main
force push
rewriting published history
merging own PR
starting next phase before approval
combining multiple phases in one PR
```

Только Orchestrator определяет момент merge.

---

# 4. Commit policy

Во время Phase допускается несколько логических commits.

Commit должен отражать законченное изменение.

Предпочтительный формат:

```text
feat: add persistent processing worker
test: cover processing recovery
fix: prevent duplicate telegram ingestion
docs: update phase implementation state
```

Не делать отдельный commit для каждого небольшого редактирования.

Не создавать искусственно огромный single commit во время разработки, если несколько checkpoints облегчают восстановление.

Перед review история branch должна быть понятной.

После approval PR будет squash-merged, поэтому идеальная публичная commit history внутри feature branch не является самоцелью.

---

# 5. Pull Request

После завершения Phase создай PR:

```text
phase/NN-... → main
```

PR title:

```text
Phase NN: <short description>
```

Пример:

```text
Phase 03: Web ingestion
```

PR не должен содержать следующую Phase.

---

# 6. PR body

Каждый PR обязан содержать:

```markdown
## Phase
Phase NN — Name

## Goal
Краткое описание реализованного vertical slice.

## Implemented
- ...
- ...
- ...

## Architecture decisions
- только действительно значимые решения

## Database changes
- migrations / none

## Security impact
- изменения security-sensitive поведения / none

## Tests
Commands:
- `ruff check .`
- `ruff format --check .`
- `pytest`

Results:
- ...

## Manual verification
- сценарий
- результат

## Known limitations
- только реальные ограничения текущего Phase

## Deferred by scope
- вещи из следующих Phase, которые намеренно не реализованы

## Repository state
- branch:
- HEAD commit:
- target: main
```

Нельзя писать `tests pass`, если они фактически не запускались.

---

# 7. Handoff to Orchestrator

После открытия PR сообщи Orchestrator только необходимую для review информацию.

Обязательный формат:

```text
REVIEW REQUEST

Repository: owner/repository
Phase: NN — Name
Branch: phase/NN-name
PR: #123
HEAD: <full-or-short-sha>

Goal:
<1–3 предложения>

Verification:
- ruff check . → PASS
- ruff format --check . → PASS
- pytest → 42 passed
- manual smoke → PASS

Important decisions:
- ...
- ...

Known limitations:
- ...

Ready for review.
```

`Repository` и `PR` обязательны.

Без них review не считается начатым.

---

# 8. What Orchestrator will review

Orchestrator не основывает решение только на твоём summary.

Он может самостоятельно проверить GitHub:

- PR metadata;
- changed files;
- actual diff;
- commit SHA;
- tests / workflow runs;
- implementation details;
- architectural boundaries;
- migrations;
- error handling;
- security;
- docs;
- implementation state.

Поэтому summary должен быть точным.

Не скрывай компромиссы или failing checks.

---

# 9. Review outcomes

Orchestrator отвечает одним из трёх состояний.

## APPROVED

Phase принят.

Только после явного:

```text
APPROVED
```

разрешено считать Phase завершённым.

Далее выполняй инструкции Orchestrator относительно merge/следующей Phase.

---

## CHANGES REQUIRED

Phase не принят.

Orchestrator предоставит обязательные изменения.

Ты должен:

1. остаться в той же Phase branch;
2. исправить требования;
3. добавить/обновить regression tests;
4. повторно прогнать quality gate;
5. push новые commits в тот же PR;
6. отправить новый `REVIEW REQUEST`.

Не открывай новый PR без указания Orchestrator.

---

## BLOCKED

Используется только для реального внешнего blocker.

В этом случае выполняй всё, что остаётся возможным без blocker, и жди инструкции только по заблокированной части.

## 9.1 Durable record of verdicts

Каждый вердикт Orchestrator (`APPROVED`, `CHANGES REQUIRED`, `BLOCKED`) implementation agent обязан немедленно зафиксировать в `docs/REVIEWS.md` с привязкой к PR number и reviewed HEAD SHA.

Chat-беседа не является долговечным хранилищем решений. Формальный GitHub PR review предпочтителен, когда identity позволяет (ограничение GitHub: автор PR не может оставить `REQUEST_CHANGES` на собственный PR). Новая сессия восстанавливает состояние оркестрации из `docs/REVIEWS.md` + GitHub, а не из истории чата.

## 9.2 APPROVED → merge handshake (no TOCTOU)

```text
APPROVED @ HEAD A
↓ один finalization commit:
    docs/REVIEWS.md + APPROVED / PR / reviewed HEAD A
    docs/IMPLEMENTATION_STATE.md IN_REVIEW → DONE, approved HEAD = A
↓ HEAD B
↓ MERGE READY
Previous approved HEAD: A
New HEAD: B
↓ Orchestrator проверяет A..B: только review/state finalization
↓ squash merge expected HEAD B
↓ sync main
↓ только теперь следующая Phase
```

Финализирующий commit содержит ровно две разрешённые мутации: запись вердикта в `docs/REVIEWS.md` и перевод статуса фазы в `docs/IMPLEMENTATION_STATE.md`. Никаких иных изменений между APPROVED и merge.

---

# 10. Review findings severity

Считай замечания Orchestrator обязательными, если явно не указано обратное.

Возможные уровни:

```text
BLOCKER
MAJOR
MINOR
NIT
```

`BLOCKER` и `MAJOR` всегда исправляются до approval.

`MINOR` по умолчанию также исправляется до merge, если Orchestrator не пометил его как deferred.

`NIT` является необязательным только если это явно указано.

---

# 11. Never self-approve

Запрещено самостоятельно заявлять:

```text
phase accepted
review complete
ready to start next phase
```

Разрешено только:

```text
implementation complete
ready for external review
```

Acceptance принадлежит Orchestrator.

---

# 12. No scope expansion while waiting for review

После отправки `REVIEW REQUEST`:

не начинай следующий Phase.

Можно:

- отвечать на вопросы reviewer;
- исследовать замечания;
- исправлять текущий PR после `CHANGES REQUIRED`.

Нельзя:

- заранее реализовывать следующий Phase;
- менять unrelated архитектуру;
- добавлять bonus functionality.

---

# 13. Re-review discipline

После requested changes предоставь:

```text
RE-REVIEW REQUEST

Repository:
PR:
Previous reviewed HEAD:
New HEAD:

Resolved:
1. <finding> → <resolution>
2. <finding> → <resolution>

Tests added/changed:
- ...

Verification:
- ...

Remaining:
- none
```

Не проси reviewer самостоятельно выяснять, какие его замечания были исправлены.

---

# 14. Keep main green

Ни один PR не должен knowingly ломать:

- existing tests;
- migrations;
- startup;
- currently completed vertical slices.

Backward compatibility завершённых Phases является частью acceptance.

Если новая Phase обнаружила старый bug, исправить его можно в текущем PR, если fix непосредственно необходим.

Объясни это в PR.

---

# 15. Phase boundaries

Одна Phase должна давать один законченный increment.

Например:

```text
Phase 02:
text → analysis → priority
```

а не одновременно:

```text
text
web
voice
youtube
search
notifications
```

Если реализация неожиданно требует существенных изменений за пределами Phase:

не расширяй scope молча.

Сообщи Orchestrator:

```text
SCOPE DEVIATION

Required change:
Why:
Affected components:
Can current Phase proceed without it:
Recommended minimal solution:
```

---

# 16. Architecture changes

Не меняй базовые архитектурные решения без причины.

Особенно:

```text
SQLite
modular monolith
single processing pipeline
NormalizedContent boundary
replaceable LlmProvider
persistent intermediate results
deterministic PriorityEngine
Telegram as delivery adapter
```

Если считаешь архитектурное изменение необходимым:

сначала подготовь:

```text
ARCHITECTURE PROPOSAL

Problem:
Evidence:
Current limitation:
Proposed change:
Alternatives considered:
Migration impact:
Complexity impact:
Risk if unchanged:
```

Не реализуй значимый architectural pivot до решения Orchestrator.

Малые локальные refactorings разрешены самостоятельно.

---

# 17. Dependency additions

Перед добавлением значимой новой dependency:

убедись, что существующая dependency или standard library не решают задачу.

Если dependency является крупной инфраструктурной:

```text
Redis
Celery
RabbitMQ
Kafka
PostgreSQL
vector database
new framework
```

не добавляй её без approval Orchestrator.

Обычные небольшие библиотеки, прямо необходимые текущему Phase, можно добавлять самостоятельно.

---

# 18. Database discipline

Schema change обязательно сопровождается migration.

Reviewer должен иметь возможность проверить:

```text
fresh DB → migrate → run
```

При существенном изменении:

```text
existing DB → migrate → retain data
```

Не редактируй старую уже принятую migration для изменения будущего schema.

Создавай новую migration.

---

# 19. Test evidence

Перед review обязательно выполнить весь доступный локальный quality gate.

Минимум:

```bash
ruff check .
ruff format --check .
pytest
```

Если появился дополнительный standard check в repo — также запускай его.

Если test невозможно выполнить:

не скрывай.

Укажи:

```text
NOT RUN
Reason:
What was verified instead:
What remains unverified:
```

---

# 20. External integrations

Отсутствие:

```text
OpenAI key
Telegram credentials
YouTube availability
external website availability
```

не является причиной бросить весь Phase.

Используй:

```text
fake
fixture
mock boundary
local deterministic integration test
```

и заверши всё, что можно завершить.

Live verification пометь отдельно.

---

# 21. Reviewer-directed fixes take precedence

Если Orchestrator просит исправление, не спорь через реализацию другого решения без объяснения.

Если обнаружено, что requested fix технически ошибочен:

предоставь evidence.

Формат:

```text
REVIEW CONCERN

Requested change:
Observed issue:
Evidence:
Proposed alternative:
```

Жди решения только если выполнение requested change действительно создаст correctness/security problem.

---

# 22. Merge policy

По умолчанию используется:

```text
squash merge
```

Название squash commit:

```text
Phase NN: <description>
```

После merge:

```text
main
```

становится единственной базой следующего Phase.

Никогда не основывай Phase N+1 на незамёрженном Phase N.

---

# 23. After merge

После подтверждения merge:

```bash
git checkout main
git pull --ff-only
```

Убедись, что HEAD соответствует remote main.

Только затем создавай новую branch.

Старую phase branch можно удалить после подтверждённого merge.

---

# 24. Versioning

Не создавать release/tag после каждого Phase.

Phases являются development checkpoints.

Первый продуктовый tag создаётся после полной MVP acceptance:

```text
v0.1.0
```

Последующие изменения используют Semantic Versioning в разумной форме:

```text
0.x.y
```

до стабильного публичного API.

Release/tag создаётся только по указанию Orchestrator.

---

# 25. Documentation state

`docs/IMPLEMENTATION_STATE.md` должен соответствовать реальности.

Перед review Phase может быть:

```text
IN_REVIEW
```

После реализации, но до approval не ставь:

```text
DONE
```

`DONE` допустим только после решения Orchestrator либо непосредственно перед merge, если Orchestrator явно это разрешил.

Рекомендуемые состояния:

```text
NOT_STARTED
IN_PROGRESS
IN_REVIEW
DONE
BLOCKED
```

---

# 26. Decision ownership

Ты принимаешь самостоятельно:

- локальные names;
- структуры функций;
- небольшие implementation details;
- тестовую организацию;
- безопасные reversible refactors.

Orchestrator принимает:

- acceptance Phase;
- переход к следующему Phase;
- архитектурные pivots;
- крупную infrastructure;
- scope change;
- merge;
- release;
- существенную смену технологического решения.

---

# 27. Primary objective

Не пытайся впечатлить reviewer количеством кода.

Цель:

```text
small diff
+
complete behavior
+
strong tests
+
clear recovery
+
no unnecessary architecture
```

Лучший PR — тот, который легко доказать правильным.

---

# 28. Final rule

После каждого Phase:

```text
implement
→ verify
→ push
→ PR
→ REVIEW REQUEST
→ STOP
```

Следующее действие определяется Orchestrator.