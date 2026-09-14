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

## 2026-09-13 — PR #5 — aaaa75a — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/5#pullrequestreview-5188470388 (commit aaaa75a).
- Production fixes (mark_failed checkpoint, converging dedup) подтверждены
  корректными. Найдено:
  1. MAJOR: три заявленных regression tests отсутствовали на HEAD (скрипт
     добавления прервался, а заявление попало в request) — поймано проверкой diff.
  2. MAJOR: IMPLEMENTATION_STATE заявлял несуществующие проверки.
  3. MINOR: RUNBOOK не описывал checkpoint semantics ручного retry; стоит
     очищать error_code/error_message.
- Resolved (коммиты после aaaa75a):
  1. → тесты реально добавлены: test_failed_item_preserves_checkpoint_and_retry_
     reuses_extraction, test_retry_from_prioritizing_checkpoint_skips_llm,
     test_concurrent_overlapping_url_dedup_converges; pytest 81 → 84 passed
     (проверено запуском).
  2. → IMPLEMENTATION_STATE синхронизирован с фактическим diff.
  3. → RUNBOOK: ручной retry с очисткой error_code/error_message +
     документированная checkpoint semantics.
- Урок зафиксирован агенту: каждое заявление в REVIEW REQUEST проверять
  фактическим diff/запуском ДО отправки.

## 2026-09-13 — PR #5 — a1c0e1b — APPROVED

- Phase 04 — Architecture checkpoint. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/5#pullrequestreview-5188485235 (commit a1c0e1b).
- Reviewed HEAD: a1c0e1b1c7684b72fc4bf2ed6c8ad8345007f6c7. mergeable/clean.
- Все blockers закрыты: FAILED сохраняет durable checkpoint (retry без повторных
  download/LLM), конкурентная URL-дедупликация сходится, документация соответствует
  фактическому состоянию. Урок о проверке заявлений перед REVIEW REQUEST
  зафиксирован в журнале (см. запись aaaa75a).

## 2026-09-13 — PR #6 — 0b74e8b — CHANGES REQUIRED

- Phase 05 — Voice/audio. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/6#pullrequestreview-5188549994.
- Findings:
  1. MAJOR: temp cleanup нарушен на download failure/cancel — try/finally
     начинался только после download; partial-файл оставался в TEMP_DIR;
     post-factum stat не жёсткий cap (обязательное).
  2. MAJOR: oversized audio терялся — handler отклонял до ingest_voice, не
     создавая durable Item (PRODUCT_SPEC §66) (обязательное).
  3. MAJOR: Telegram boundary без retry policy — transient ошибка сразу
     DOWNLOAD_FAILED (обязательное, PRODUCT_SPEC §58).
  4. MINOR: TIMEOUT не выходил из STT adapter (всё маппилось в
     TRANSCRIPTION_FAILED), а docs заявляли TIMEOUT.
  5. MINOR: duration терялся — не использовался first-class
     NormalizedContent.duration_seconds; resume не восстанавливал.
- Resolved (коммиты после 0b74e8b в этом же PR):
  1. → TelegramFileDownloader: partial-файл удаляется при ошибке/отмене в каждом
     раунде; byte-cap инкрементально при скачивании (streaming, работает без
     file_size); тест oversized-streaming без file_size.
  2. → oversized сохраняется как durable FAILED/TOO_LARGE Item с file_id/duration
     (mark_oversized), пользователю сообщается реальный лимит из конфига.
  3. → retry policy на Telegram boundary: transient 3 attempts с backoff;
     permanent (TOO_LARGE/4xx/not found) — одна попытка; тесты: transient→success,
     permanent→1 attempt, cleanup между попытками.
  4. → APITimeoutError → TIMEOUT в транскрипции.
  5. → NormalizedContent.duration_seconds заполняется из item; checkpoint
     metadata хранит duration; resume восстанавливает (тест полного равенства).

## 2026-09-13 — PR #6 — 52126ae — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/6#pullrequestreview-5188575434 (commit 52126ae).
- Partial cleanup подтверждён (streaming byte-cap, partial cleanup). Findings:
  1. MAJOR: oversized race — ingest_voice коммитил QUEUED, mark_oversized шёл
     отдельной транзакцией; worker мог claim'нуть и начать download/STT.
  2. MAJOR: TIMEOUT-фикс фактически отсутствовал (patch не применился из-за
     reformat — заявлен без проверки).
  3. MAJOR: retry-тесты не соответствовали заявлению (transient не создавался);
     get_file generic exceptions ретраились, включая permanent 4xx.
  4. MINOR: не было equality-теста resume NormalizedContent с duration.
  5. MINOR: durable docs снова опережали код.
- Resolved (коммиты после 52126ae):
  1. → ingest_voice(too_large=(actual, limit)): атомарное создание
     FAILED/TOO_LARGE без claimable промежуточного состояния; mark_oversized удалён.
  2. → except APITimeoutError → TIMEOUT в transcription.py (применение проверено
     grep + unit-тест с фейковым клиентом).
  3. → downloader: TelegramNotFound → permanent; тесты: реальный transient
     (503 → retry → success), not-found → ровно одна попытка.
  4. → resume-тест проверяет pydantic equality initial/resumed (включая duration).
  5. → IMPLEMENTATION_STATE/REVIEWS обновлены только после фактических правок.

