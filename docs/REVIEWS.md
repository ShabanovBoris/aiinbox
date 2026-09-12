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

## 2026-09-13 — PR #3 — d88f2b1 — CHANGES REQUIRED

- Phase 02 — Text end-to-end. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/3#pullrequestreview-5188213744 (commit d88f2b1).
- Findings:
  1. MAJOR: адаптер использует json_object (JSON mode) вместо schema-constrained
     Structured Outputs (json_schema); AnalysisResult не запрещает extra-поля —
     чужой priority_score от LLM молча отбрасывался бы (обязательное).
  2. MAJOR: processing_stage не durable — стадии присваиваются ORM-объекту, но
     commit только после READY; падение во время анализа теряло глубину прогресса,
     а requeue перезаписывал stage на REQUEUED (противоречит D-001) (обязательное).
  3. MAJOR: нет upgrade-теста существующей Phase 1 DB → head (данные должны
     сохраниться, новые колонки добавиться) (обязательное).
- Resolved (коммиты после d88f2b1 в этом же PR):
  1. → response_format=json_schema strict (strict-схема генерируется из AnalysisResult:
     required=все поля, additionalProperties=false, лишние keywords сняты);
     AnalysisResult extra="forbid"; регрессия: JSON с лишним priority_score →
     INVALID_LLM_OUTPUT.
  2. → pipeline коммитит каждую стадию до внешних вызовов; READY атомарен с
     результатом; requeue_stale сохраняет содержательную стадию (REQUEUED только
     вместо маркера PROCESSING); регрессии: другая сессия видит ANALYZING во время
     блокирующего provider-вызова, requeue сохраняет ANALYZING.
  3. → тест: upgrade 4cbfde82e2e8 → head с данными Phase 1 (User+Item) — данные
     целы, analysis-колонки добавлены.

## 2026-09-13 — PR #3 — f6f4d0a — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/3#pullrequestreview-5188228742 (commit f6f4d0a).
- Findings:
  1. MAJOR: strict-трансформер сломан — keyword-whitelist применялся и к картам
     имён (properties/$defs), вырезая все поля; тест не ловил (пустые множества).
  2. MAJOR: checkpoint durable, но не resumable — claim_next затирал стадию
     маркером PROCESSING; PRIORITIZING коммитился ДО записи analysis-полей
     (дорогой LLM-результат терялся при падении после commit).
  3. MINOR: IMPLEMENTATION_STATE отставал (JSON-mode, 39 passed).
- Finding #3 предыдущего ревью (upgrade Phase 1 DB → head) подтверждён закрытым.
- Resolved (коммиты после f6f4d0a):
  1. → properties/$defs обрабатываются как карты имён (whitelist только к
     значениям); усиленный тест: все 16 полей, непустой $defs.ItemType (7 enum),
     resolvable $ref.
  2. → claim не трогает processing_stage; pipeline: анализ-поля пишутся в том же
     commit, что ставит PRIORITIZING; resume с PRIORITIZING восстанавливает
     AnalysisResult из БД без LLM (с fallback на полный анализ при неполном
     checkpoint'е); полный сценарий ревью покрыт тестом
     test_checkpoint_resumable_llm_not_called_twice.
  3. → IMPLEMENTATION_STATE обновлён (strict outputs, актуальные счётчики).

## 2026-09-13 — PR #3 — cffa2da — APPROVED

- Phase 02 — Text end-to-end. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/3#pullrequestreview-5188249594 (commit cffa2da).
- Reviewed HEAD: cffa2da8a430258c07db60cd28d08030c4c0ca7b. mergeable/clean.
- Все findings двух кругов ревью (d88f2b1, f6f4d0a) закрыты: strict Structured
  Outputs с сохранёнными properties/$defs, resumable checkpoint без повторного
  LLM-вызова, IMPLEMENTATION_STATE синхронизирован.
- Финализация: Phase 2 → DONE в IMPLEMENTATION_STATE.

## 2026-09-13 — PR #4 — cd88b89 — CHANGES REQUIRED

- Phase 03 — Web ingestion. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/4#pullrequestreview-5188307687 (commit cd88b89).
- Findings:
  1. MAJOR/SECURITY: SSRF не привязан к фактическому соединению — валидация DNS
     и httpx-резолв раздельны (DNS rebinding/TOCTOU, OWASP); Playwright fallback
     без private-network enforcement (обязательное).
  2. MAJOR: MAX_DOWNLOAD_BYTES не был лимитом скачивания — client.get читал тело
     до проверки; _default_client игнорировал WEB_TIMEOUT_SECONDS (обязательное).
  3. MAJOR: resume из WEB_TEXT терял user_note/title/author/language
     (обязательное).
  4. MAJOR: заявленный transient retry отсутствовал — одна попытка, сразу FAILED
     (обязательное, PRODUCT_SPEC §58).
  5. MAJOR: URL normalization меняла ресурс — терялись явный порт и trailing
     slash (обязательное, ТЗ §62).
- Resolved (коммиты после cd88b89 в этом же PR):
  1. → PinningTransport: резолв+валидация+connect на один и тот же проверенный IP
     (Host/SNI оригинальные); Playwright fallback по умолчанию ВЫКЛЮЧЕН
     (config-гейт), при включении — route-deny непубличных адресов + block
     service workers.
  2. → streamed download с инкрементальным byte-cap (без Content-Length тоже);
     таймаут из конфига применяется в _default_client; тесты: oversized без
     Content-Length, кастомный timeout.
  3. → contents.metadata_json хранит title/author/language/user_note; resume
     восстанавливает эквивалентный NormalizedContent (тест на полное равенство).
  4. → retry: transient (TIMEOUT/DOWNLOAD_FAILED-transient) до 3 попыток с
     exponential backoff; permanent (SECURITY_REJECTED/4xx/TOO_LARGE) — ровно
     одна попытка; тесты на оба пути.
  5. → normalize_url: порт и trailing slash сохраняются; default-порт снимается;
     тесты на 8443/a/ и trailing slash.

## 2026-09-13 — PR #4 — 133a396 — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/4#pullrequestreview-5188369457 (commit 133a396).
- Findings #2–#5 предыдущего ревью подтверждены закрытыми. Остались три в
  transport/security fix:
  1. MAJOR: PinningTransport не делегирует aclose() внутреннему транспорту —
     connection pool/sockets остаются незакрытыми.
  2. MAJOR/SECURITY: включаемый Playwright path всё ещё без SSRF-изоляции
     (route-deny не закрывает TOCTOU/WebSockets) — нарушал бы §20 при opt-in.
  3. MAJOR: connection pool после pinning идентифицирует origin по IP — redirect
     A→B на один CDN IP мог переиспользовать TLS-сессию с SNI A.
- Resolved (коммиты после 133a396):
  1. → PinningTransport.aclose() делегирует self._inner.aclose(); тест вызывает
     cleanup на фейковом inner.
  2. → Playwright fallback жёстко отключён без production opt-in (route-deny
     не является SSRF-изоляцией); config-флаг и route-код удалены; вернётся
     отдельным изменением с настоящим network boundary. Renderer — только
     тестовый seam.
  3. → Connection: close на каждый запрос через PinningTransport — переиспользование
     соединений по IP-origin исключено; тест фиксирует заголовок.

## 2026-09-13 — PR #4 — 86d40e6 — CHANGES REQUIRED (re-review 2)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/4#pullrequestreview-5188394647 (commit 86d40e6).
- Три transport/security finding'а 133a396 подтверждены закрытыми (aclose,
  Connection: close, playwright отключён в рантайме).
