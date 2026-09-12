# Review Journal — durable record of Orchestrator decisions

Канонический долговечный журнал вердиктов Orchestrator'а. Chat-беседа не является
надёжным хранилишем решений: новая сессия восстанавливает состояние оркестрации
из этого файла + GitHub, а не из истории чата. Каждая реакция Orchestrator'а
(`APPROVED` / `CHANGES REQUIRED` / `BLOCKED`) записывается сюда агентом немедленно
после получения, с привязкой к PR и reviewed HEAD SHA.

Формат записи: дата — PR — reviewed HEAD — outcome — findings — резолюции.

## 2026-09-12 — PR #1 — de516ee — CHANGES REQUIRED

- Reviewer: Orchestrator (ChatGPT, фиксированная беседа).
  GitHub PR review недоступен для коннектора: его GitHub identity — автор PR,
  а GitHub запрещает `REQUEST_CHANGES` на собственный PR. Первичный источник
  вердикта — сообщение в беседе; эта запись — durable фиксация.
- Findings:
  1. main protection: GitHub API возвращал `protected: false` при заявленной
     в документах защите (обязательное).
  2. IMPLEMENTATION_STATE: stale blocker (GitHub push) в Phase 0 после того,
     как репозиторий уже создан и PR открыт (обязательное).
  3. Handshake `APPROVED → DONE → merge` недоопределён (TOCTOU между
     approved HEAD A и финализирующим commit B) (обязательное).
  4. Вердикты Orchestrator'а не имели обязательного durable record (обязательное).
  5. PR #1 формально нарушает naming протокола — bootstrap case (MINOR).
  6. RUNBOOK хранил эфемерное состояние аутентификации (MINOR).
- Bootstrap exemption: **PR #1 is the bootstrap adoption of the orchestration
  protocol and is exempt from phase branch/title naming rules.**
- Resolved (этим же PR, коммиты после de516ee):
  1. → включён branch protection для `main` (PR-only, no force push,
     no deletions, linear history); перепроверено API.
  2. → stale blocker удалён, Last verification обновлён.
  3. → канонический flow зафиксирован в AGENTS.md §57 и RUNBOOK
     (status-finalization commit, delta docs-status-only, MERGE READY).
  4. → создан этот журнал; правило фиксации вердиктов добавлено в AGENTS.md §57.
  5. → оговорка добавлена в тело PR #1 и в эту запись.
  6. → RUNBOOK переписан на воспроизводимую процедуру `gh auth status` / `gh auth login`.

## Шаблон записи

```text
## YYYY-MM-DD — PR #N — <reviewed HEAD sha> — OUTCOME

- Findings: <список с обязательностью>
- Resolved: <пункт → решение>            # для CHANGES REQUIRED
- New HEAD: <sha>                        # после status-finalization/fixes
- Notes: <bootstrap/scope, если есть>
```