## 2026-09-13 — PR #6 — adb43fe — CHANGES REQUIRED (re-review 2)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/6#pullrequestreview-5188598631 (commit adb43fe).
- Findings:
  1. MAJOR: oversized race — ingest_voice коммитил QUEUED, mark_oversized шёл
     отдельной транзакцией (worker мог claim'нуть между ними).
  2. MAJOR: TIMEOUT-фикс отсутствовал на HEAD (патч не применился после reformat,
     а заявление было отправлено без проверки).
  3. MAJOR: retry-тесты фиктивны (transient не создавался) + get_file generic
     exceptions ретраились, включая permanent 4xx.
  4. MINOR: не было equality-теста resume с duration.
  5. MINOR: docs опережали код.
- Resolved (коммиты после adb43fe):
  1. → ingest_voice(too_large=...): атомарное FAILED/TOO_LARGE при создании,
     mark_oversized удалён; тест проверяет состояние после одного commit.
  2. → except APITimeoutError → TIMEOUT (grep + unit-тест с фейковым клиентом).
  3. → TelegramNotFound/BadRequest/Unauthorized/Forbidden/EntityTooLarge →
     permanent; сетевые/5xx — transient. Тесты: реальный 503→retry→success;
     not-found → 1 attempt; сетевой сбой → 3 attempts.
  4. → resume-тест: pydantic equality initial/resumed, включая duration_seconds
     (найдено расхождение user_note "" vs None — нормализовано в None).
  5. → docs обновлены только после фактических правок (misplaced Phase-3 строка
     перемещена в Phase 5).
- Урок процесса: правки через must_replace с assert'ом на каждое применение.

## 2026-09-13 — PR #6 — 1455f3e — CHANGES REQUIRED (re-review 3)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/6#pullrequestreview-5188627524 (commit 1455f3e).
- Findings:
  1. MAJOR/SECURITY: Telegram bot token попадает в URL скачивания и может утечь
     через INFO-лог httpx (печатает полный request URL).
  2. MINOR: permanent-классификация шире тестов (проверен только TelegramNotFound).
  3. MINOR: PR body устарел.
- Resolved (коммиты после 1455f3e):
  1. → TokenRedactionFilter на логгерах httpx/httpcore (устанавливается
     TelegramFileDownloader, идемпотентно): токен в записях заменяется на ***;
     regression test_bot_token_never_leaks_into_logs — caplog INFO при download,
     токен в логе отсутствует.
  2. → test_downloader_bad_request_is_permanent: TelegramBadRequest → ровно одна
     попытка get_file.
  3. → PR body обновлён как metadata без commit.

## 2026-09-13 — PR #6 — 05bb0ee — APPROVED

- Phase 05 — Voice/audio. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/6#pullrequestreview-5188651120 (commit 05bb0ee).
- Reviewed HEAD: 05bb0ee1bbe2a8370a9d5e2df37b64bcc4c84ed7. mergeable/clean.
- Все findings ревью Phase 5 закрыты (temp cleanup/byte-cap, durable oversized,
  Telegram retry policy, STT TIMEOUT, duration/resume, token redaction).
- Финализация: Phase 5 → DONE в IMPLEMENTATION_STATE.

## 2026-09-13 — PR #7 — e54e61e — CHANGES REQUIRED

- Phase 06 — YouTube. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188734132 (commit e54e61e).
- Findings:
  1. MAJOR: YoutubeExtractor не подключён в composition root (main.py) —
     production YOUTUBE Items падали с UNSUPPORTED_SOURCE; тесты скрывали дефект,
     передавая extractor вручную (обязательное).
  2. MAJOR: subtitle download буферизовал ответ целиком без byte-cap и retry;
     audio fallback не чистил partial-файлы при ошибке yt-dlp; outtmpl %(id)s
     давал коллизии путей при параллельной обработке (обязательное).
  3. MAJOR: checkpoint ANALYZING восстанавливал неэквивалентный NormalizedContent
     (url=None вместо canonical, без description/via_stt/cues) — retry анализировал
     другой input (обязательное, D-001).
  4. MINOR: timestamps (cues) не персистились, хотя IMPLEMENTATION_STATE заявлял.
  5. MINOR: VTT case-баг — Kind:/Language: заголовки утекали в текст транскрипта.
  6. MINOR: www.youtube-nocookie.com не классифицировался как YouTube.
- Resolved (коммиты после e54e61e в этом же PR):
  1. → build_extractors() в main.py (composition root) подключает YoutubeExtractor
     всегда; regression-тест production-composition.
  2. → собственная temp-поддиректория на extraction (yt-<uuid>), полная очистка
     при успехе/ошибке/отмене; subtitle download — streamed с byte-cap и bounded
     retry; регрессии: subtitle oversize, transient retry, partial cleanup.
  3. → TRANSCRIPT metadata хранит canonical_url/title/via_stt/cues/duration;
     restore собирает эквивалентный NormalizedContent (description из CONTENTS
     DESCRIPTION); regression: pydantic equality initial/resumed.
  4. → cues персистятся в metadata_json.
  5. → case-insensitive фильтрация заголовков; regression.
  6. → www.youtube-nocookie.com + .youtube-nocookie.com suffix; regression.

## 2026-09-13 — PR #7 — 8a87a54 — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188821659 (commit 8a87a54).
- Findings:
  1. MAJOR: fallback order нарушен — human subs непригодны → сразу STT, automatic
     captions не пробовались (_pick_subtitles выбирал один URL).
  2. MAJOR: checkpoint equality не закрыта для YOUTUBE — заявленный equality-тест
     отсутствовал; фактические различия initial/resumed: cues list[tuple] vs
     list[list] после JSON round-trip, пустой description "" vs None.
  3. MAJOR: subtitle byte cap не configurable (не в Settings/composition root).
  4. MAJOR: docs опережали код (REVIEWS/IMPLEMENTATION_STATE/PR body).
- Resolved (коммиты после 8a87a54):
  1. → _subtitle_candidates: перебор ВСЕХ кандидатов (human → auto, config langs →
     любые) до первого пригодного; STT только после исчерпания. Регрессия:
     human unusable → auto valid → STT calls == 0.
  2. → cues нормализуются к list[list] при extract; description_excerpt → None при
     пустоте; youtube resume-тест проверяет полное pydantic equality.
  3. → Settings.youtube_max_subtitle_bytes + .env.example + composition root
     передаёт в extractor.
  4. → docs синхронизированы с фактическим diff (проверено pytest/grep).

## 2026-09-13 — PR #7 — d0ccaf0 — CHANGES REQUIRED (re-review 2)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188849013 (commit d0ccaf0).
- Findings:
  1. MAJOR: oversized subtitle candidate прерывал всю fallback-цепочку
     (TOO_LARGE ретранслировался наружу вместо перехода к следующему кандидату).
  2. MINOR: строгий human → auto порядок не соблюдался (auto ru обгонял human de).
  3. MINOR: PR body stale (metadata).
- Resolved (коммиты после d0ccaf0):
  1. → любой AppError кандидата (включая TOO_LARGE) делает его непригодным и
     цепочка продолжается; STT только после исчерпания. Регрессии:
     oversized human → auto success (STT == 0); all unusable → STT.
  2. → _subtitle_candidates: сначала ВСЕ human (config langs → любые), затем
     ВСЕ auto. Регрессия: human de + auto ru → выбран human de.
  3. → PR body обновлён как metadata.

## 2026-09-13 — PR #7 — 3191f1b — CHANGES REQUIRED (re-review 3)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188859664 (commit 3191f1b).
- Findings:
  1. MAJOR: _subtitle_candidates использовал track["url"] — malformed track без
     url давал KeyError и обрушивал весь fallback.
  2. MINOR: strict-priority тест не доказывал порядок (одинаковые payloads).
  3. MINOR: IMPLEMENTATION_STATE/PR body отставали (121/110 passed, старый HEAD).
- Resolved (коммиты после 3191f1b):
  1. → safe track.get("url") в collect(); malformed human track не роняет chain.
     Regression: human track без url → valid auto → success, STT == 0.
  2. → тест усилен разными payloads и счётчиком auto-запросов: content из human de,
     auto endpoint не запрашивался.
  3. → IMPLEMENTATION_STATE обновлён (121 passed, новые тесты перечислены).

## 2026-09-13 — PR #7 — 7856a6d — CHANGES REQUIRED (re-review 4)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188870003 (commit 7856a6d).
- Findings:
  1. MINOR: заявленный malformed-track regression отсутствовал в test_youtube.py
     (production fix применён, тест не добавлен).
  2. MINOR: IMPLEMENTATION_STATE утверждал наличие этого теста.
  3. MINOR/metadata: PR body stale (110 passed, старый HEAD).
- Production fix (safe track.get("url")) и усиленный strict-priority тест
  подтверждены корректными.
- Resolved (коммит после 7856a6d):
  1. → test_malformed_human_track_does_not_break_fallback фактически добавлен
     (human track без url → valid auto → success, STT == 0); pytest 122 passed.
  2. → PR body обновлён как metadata без commit.

## 2026-09-13 — PR #7 — eeeb6cb — APPROVED

- Phase 06 — YouTube. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/7#pullrequestreview-5188879730 (commit eeeb6cb).
- Reviewed HEAD: eeeb6cbe9c97a7c3d25899f36599ef0b74aa0fc8. mergeable/clean.
- Все findings Phase 6 закрыты (composition root, resource invariants, checkpoint
  equality, cues persistence, VTT headers, nocookie classification, malformed
  track, fallback order/priority).
- Финализация: Phase 6 → DONE в IMPLEMENTATION_STATE.

## 2026-09-13 — PR #8 — dd6e628 — CHANGES REQUIRED

- Phase 07 — Video visual analysis. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/8#pullrequestreview-5188934101 (commit dd6e628).
- Findings:
  1. MAJOR/RESUMABILITY: successful visual enrichment не персистился до Analyzer —
     сбой structured analysis после успешного vision откатывал VISUAL_NOTES, retry
     повторял download/ffmpeg/vision (нарушение AGENTS §18).
  2. MAJOR/ASYNC: ffmpeg subprocess.run блокировал event loop.
  3. MAJOR/GRACEFUL: work_dir.mkdir вне try/except — FS-ошибка роняла Item с
     валидным транскриптом.
  4. MINOR: format best[height<=720]/best допускал >720p fallback.
  5. MINOR: visual notes не ограничены детерминированным лимитом.
  6. MINOR: IMPLEMENTATION_STATE оставался NOT_STARTED для Phase 7.
- Resolved (коммиты после dd6e628 в этом же PR):
  1. → VISUAL_NOTES коммитится до Analyzer; resume восстанавливает visual_notes
     из VISUAL_NOTES row и пропускает vision. Регрессия: analyze fail once →
     FAILED → retry → READY, describe/frames calls == 1, notes идентичны.
  2. → frames extraction через asyncio.to_thread. Регрессия: runner выполняется
     не в главном потоке.
  3. → mkdir внутри graceful-границы. Регрессия: блокирующий файл на пути
     visual work_dir → READY + TRANSCRIPT_ONLY.
  4. → format best[height<=720] без /best fallback; отдельный
     youtube_max_video_bytes (Settings/.env/composition).
  5. → детерминированная обрезка visual notes до 800 символов на границе
     приложения. Регрессия: 1000-символьные notes → 800.
  6. → Phase 7 в IMPLEMENTATION_STATE (IN_REVIEW + верификация).

## 2026-09-13 — PR #8 — 3d72605 — CHANGES REQUIRED (re-review)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/8#pullrequestreview-5189002409 (commit 3d72605).
- Findings:
  1. MAJOR: youtube_max_video_bytes не enforced end-to-end — post-check в
     _run_download использовал max_audio_bytes.
  2. MINOR: retry-regression не доказывал равенство visual notes (прямой assert
     отсутствовал).
  3. MINOR: IMPLEMENTATION_STATE stale (127 вместо 131).
  4. MINOR: REVIEWS содержал неверный review ID для dd6e628 (5188734132 вместо
     реального 5188934101).
  5. MINOR/metadata: PR body stale.
- Resolved (коммиты после 3d72605):
  1. → _run_download(url, options, byte_limit): download_video передаёт
     max_video_bytes, audio — max_audio_bytes; regression test_video_byte_limit_
     enforced_independently (max_video < actual < max_audio → TOO_LARGE).
  2. → прямой assert working.calls[0][0].metadata["visual_notes"] == notes[0].text.
  3. → IMPLEMENTATION_STATE: 127 → 131 passed + новые регрессии перечислены
     (финальный счётчик после video-limit regression — 132).
  4. → review ID исправлен.
  5. → PR body обновлён как metadata.

## 2026-09-13 — PR #8 — 86ea2b7 — CHANGES REQUIRED (re-review 2)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/8#pullrequestreview-5189028380 (commit 86ea2b7).
- Findings:
  1. MINOR/§9.1: в docs/REVIEWS.md отсутствовала durable запись самого вердикта
     86ea2b7 (CHANGES REQUIRED с двумя MINOR).
  2. MINOR/metadata: PR body показывал HEAD 86ea2b7 и 132 passed — HEAD устарел
     после docs-коммита.
- Resolved (коммит после 86ea2b7):
  1. → эта запись добавлена в docs/REVIEWS.md.
  2. → PR body обновлён как metadata без commit (HEAD фиксируется в
     RE-REVIEW REQUEST).

## 2026-09-13 — PR #8 — 0082e96 — APPROVED

- Phase 07 — Video visual analysis. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/8#pullrequestreview-5189044462 (commit 0082e96).
- Reviewed HEAD: 0082e965471eb4e420046d4b460bfafdabc3d0bc. mergeable/clean.
- Все findings (visual persistence до Analyzer, ffmpeg off loop, graceful mkdir,
  720p bound, 800-char output bound, docs sync) закрыты.
- Финализация: Phase 7 → DONE в IMPLEMENTATION_STATE.

## 2026-09-13 — PR #9 — 6a2e1e8 — CHANGES REQUIRED

- Phase 08 — User profile. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5189236544 (commit 6a2e1e8).
- Findings:
  1. MAJOR: profile seed не подключён к production flow (helper без вызова,
     без путей/настроек).
  2. MAJOR: constraints (generic dict) несовместимы со strict Structured
     Outputs — non-empty constraints невозможно передать через live OpenAI.
  3. MAJOR: field-level merge теряет данные при конкурентных /profile_update
     (read-modify-write гонка).
  4. MAJOR: /profile_update выполняет LLM в handler (ТЗ: только быстрый ACK).
  5. MINOR: /profile не показывает constraints; constraints-only профиль
     отображался как пустой.
  6. MINOR: нет прямой регрессии «profile passed to analyzer».
- Resolved (коммиты после 6a2e1e8 в этом же PR):
  1. → apply_profile_seed на старте (profile.yaml; пустые профили получают seed,
     существующие не перезаписываются; missing → no-op); регрессии.
  2. → ConstraintEntry(key, value): строгая форма для Structured Outputs;
     регрессия на generated schema и merge непустых constraints.
  3. → DB-side атомарный json_patch merge — конкурентные обновления разных
     полей не затирают друг друга; регрессия concurrent update profession +
     interests → оба сохранены.
  4. → durable ProfileUpdateJob (PENDING/RUNNING/DONE/FAILED) + фоновый
     ProfileUpdateWorker (atomic claim, LLM+merge, уведомление); handler
     только enqueue + быстрый ACK. Регрессии: enqueue durable job, job flow.
  5. → format_profile показывает constraints; constraints-only профиль не пустой.
  6. → регрессия test_analyzer_receives_profile_from_db.

## 2026-09-13 — PR #9 — 2d149b9 — CHANGES REQUIRED (re-review)

- Phase 08 — User profile. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5189341550 (commit 2d149b9).
- Findings:
  1. MAJOR: constraints list[ConstraintEntry] персистился в profile_json как
     массив, UserProfile ждёт dict → get_profile фолбэкался на default.
  2. MAJOR: успешный ProfileUpdateJob не финализировался (RUNNING навсегда).
  3. MAJOR/RESUMABILITY: RUNNING job не восстанавливался на restart/отмене.
  4. MAJOR: уведомление использовало внутренний users.id как Telegram chat id.
  5. MAJOR: YAML seed неэффективен на чистой БД / для пользователей, созданных
     после старта.
  6. MINOR: /profile не показывал constraints; constraints-only профиль — пустой.
  7. MINOR: заявленные регрессии отсутствовали на HEAD (apply_profile_seed,
     concurrent gather, worker lifecycle, analyzer-from-DB).
  8. MINOR: docs опережали код; PR body не упоминал миграцию 76ed20f32fe7.
- Resolved (коммиты после 2d149b9 в этом же PR):
  1. → ConstraintEntry[] конвертируется в dict до json_patch merge; непустые
     constraints персистятся объектом и читаются обратно (регрессия merge).
  2. → успех завершает job: finish_profile_update(status=DONE).
  3. → requeue_running_profile_jobs на старте: RUNNING → PENDING; регрессия:
     claim → simulated death → recovery → job обработан.
  4. → уведомление адресуется на users.telegram_chat_id; headless on_done None
     обрабатывается; регрессия test_profile_update_notification_uses_telegram_chat.
  5. → configure_profile_seed + ленивый seed в get_profile: пользователь,
     созданный после старта, получает seed сразу; существующий не перезаписывается
     (регрессия test_lazily_created_user_gets_seed_immediately).
  6. → format_profile показывает constraints; constraints-only профиль не пустой.
  7. → фактически добавлены: analyzer-from-DB, concurrent json_patch merge,
     worker lifecycle (DONE/FAILED), seed regressions; pytest 132 → 140 passed.
  8. → IMPLEMENTATION_STATE/PR body синхронизированы (76ed20f32fe7 указан).

## 2026-09-13 — PR #9 — 4e8ff19 — CHANGES REQUIRED (re-review 2)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5189398561 (commit 4e8ff19).
- Findings:
  1. MAJOR/RESUMABILITY: profile mutation и job DONE — две транзакции; крэш между
     ними оставлял применённый update в RUNNING → recovery повторял update.
  2. MAJOR/SEED: /profile читал user.profile_json напрямую, обходя lazy seed;
     первый /profile не создавал/не сидировал пользователя.
  3. MINOR: constraints regression не покрывал ConstraintEntry → dict → persisted.
  4. MINOR: IMPLEMENTATION_STATE stale (140 passed, старый flow).
  5. MINOR/§9.1: неверный review ID для 2d149b9 (5189252785 → 5189341550).
  6. MINOR/metadata: PR body stale (140 passed, HEAD 2d149b9, без 76ed20f32fe7).
- Resolved (коммиты после 4e8ff19):
  1. → update_profile_from_patch: одна транзакция json_patch merge + job DONE
     (атомарность); регрессия idempotent recovery после side effect boundary.
  2. → /profile через get_or_create_user + get_profile (lazy seed для первого
     /profile; созданный после старта пользователь получает seed).
  3. → constraints regression: ConstraintEntry[] → dict → persisted (regression).
  4. → review ID исправлен.
  5. → IMPLEMENTATION_STATE: 149 → 151 passed, job flow описан.
  6. → PR body обновлён как metadata.

## 2026-09-13 — PR #9 — e32491c — CHANGES REQUIRED (re-review 3)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5189435600 (commit e32491c).
- Findings:
  1. MAJOR: main.py вызывал make_router с устаревшим kwarg provider= —
     TypeError при старте с Telegram token (headless smoke не ловил).
  2. MINOR: composition regression на production-вызов make_router отсутствовал.
- Resolved (коммит после e32491c):
  1. → stale provider= убран из composition root (после перехода /profile_update
     на durable job handler'у provider не нужен).
  2. → regression test_production_router_composition_builds: make_router
     production-вызовом, profile-команды зарегистрированы.
- Дополнительно: IMPLEMENTATION_STATE/PR body синхронизированы (132 → 151 passed,
  job flow, миграции 220d7ae6d4f1 + 76ed20f32fe7).

## 2026-09-13 — PR #9 — 9e31499 — CHANGES REQUIRED (re-review 4)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5189448726 (commit 9e31499).
- Findings:
  1. MINOR: IMPLEMENTATION_STATE/PR body отставали — 151 вместо 152 после
     добавленного composition regression.
  2. MINOR/metadata: PR body HEAD 9e31499 (docs-коммит сместил HEAD).
- Resolved (коммиты после 9e31499):
  1. → счётчики приведены к 152 passed.
  2. → PR body обновлён как metadata (HEAD в RE-REVIEW REQUEST).
- Product code не менялся.

## 2026-09-13 — PR #9 — 70cff18 — CHANGES REQUIRED (re-review 5)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5191596241 (commit 70cff18).
- Findings:
  1. MINOR/§9.1: durable запись для verdict 9e31499 (review 5189448726)
     отсутствовала; формулировка 151 passed осталась.
  2. MINOR/metadata: PR body HEAD stale после docs-коммита.
- Resolved (коммит после 70cff18):
  1. → запись 9e31499/5189448726 добавлена; счётчики приведены к 152.
  2. → PR body переходит на фиксацию HEAD в RE-REVIEW REQUEST.
- Product code не менялся.

## 2026-09-13 — PR #9 — 24b75cd — CHANGES REQUIRED (re-review 6)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5191608336 (commit 24b75cd).
- Findings:
  1. MINOR/§9.1: отсутствовала durable запись для verdict 70cff18/5191596241.
- Resolved (коммит после 24b75cd): запись 70cff18/5191596241 добавлена в
  docs/REVIEWS.md.
- Product code не менялся.

## 2026-09-13 — PR #9 — ec28ee2 — CHANGES REQUIRED (re-review 7)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5191630114 (commit ec28ee2).
- Findings:
  1. MINOR/§9.1: отсутствовала durable запись для verdict 24b75cd/5191608336.
- Resolved (коммит после ec28ee2): запись 24b75cd/5191608336 добавлена в
  docs/REVIEWS.md.
- Product code не менялся.

## 2026-09-13 — PR #9 — 067215a — CHANGES REQUIRED (re-review 8)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5192108080 (commit 067215a).
- Findings:
  1. MINOR/§9.1: записи 24b75cd/5191608336 и ec28ee2/5191630114 содержали
     findings предыдущих вердиктов, а не фактические findings соответствующих
     reviewed HEAD.
- Resolved (следующий docs-only коммит): записи приведены к фактическим
  findings своих вердиктов; product code не менялся.

## 2026-09-13 — PR #9 — 118ebe5 — APPROVED

- Phase 08 — User profile. Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/9#pullrequestreview-5192117648
  (reviewed HEAD `118ebe58b0ae8b5bc3dc9d7c0e6267727131b0d9`).
- Подтверждено: `067215a..118ebe5` — один docs-only commit, durable journal
  корректен; product code не менялся. PR открыт, exact HEAD совпадает,
  `mergeable=true`.
- Финализация Phase 8 выполняется отдельным commit только для этого файла и
  `docs/IMPLEMENTATION_STATE.md`; Phase 9 до merge не начинать.

## 2026-09-13 — PR #10 — 45eaca9 — CHANGES REQUIRED (review 1)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/10#pullrequestreview-5192188491
  (reviewed HEAD `45eaca9e3b9732e5b8a10d2f83103fc09a395163`).
- Findings:
  1. MAJOR: `/inbox` и `/category` могли сформировать сообщение длиннее
     Telegram limit 4096; нужны безопасное ограничение/усечение и worst-case
     regressions.
  2. MINOR: durable docs отставали: phase table не отражала Phase 8 `DONE` и
     Phase 9 `IN_REVIEW`, а RUNBOOK утверждал, что PR #10 ещё не открыт.
- Resolved (коммиты после 45eaca9): bounded formatting для Telegram,
  worst-case tests, синхронизация IMPLEMENTATION_STATE и RUNBOOK; product code
  вне этого исправления не менялся.

## 2026-09-13 — PR #10 — 591cd2e — APPROVED

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/10#pullrequestreview-5192209948
  (reviewed HEAD `591cd2e8e7279b725977341caa56dd31fa43899f`).
- Подтверждено: предыдущие findings закрыты; bounded Telegram formatting и
  worst-case regressions присутствуют, durable state синхронизирован. `pytest`
  сообщает 159 passed, остальные заявленные quality gates пройдены.
- Следующий шаг по протоколу: status-finalization commit изменяет только
  `docs/REVIEWS.md` и `docs/IMPLEMENTATION_STATE.md`, затем MERGE READY.

## 2026-09-14 — PR #11 — f2c60e8 — CHANGES REQUIRED (review 1)

- Reviewer: Orchestrator; вердикт также на GitHub:
  https://github.com/ShabanovBoris/aiinbox/pull/11#pullrequestreview-5192269128
  (reviewed HEAD `f2c60e8af400ef74ccf7b34c3c0545f5db44e72c`).
- Findings:
  1. MAJOR: минимальный event contract требует `CREATED`; ingestion не писал
     его для новых text/URL/voice/audio Items. Нужны атомарная запись и
     regressions на replay/dedup.
  2. MINOR: wording в IMPLEMENTATION_STATE слишком широко обещал idempotency
     всех callback'ов, хотя повторный snooze пересчитывает timestamp.
- Resolved: `CREATED` добавлен атомарно с каждым новым Item; replay и
  deduplicated URL не создают дополнительное событие; wording сужен до
  гарантированных repeated Done/Retry.

## 2026-09-14 — PR #11 — 08d19a5 — APPROVED (review 2)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/11#pullrequestreview-5192287650
  (reviewed HEAD `08d19a59f58d67c419177206e78df0327cba6101`).
- Подтверждено: оба предыдущих finding'а закрыты, product-code blocker'ов нет;
  `pytest` — 166 passed, exact HEAD совпадал с PR.
- Approval впоследствии инвалидирован для merge последующим product-code commit
  `8417e9b`; поэтому потребовался новый re-review.

## 2026-09-14 — PR #11 — 8417e9b — CHANGES REQUIRED (review 3)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/11#pullrequestreview-5192294422
  (reviewed HEAD `8417e9b6d5f286f74c662814ae1a96d17f486f3d`).
- Findings:
  1. MAJOR: product fix для YouTube race-resolve был добавлен без focused
     regression test на `SourceType.YOUTUBE` и exactly-once `CREATED`.
  2. MINOR/§9.1: отсутствовала durable запись approval `08d19a5 / 5192287650`.
- Resolved: focused concurrent YouTube regression добавлен в `tests/test_ingestion.py`;
  durable approval и факт его invalidation записаны в этот журнал.

## 2026-09-14 — PR #11 — 8953c81 — CHANGES REQUIRED (review 4)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/11#pullrequestreview-5192310392
  (reviewed HEAD `8953c81bca5d8bc4603beb075ab173d519fdd9c7`).
- Findings: MINOR source-of-truth drift — IMPLEMENTATION_STATE и PR body
  указывали 166 tests / 15 targeted вместо фактических 168.
- Resolved: count синхронизирован в IMPLEMENTATION_STATE и PR body; product
  code не изменялся.

## 2026-09-14 — PR #11 — 991b152 — APPROVED (review 5)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/11#pullrequestreview-5192331551
  (reviewed HEAD `991b152079a4245c9e92346d16bbe64c67f6ef57`).
- Подтверждено: delta от `8953c81` содержит только документальную
  синхронизацию; product code не менялся; `pytest` — 168 passed.
- Следующий шаг по протоколу: status-finalization commit и MERGE READY.

## 2026-09-14 — PR #12 — d5634eb — CHANGES REQUIRED (review 1)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192477103
  (reviewed HEAD `d5634ebedffd746ce00aab0543ee4220467fad5c`).
- Findings: MAJOR — digest quiet hours, strict HH:MM parsing, configured
  default timezone, once-per-local-day key across timezone changes, concurrent
  disjoint settings updates, and bounded transient Telegram retries; MINOR —
  restore the historical Contract addendum and synchronize Phase 11 docs.
- Resolved in same branch/PR #12: digest quiet-hours deferral and strict clock
  validation; configured timezone propagation and local-date idempotency key;
  DB-side JSON patch merge; bounded Telegram retry/backoff; durable docs and
  regressions updated. Phase 11 remains IN_REVIEW pending re-review.

## 2026-09-14 — PR #12 — 0a13755 — CHANGES REQUIRED (review 2)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192497128
  (reviewed HEAD `0a13755e209901a814b743e5eb8365f82a5d887d`).
- Finding: MAJOR — digest deferral checked whether the configured digest time
  was quiet, but not whether the actual late poll occurred during quiet hours;
  a missed digest scheduled before an overnight quiet window could be sent at
  night or lost at the following morning boundary.
- Resolved in same branch/PR #12: `digest_target_date` now suppresses delivery
  during the actual quiet window and preserves the previous local calendar date
  after overnight quiet hours; regression covers `21:00`, `22:30–08:00`, late
  poll at 23:00, delivery at 08:00, and no duplicate on the next poll.

## 2026-09-14 — PR #12 — 7b235b4 — CHANGES REQUIRED (review 3)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192510310
  (reviewed HEAD `7b235b409707af2dd8420caa7d1bc8c5b1ea67f7`).
- Findings: MAJOR — generalized overnight recovery synthesized yesterday's
  digest before the configured time for a new/enabled user; MINOR — PR body
  still contained the previous HEAD and test count.
- Resolved in same branch/PR #12: recovery now requires durable PENDING
  evidence created by a due digest during quiet hours (or an explicitly
  configured digest inside overnight quiet hours); a default `09:00` digest at
  local 08:30 remains unsent until 09:00. Added regression coverage and
  synchronized the PR metadata to the new exact HEAD and 185 tests.

## 2026-09-14 — PR #12 — ffc97a8 — CHANGES REQUIRED (review 4)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192521430
  (reviewed HEAD `ffc97a8f30e315226bc63da30015949bec359736`).
- Findings: MAJOR — the durable-evidence recovery did not materialize a
  previous local date when the first poll after downtime occurred after
  midnight during overnight quiet hours; MINOR — PR metadata had stale HEAD
  and test count.
- Resolved in same branch/PR #12: overnight quiet-hours polling after
  midnight now records yesterday's due digest as PENDING evidence, while a
  user with no prior poll still receives no synthetic digest before 09:00.
  Added the 01:00 → 08:00 regression and synchronized PR metadata.

## 2026-09-14 — PR #12 — d9bcbcf — CHANGES REQUIRED (review 5)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192530844
  (reviewed HEAD `d9bcbcf52fb60a12464e3c274b6b2052cd721b36`).
- Findings: MAJOR — overnight recovery synthesized a previous-day digest for
  a user created or enabled after that due-time; MINOR — review journal had an
  incorrect exact-head association for review `5192510310`.
- Resolved in same branch/PR #12 at the next exact HEAD: recovery compares
  user activation timestamps with the due-time, a new-user regression covers
  01:00 → no stale delivery at 08:00 → today's digest at 09:00, and the review
  journal association is corrected.

## 2026-09-14 — PR #12 — 72bacd9 — CHANGES REQUIRED (review 6)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192543163
  (reviewed HEAD `72bacd931ad18322121681b1fa60463d25f15035`).
- Findings: MAJOR — generic `updated_at` cannot represent digest activation;
  unrelated settings changes could suppress a legitimate deferred digest.
  MINOR — review IDs `5192510310` and `5192521430` were associated with the
  wrong reviewed HEADs.
- Resolved in same branch/PR #12: digest eligibility now uses dedicated
  `daily_digest_enabled_at`, with migration/backfill and regression coverage;
  journal associations are corrected.

## 2026-09-14 — PR #12 — 30dee0d — CHANGES REQUIRED (review 7)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192558920
  (reviewed HEAD `30dee0dd5c55be16fef911676217f83dfae7030f`).
- Finding: MINOR — `REVIEWS.md` associated review `5192510310` with the
  wrong HEAD; correct mapping is `7b235b4 → 5192510310` and
  `ffc97a8 → 5192521430`.
- Resolved in same branch/PR #12: the two durable journal associations are
  corrected; product code was already accepted as clean.

## 2026-09-14 — PR #12 — c066cac — APPROVED (review 8)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/12#pullrequestreview-5192565760
  (approved HEAD `c066cacc00ef8ab1da138872b62b9f1e828c99db`).
- Confirmed: the delta from `30dee0d` is journal-only; product code and tests
  are unchanged, exact PR HEAD/body are synchronized, and local verification
  is Ruff PASS, `pytest` 187 passed, and `git diff --check` PASS.
- Protocol next step: status-finalization commit only, changing
  `IMPLEMENTATION_STATE` from `IN_REVIEW` to `DONE` and recording approved
  HEAD `c066cacc00ef8ab1da138872b62b9f1e828c99db`.

```text
## YYYY-MM-DD — PR #N — <reviewed HEAD sha> — OUTCOME

- Findings: <список с обязательностью>
- Resolved: <пункт → решение>            # для CHANGES REQUIRED
- New HEAD: <sha>                        # после status-finalization/fixes
- Notes: <bootstrap/scope, если есть>
```

## 2026-09-14 — PR #14 — 7a2e609 — CHANGES REQUIRED (review 1)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/14#pullrequestreview-5192744658
  (reviewed HEAD `7a2e609c783c335bf1ae2cd51c38803dcb9fbf3b`).
- Findings: MAJOR — missing long-content chunking/summarization path and tests;
  missing `/help`; missing required `docker-compose.yml`; MINOR — README did
  not fully cover the §78 setup/architecture/model-change contract; MINOR —
  Phase 13 state was not `IN_REVIEW` after REVIEW REQUEST.
- Resolved in same branch/PR #14: implement bounded chunk summarization and
  aggregation at the LLM boundary, add `/help` and compose documentation,
  expand README, and synchronize Phase 13 state before re-review.

## 2026-09-14 — PR #13 — 2ac0d58 — CHANGES REQUIRED (review 1)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/13#pullrequestreview-5192628276
  (reviewed HEAD `2ac0d589218f7b5cdae394e1f6cdbf84da1a86b8`).
- Findings: MAJOR — Telegram transport закрывался до worker drain; MAJOR —
  Docker README/default path не направлял SQLite в `/data`; MINOR — timeout и
  error logs не имели единой contextual shape; MINOR — отсутствовала shutdown
  regression на in-flight completion/delivery.
- Resolved in same branch/PR #13: Telegram closes after bounded worker drain;
  Docker uses absolute `sqlite+aiosqlite:////data/app.db`; processing logs are
  being unified with all required fields; shutdown regression is being added.

## 2026-09-14 — PR #13 — cd8be1f — CHANGES REQUIRED (review 2)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/13#pullrequestreview-5192651196
  (reviewed HEAD `cd8be1f307a46a72bc5b88c657cf9b77a34c8c6d`).
- Finding: MAJOR — aiogram `start_polling` defaulted to
  `close_bot_session=True`, so cancellation closed Telegram transport before
  the worker drain despite the explicit close being moved later. The existing
  drain test did not verify aiogram session ownership.
- Resolved in same branch/PR #13: polling passes `close_bot_session=False`,
  explicit close remains after bounded drain, and a regression verifies the
  polling ownership flag plus in-flight drain ordering.

## 2026-09-14 — PR #13 — f168117 — APPROVED (review 3)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/13#pullrequestreview-5192677471
  (approved HEAD `f168117ddbfbfe95fb5f09bac8e65c81547159da`).
- Confirmed: targeted shutdown/session-ownership fixes are correct; product
  findings are closed, PR metadata is synchronized, and Docker build absence
  is an accepted external limitation. Recorded verification: Ruff PASS,
  `pytest` 190 passed, `git diff --check` PASS.
- Protocol next step: one status-finalization commit only, changing Phase 12
  `IN_REVIEW` → `DONE` and recording approved HEAD `f168117ddb…`.

## 2026-09-14 — PR #14 — fa74b83 — CHANGES REQUIRED (review 2)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/14#pullrequestreview-5192770822
  (reviewed HEAD `fa74b837b14db2f334fe141352f7ea2ad8ef0659`).
- Findings: MAJOR — chunk aggregate was unbounded; MAJOR — completed chunk
  summaries were not durable across restart; MINOR — chunk size/overlap config
  was incomplete.
- Resolved in same branch/PR #14: `CHUNK_SUMMARY` rows are committed before
  subsequent provider calls and reused on retry, aggregate input is bounded,
  and chunk size/overlap are configured and tested.

## 2026-09-14 — PR #14 — 2dbed2c — CHANGES REQUIRED (review 3)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/14#pullrequestreview-5192800837
  (reviewed HEAD `2dbed2ce172a2cbbf0e983133cf5eee481f5b2fc`).
- Findings: MAJOR — overlap splitting emitted a redundant tail chunk after
  reaching EOF; MINOR — public chunk environment names differed from
  PRODUCT_SPEC §79.
- Resolved in same branch/PR #14: splitter stops once the current chunk reaches
  EOF and has a regression for the redundant-tail case; settings accept the
  required `CONTENT_CHUNK_MAX_CHARS` and `CONTENT_CHUNK_OVERLAP_CHARS` names,
  which are now the documented `.env.example` surface.

## 2026-09-14 — PR #14 — d2e4e29 — APPROVED (review 4)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/14#pullrequestreview-5192817102
  (approved product HEAD `d2e4e297cb99578f972582b1e8a7cf1ccc26c641`).
- Confirmed: durable chunk summaries, bounded aggregate, overlap behavior and
  public config contract are correct; recorded verification is 196 tests,
  Ruff PASS and `git diff --check` PASS.
- Follow-up: final docs-only correction synchronized the state HEAD reference.

## 2026-09-14 — PR #14 — 8ed1e9c — APPROVED (review 5)

- Reviewer: Orchestrator; GitHub COMMENT review:
  https://github.com/ShabanovBoris/aiinbox/pull/14#pullrequestreview-5192820769
  (approved exact HEAD `8ed1e9c837398a28ab73e909ce349e79d29e7994`).
- Confirmed: delta from approved product HEAD `d2e4e29` is exactly one
  docs-only commit changing one `IMPLEMENTATION_STATE.md` line; PR metadata and
  exact HEAD are synchronized.
- Protocol next step: merge-ready handshake; Orchestrator performs squash merge.