- Remaining (cleanup):
  1. MINOR: playwright-зависимость не удалена из pyproject/uv.lock.
  2. MINOR: IMPLEMENTATION_STATE отстал (78 вместо 81 passed, нет описания
     финальных transport fixes).
- Resolved (коммит после 86d40e6):
  1. → playwright удалён из pyproject + uv lock пересобран (production-путь его
     не импортирует).
  2. → IMPLEMENTATION_STATE: 81 passed, финальные transport fixes зафиксированы,
     Phase 3 остаётся IN_REVIEW.

## 2026-09-13 — PR #4 — 14f4804 — APPROVED

- Phase 03 — Web ingestion. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/4#pullrequestreview-5188406402 (commit 14f4804).
- Reviewed HEAD: 14f4804e5a2618f9c3a6f41cf52bf7374c92e382. mergeable/clean.
- Все findings (SSRF pinning/Playwright/byte-cap/retry/resume-metadata/normalization)
  закрыты; playwright dependency удалена.
- Финализация: Phase 3 → DONE в IMPLEMENTATION_STATE.

## 2026-09-13 — PR #5 — 64e95d8 — CHANGES REQUIRED

- Phase 04 — Architecture checkpoint. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/5#pullrequestreview-5188456553 (commit 64e95d8).
- Findings:
  1. MAJOR: mark_failed ставил processing_stage="FAILED" — уничтожал durable
     checkpoint (persisted WEB_TEXT/analysis); retry перекачивал страницу / повторял
     LLM (нарушение D-001, PRODUCT_SPEC §59) (обязательное).
  2. MAJOR: _resolve_after_race не сходится при гонке дедупликации URL —
     конкурентные сообщения с пересекающимися URL давали исключение без ACK
     (обязательное).
  3. MINOR: IMPLEMENTATION_STATE заявлял правки тестов, которых не было на HEAD
     (assert second is not None; sanity isinstance) — источник истины обязан
     соответствовать фактическому состоянию.
- Resolved (коммиты после 64e95d8 в этом же PR):
  1. → mark_failed меняет только status/error_*; стадия сохраняется. Регрессии:
     WEB → LLM_FAILED → FAILED+ANALYZING → retry → READY при extractor.calls==1;
     retry из PRIORITIZING завершает priority без вызова LLM (counting.calls==0).
  2. → сходящаяся схема _ingest_web_urls: re-select → insert → rollback →
     re-resolve (bounded 3 раунда). Регрессия: конкурентные сообщения с
     пересекающимися URL → каждый URL ровно один Item, без исключений.
  3. → фактические правки тестов применены и проверены; IMPLEMENTATION_STATE
     синхронизирован с HEAD.
- Отдельно обновлён RUNBOOK (retry FAILED→QUEUED сохраняет стадию автоматически).

## Шаблон записи

```text
## YYYY-MM-DD — PR #N — <reviewed HEAD sha> — OUTCOME

- Findings: <список с обязательностью>
- Resolved: <пункт → решение>            # для CHANGES REQUIRED
- New HEAD: <sha>                        # после status-finalization/fixes
- Notes: <bootstrap/scope, если есть>
```
