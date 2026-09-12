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

## 2026-09-12 — PR #1 — 24a2b2f — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также зафиксирован непосредственно на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/1#pullrequestreview-5187931848 (COMMENTED, commit 24a2b2f).
- Первые шесть замечаний de516ee закрыты (protection перепроверена API: protected=true).
- Remaining MAJOR: файл протокола не был изменён при фиксе (blob SHA совпадал на
  de516ee и 24a2b2f), при этом протокол выше AGENTS.md/RUNBOOK — handshake и
  durable-record правило должны быть определены в самом протоколе.
- Уточнение handshake: finalization commit содержит ровно две мутации
  (REVIEWS.md запись + IMPLEMENTATION_STATE IN_REVIEW → DONE).
- Resolved (коммит после 24a2b2f): в протокол добавлены §9.1 (durable record) и
  §9.2 (APPROVED → merge handshake); эта запись создана.

## 2026-09-12 — PR #1 — 5611be6 — APPROVED

- Reviewer: Orchestrator; вердикт зафиксирован на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/1#pullrequestreview-5187945174 (COMMENTED, commit 5611be6).
- Reviewed HEAD: 5611be6b52fc546cd4dd060b985d24af8619ed88 (PR #1 указывает на него).
- Оставшийся MAJOR закрыт: протокол дополнен §9.1 (durable verdict record) и
  §9.2 (no-TOCTOU handshake); все предыдущие замечания de516ee и 24a2b2f закрыты.
- Дальнейшее: финализирующий commit (только REVIEWS.md + IMPLEMENTATION_STATE),
  объявление MERGE READY (Previous approved HEAD: 5611be6b…, New HEAD: B);
  Orchestrator проверяет delta 5611be6..B и выполняет squash merge с
  expected_head_sha=B. Phase 1 — только после merge и sync main.

## 2026-09-12 — PR #2 — 522b2ce — CHANGES REQUIRED

- Phase 01 — Skeleton. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/2#pullrequestreview-5188124942 (commit 522b2ce).
- Findings:
  1. MAJOR: гонка создания User — SELECT→INSERT→flush без обработки уникальности
     внутри get_or_create_user; параллельные первые сообщения нового пользователя
     могли терять Item (обязательное).
  2. MAJOR: ACK («Принял. Разбираю…») отправлялся до persistence — при ошибке БД
     пользователь получал ложное подтверждение (обязательное).
  3. MAJOR: SQLite FK не enforcement'ится — PRAGMA foreign_keys=ON не включалась
     на connection (обязательное).
  4. MINOR: атомарность claim (D-004) проверялась только последовательно — нужен
     конкурентный тест через asyncio.gather.
- Resolved (коммиты после 522b2ce в этом же PR):
  1. → get_or_create_user обрабатывает IntegrityError на flush (rollback +
     переиспользование существующего User); регрессия: параллельные первые
     сообщения → 1 User + 2 Items.
  2. → порядок persist QUEUED → ACK; регрессия: падение ingestion → ACK не отправлен.
  3. → enable_sqlite_fk (event listener `pragma foreign_keys=ON`) в make_engine и
     тестовом контексте; регрессия: Item с несуществующим user_id отвергается БД.
  4. → конкурентные тесты claim: 2 воркера × 1 Item → ровно один claim;
     2 воркера × 2 Items → разные Items.

## 2026-09-12 — PR #2 — 0ca1266 — APPROVED

- Phase 01 — Skeleton. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/2#pullrequestreview-5188149992 (commit 0ca1266).
- Reviewed HEAD: 0ca1266678b6e902dd7f0d387bd4059efd69e27c.
- Все четыре finding'а вердикта 522b2ce закрыты и покрыты регрессионными тестами;
  mergeable_state=clean. Финализация: IMPLEMENTATION_STATE Phase 1 → DONE.

## Шаблон записи

```text
## YYYY-MM-DD — PR #N — <reviewed HEAD sha> — OUTCOME

- Findings: <список с обязательностью>
- Resolved: <пункт → решение>            # для CHANGES REQUIRED
- New HEAD: <sha>                        # после status-finalization/fixes
- Notes: <bootstrap/scope, если есть>
```
