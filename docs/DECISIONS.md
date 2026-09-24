# Architecture Decisions

Только текущие неочевидные архитектурные инварианты. История реализации и review
хранится в GitHub PR/commits, а не в этом файле.

## D-001 — Resumable single-process pipeline

Item processing хранит durable stage/content/checkpoints в SQLite. Retry/restart
продолжают с compatible checkpoint, а не повторяют download/STT/LLM без причины.

## D-002 — Стабильное ядро, заменяемые adapters

Telegram, HTTP/YouTube и LLM — края. `NormalizedContent`, analysis, priority,
lifecycle и persistence не должны зависеть от конкретного provider/client.

## D-003 — GitHub PR как acceptance boundary

`main` защищён. Изменение идёт через scoped branch + PR + required `quality`.
Orchestrator review привязан к exact HEAD SHA. После APPROVED этот HEAD либо
squash-merge'ится с expected SHA, либо любое новое изменение требует re-review.

## D-004 — Atomic queue claim + startup recovery

Processing claim — conditional `UPDATE ... RETURNING` для oldest QUEUED.
Single-process runtime позволяет на startup безопасно вернуть оставшиеся
PROCESSING в QUEUED.

## D-005 — Application-controlled SQLite FTS5

`item_search` — производная проекция из Item/contents, не canonical state.
Приложение синхронизирует/rebuild'ит индекс явно; отдельный search service не нужен.

## D-006 — Lifecycle action и event атомарны

Done/Snooze/Archive/Retry выполняются conditional update'ом. Event создаётся только
если transition реально выигран; повторные callbacks — no-op.

## D-007 — SQLite scheduler для digest/snooze

`reminders` хранит durable schedule/idempotency. Для этих scheduled notifications
предпочтено избежать duplicate после restart даже ценой узкого окна silent loss
между durable claim и Telegram send.

## D-008 — Bounded processing/shutdown + fail-fast supervisor

Item имеет end-to-end processing deadline. Graceful shutdown даёт workers bounded
drain. Неожиданная смерть critical worker/polling или DB infrastructure failure
роняет процесс; внешний supervisor отвечает за restart.

## D-009 — Chunk summary identity

`CHUNK_SUMMARY` reuse разрешён только при совпадении chunk index/settings и
SHA-256 exact chunk text. Position-only checkpoint недостаточен.

## D-010 — OpenRouter через OpenAI-compatible boundary

OpenAI и OpenRouter используют общий compatible analysis/vision transport, но
раздельные credentials/model IDs. OpenRouter long STT режется на bounded mono
WAV PCM 16 kHz segments.

## D-011 — Durable immediate Telegram outbox

READY/FAILED/profile DONE создают delivery intent в той же business transaction.
`DeliveryWorker` обрабатывает PENDING→SENDING→SENT; interrupted SENDING
requeue'ится на startup. Семантика — at-least-once.

Retry отменяет obsolete pending/sending failure intent; перед failure send worker
дополнительно проверяет, что Item всё ещё FAILED.

## D-012 — STT segment identity

`TRANSCRIPT_CHUNK` reuse требует SHA-256 exact segment bytes + provider/model +
segmentation contract. Legacy/index-only or mismatched checkpoints пересчитываются.
После успешного final transcript segment checkpoints удаляются.

## D-013 — Online SQLite backup + verified restore

Backup создаётся через SQLite Online Backup API, а не копированием live `.db`
файла. Snapshot проходит `PRAGMA integrity_check` и
`PRAGMA foreign_key_check`, хранится в отдельном volume и ротируется bounded
числом поколений. Restore всегда создаёт новый файл. При canonical swap
остановленного приложения старые `.db`, `-wal` и `-shm` архивируются как
единый recovery set, чтобы sidecars старой БД не применились к restored DB.

## D-014 — Off-host backup через host-owned rsync/SSH

Каждое verified SQLite backup generation состоит из `.db` и стандартного
`.sha256` sidecar. Off-host transport принадлежит deployment host, а не
application runtime: `scripts/offsite_backup.sh` использует rsync/SSH и обычный
OpenSSH key/config, не добавляя cloud SDK или storage credentials в контейнер.
Успешная replication включает download-back того же generation,
checksum + SQLite/FK verification и restore drill во временную DB. Canonical
`/data/app.db` при таком drill не изменяется.

## D-015 — Forwarding как provenance, а не source type

Telegram forwarding нормализуется на bot boundary в `items.source_metadata_json`.
Исходный `SourceType` остаётся `TEXT`/`WEB`/`YOUTUBE`/`VOICE`/`AUDIO`/`VIDEO`. Текст или
caption автора forwarded-сообщения сохраняется в существующем `USER_TEXT`
content и передаётся анализатору как source context, отдельно от `user_note`.
Это сохраняет единый processing pipeline и не смешивает чужой текст с сигналом
намерения пользователя.

## D-016 — Telegram message является границей Item

Context: одно Telegram message/post может одновременно содержать text/caption,
несколько URL и media. Разбиение такого сообщения на несколько Items теряет общий
смысл и заставляет пользователя разбирать результаты по частям.

Decision: одно входящее Telegram message создаёт один `Item`. Independently
extractable URL/media хранятся в `item_sources`; `contents.source_id` привязывает
durable checkpoints к конкретному source. После extraction все успешные части
собираются в один `NormalizedContent` и проходят один Analyzer/PriorityEngine.
Контракт финального synthesis требует учитывать каждый содержательный source и
message-level text/caption; порядок sources не должен приводить к молчаливой
потере более поздних частей.

Reason: Item соответствует пользовательской единице информации, а source остаётся
технической единицей extraction/retry.

Consequences: replay дедуплицируется по Telegram message identity; одинаковый URL
в разных сообщениях не склеивает Items. Ошибка одного source даёт `PARTIAL`, если
остаётся meaningful text/другой source; Item падает только когда анализировать
нечего или падает общий analysis. Для длинного multi-source content промежуточное
chunk summarization также должно сохранять существенную тему каждого source.

## D-017 — URL PDF сохраняет WEB source identity

Context: тип ответа URL становится известен только после secure fetch. Смена
`ItemSource.source_type` при обработке создала бы отдельный state transition,
который нужно было бы восстанавливать при crash/retry.

Decision: URL остаётся `SourceType.WEB`. Если защищённый web response подтверждён
как PDF, парсер возвращает document `NormalizedContent`, а durable checkpoint
сохраняется как `DOCUMENT_TEXT` с PDF metadata. Telegram file documents используют
`SourceType.DOCUMENT`.

Reason: исходный URL остаётся стабильной identity source, а `Content.kind` уже
точно описывает фактически извлечённое представление.

Consequences: recovery различает web page и URL PDF по durable content kind;
оба варианта используют один SSRF/DNS-pinning downloader.

## D-018 — Silent Telegram videos use visual-only analysis

Context: Some Telegram videos contain useful visual information but have no audio
stream, so audio extraction cannot produce a transcript.

Decision: When a VIDEO source has no audio stream or an empty transcript, use the
existing frame/vision path. Mark the Item `VISUAL_ONLY` only after visual notes are
successfully persisted; restore those notes as the source checkpoint after restart.

Reason: A missing transcript does not make visual content unusable, and the result
must not imply that speech was analyzed.

Consequences: If visual extraction also fails, existing failure/partial-source
handling remains in effect.

## D-019 — Профиль задаёт язык ответа

Context: язык транскрипта Telegram-видео может отличаться от языка, на котором
пользователь хочет получать краткие описания; текущий prompt ориентирует модель
на язык контента.

Decision: добавить `preferred_language` в JSON-профиль пользователя, по умолчанию
`ru`; использовать его для визуальных заметок и текстовых полей анализа VIDEO/
YOUTUBE Items. Поле `AnalysisResult.language` остаётся языком исходного материала.

Reason: язык источника — это характеристика сохранённого материала, а не
предпочтение языка ответа.

Consequences: старые JSON-профили читаются с `ru` без миграции; пользователь
может изменить значение через `/profile_update`. Остальные типы Items сохраняют
выбор языка по источнику.

## D-020 — VIDEO result ссылается на сообщение через reply

Context: Telegram message links предназначены для групп и каналов; Bot API не
даёт permalink для личного чата пользователя с ботом ([message links](https://core.telegram.org/api/links),
[message IDs](https://core.telegram.org/api/updates)). Для VIDEO Item нужно
возвращать пользователя к сообщению с вложением.

Decision: READY/FAILED уведомления для Item с VIDEO отправлять reply на исходное
сообщение в текущем чате через Bot API `ReplyParameters` ([sendMessage](https://core.telegram.org/bots/api#sendmessage)).
Для forwarded public channel отдельно сохранять кнопку перехода к публичному
оригиналу.

Reason: reply preview позволяет перейти к исходной копии видео в чате с ботом и
работает также для direct/forwarded вложений без публичного username.

Consequences: если исходное сообщение уже удалено или недоступно, Bot API всё
равно отправляет результат без reply anchor.

## D-021 — Instagram captions остаются Description

Context: Reel caption может быть единственным доступным текстом, но он не
подтверждает, что речь в самом видео была распознана.

Decision: хранить bounded caption как `DESCRIPTION`; включать его как source
context рядом с transcript/visual notes. Если transcript недоступен, vision не
дал результата, а caption содержит не менее 40 символов, источник может стать
`CAPTION_ONLY`. Не записывать caption в `TRANSCRIPT`.

Reason: сохраняется полезный публичный контекст без ложного обещания STT.

Consequences: `DESCRIPTION` и source metadata восстанавливают caption-only Item
после Retry; пользовательский результат явно помечается `CAPTION_ONLY`.

## D-022 — Killable yt-dlp workers для Instagram и YouTube downloads

Context: Python yt-dlp может продолжить блокирующую операцию после отмены
async-задачи. `asyncio.run()` также ждёт default executor при shutdown, а
отмена wrapper-задачи `to_thread` не гарантирует, что поток перестал писать.
Это относится и к on-demand загрузке YouTube-видео для Telegram.

Decision: Instagram yt-dlp/ffprobe и YouTube yt-dlp media downloads выполнять в
отдельных process groups с wall-clock timeout. При timeout/shutdown завершать
process group и удалять download directory только после подтверждённого выхода
процессов.

Reason: это сохраняет D-008 bounded processing/shutdown и исключает гонку
очистки каталога с downloader-ом. Offline test factory остаётся injectable.

Consequences: metadata/download добавляют короткий запуск дочернего Python
процесса; приложение не ждёт media tools в default executor при завершении.
YouTube media transfer имеет отдельный настраиваемый wall-clock limit.

## D-023 — On-demand video delivery через существующий outbox

Context: пользователь может захотеть получить исходный YouTube/Reel внутри
Telegram после обработки. Загрузка и отправка велики и не должны выполняться в
callback handler или хранить постоянную копию медиа.

Decision: каждая готовая media ItemSource получает свой callback и durable
`ITEM_VIDEO:<source_id>` delivery. Worker скачивает файл в уникальную temp-папку,
ограничивает его размером Bot API и wall-clock timeout, отправляет исходное
аудио/видео и удаляет временный файл после остановки downloader-а. Успешный
Telegram `file_id` сохраняется для повторной отправки.

Reason: source-scoped outbox сохраняет быстрый handler, restart recovery и
независимость нескольких видео в одном Item без новой таблицы или media storage.

Consequences: повторный запрос обычно не скачивает файл заново; crash между
Telegram send и записью `file_id` сохраняет at-least-once семантику и может дать
дубликат.

## D-024 — Короткие SQLite-транзакции вокруг внешней обработки

Context: media extraction и LLM-вызовы могут длиться секунды, пока несколько
background workers используют один SQLite-файл. Удержание read transaction в
этих паузах способно сорвать запись delivery outbox с `database is locked`.

Decision: завершать checkpoint-чтения до внешних вызовов, сохранять готовый
транскрипт до необязательного video vision и ждать SQLite writer contention не
дольше 30 секунд.

Reason: короткие транзакции сохраняют конкурентную обработку и позволяют
durable outbox доставить READY даже при обычной конкуренции workers.

Consequences: долгие provider/download вызовы не удерживают SQLite read lock;
обработка остаётся single-process, а блокировка дольше 30 секунд остаётся
инфраструктурной ошибкой.

## D-025 — Ограниченный повтор невалидного AnalysisResult

Context: OpenAI-compatible provider вернул HTTP 200 с усечённым JSON, из-за чего
уже извлечённый Instagram transcript не дошёл до READY.

Decision: задавать явный output-token budget и повторять structured analysis
не более двух раз с увеличенным budget и коротким exponential backoff, если
ответ не прошёл схему.

Reason: временная ошибка провайдера восстанавливается без ручного Retry и без
повторной загрузки источника; систематически неверный ответ всё ещё становится
FAILED.

Consequences: такой сбой может создать до двух дополнительных LLM-запросов;
текст ответа модели не попадает в ошибки и логи.

## D-026 — Telegram-ограничения принадлежат адаптеру интерфейса

Context: Telegram — текущий интерфейс, но тот же pipeline может получить другой
вход и иметь другую поверхность выдачи. Сейчас часть ограничений задаётся
конфигурацией, часть захардкожена; облачный Bot API и локальный Bot API Server
имеют разные медиа-возможности.

Decision: транспортные лимиты Telegram принадлежат его адаптеру и конфигурации
выбранного профиля Bot API. Они ограничивают только приём/представление в этом
интерфейсе и не становятся правилами домена. Настройки не могут превысить
возможности выбранного Telegram API, но могут отличаться для локального Bot API
Server или другой конфигурации развёртывания. Будущий интерфейс получает свои
лимиты и не наследует значения Telegram.

Текущие ограничения, которые adapter должен учитывать:

| Ограничение | Текущее значение или реализация | Почему это принадлежит интерфейсу |
| --- | --- | --- |
| Скачивание входящих файлов из Telegram | Облачный `getFile` ограничен 20 MB. `MAX_AUDIO_BYTES`, `MAX_VIDEO_BYTES` и `MAX_DOCUMENT_BYTES` сейчас настраиваются и по умолчанию равны 20 MB. | Это лимит получения байтов через Telegram. Локальный Bot API Server снимает download limit; HTTP/web-клиент может передать те же данные другим способом. |
| Отправка медиа в Telegram | `sendAudio`, `sendVideo` и `sendDocument` в облачном Bot API ограничены 50 MB. Сейчас `TELEGRAM_MAX_UPLOAD_BYTES` захардкожен в `app/services/delivery.py`. | Лимит определяет возможность доставки файла, а не возможность его скачать, проанализировать или сохранить. Локальный Bot API Server допускает загрузку до 2000 MB. |
| Текст сообщения | 4096 символов после разбора entities. `app/bot/formatting.py` сейчас фиксирует это в `_TELEGRAM_MAX_MESSAGE_LENGTH`. | Telegram-форматтер должен укладывать ответ в API limit; общий результат приложения нельзя заранее обрезать для всех будущих интерфейсов. |
| Подпись к медиа | Telegram принимает до 1024 символов после разбора entities. Сейчас captions короткие и отдельного лимита в конфигурации нет. | При появлении динамических подписей их длина должна ограничиваться Telegram presenter-ом, не source/domain content. |
| `callback_data` inline-кнопки | 1–64 байта. `app/bot/keyboards.py` кодирует тип действия и Item/Source IDs непосредственно в callback. | Ограничение относится к сериализации Telegram-действия. Другой интерфейс сможет использовать собственные URL, формы или action IDs. |
| Частота отправки сообщений | Ограничения зависят от чата и режима broadcast; Telegram может вернуть `retry_after`. Отдельного Telegram rate limiter сейчас нет. | Delivery adapter должен уважать серверную задержку и иметь свою bounded pacing/retry policy; бизнес-обработка не должна зависеть от квот Telegram. |

Reason: облачный Telegram Bot API ограничивает скачивание через `getFile` до 20 MB,
загрузку медиа — до 50 MB, `sendMessage` — до 4096 символов, media caption — до
1024 символов, а callback payload — до 64 байт. Эти значения описывают
конкретный transport и могут измениться или отличаться у локального Bot API
Server ([Telegram Bot API](https://core.telegram.org/bots/api), [Bots FAQ](https://core.telegram.org/bots/faq)).

Consequences / future adapter TODO: при добавлении второго интерфейса вынести
`TELEGRAM_MAX_UPLOAD_BYTES` в настройки Telegram adapter; отделить входные
transport caps от parser/analysis budgets там, где один параметр сейчас служит
обоим слоям; ограничивать текст, captions и action payload только при
формировании Telegram ответа; добавить обработку `retry_after` в Telegram
delivery policy. `YOUTUBE_MAX_AUDIO_BYTES`, `YOUTUBE_MAX_VIDEO_BYTES`,
`INSTAGRAM_MAX_AUDIO_BYTES`, `INSTAGRAM_MAX_VIDEO_BYTES` и duration caps являются
отдельными source/processing budgets: их нельзя автоматически приравнивать к
лимиту Telegram upload. Например, разрешение анализировать большой ролик не
означает, что его обязательно можно отправить обратно через Telegram.

## D-027 — Durable idempotency for explicit feedback callbacks

Context: Telegram may redeliver one callback, while a later intentional click
must remain a new feedback signal for future analysis.

Decision: store a nullable idempotency key on Event and enforce uniqueness per
user in SQLite. Telegram feedback uses the namespaced CallbackQuery identity.
Category/type corrections also persist a user-scoped callback receipt in the
same serialized transaction, including when the selected value is already
current or a recognized action is rejected by a stale UI target; neither case
should create a semantic correction Event. READY/category-token applicability
and receipt claim are one BEGIN IMMEDIATE operation, so a concurrent retry
cannot cross from rejected to applied between two transactions.

Reason: transport retry identity is distinct from the meaning or age of a
feedback event, and application-only existence checks race under concurrent
callbacks.

Consequences: legacy and non-callback Events keep a NULL key; later feedback
with a new callback identity remains append-only history. The separate receipt
preserves no-op/stale callback idempotency without adding synthetic Events to
the behavioural history.

## D-028 — Behaviour rank is a derived projection over current Item metadata

Context: PM-06 must personalize candidate scores from sparse user feedback while
preserving semantic priority, manual interest and the current `/today` order.

Decision: derive category/type affinity on demand from current Item metadata and
the owner's informative Events. Reload candidate facts and read matching history
with two bounded user-scoped queries, smooth each dimension independently, and
keep every component in an immutable result value. Round half points away from zero.

Reason: the existing Item + Event data is sufficient at personal scale; a cache
or persisted score would add stale state and could overwrite semantic meaning.

Consequences: `personal_rank` is explainable and bounded but is not persisted or
used by `/today`; callers can adopt it in a later ranking phase without changing
`priority_score` or `interest_level`.

## D-029 — Preserve signal magnitude in behaviour affinity

Context: PM-06 signal weights and recency buckets express different strengths,
but dividing their weighted sum by the sum of absolute weights cancels those
differences whenever the evidence has one sign.

Decision: calculate raw affinity as the mean of effective signal weights by
informative Event count, then apply the separate sparse-history confidence.

Reason: explicit feedback must outweigh weak lifecycle inference, and recent
events must outweigh older events even when the user's history is one-sided.

Consequences: policy weights and recency affect both one-sided and mixed
history; each event remains bounded to [-1, +1], and sparse histories remain
smoothed toward neutral by K.

## D-030 — Attention score is a time-dependent projection

Context: PM-07 combines semantic priority, PM-06 behaviour, manual interest,
Item age, due date and recent exposure; the result changes as time passes and
the user sees previews.

Decision: compute `attention_score` on demand from canonical Item facts and
user-scoped `TODAY_SHOWN` / `ATTENTION_SHOWN` Events. Persist
`TODAY_SHOWN` only after the `/today` list is delivered and `ATTENTION_SHOWN`
only after each separate preview card is delivered, outside the Telegram send
and in short write transactions.

Reason: storing a derived, changing score would create stale ranking state, and
recording an exposure before Telegram delivery would misrepresent what the user
saw.

Consequences: PM-06 ignores the exposure Event as a preference signal;
`priority_score` and `/today`/digest ordering remain unchanged. No schema
migration is required.

## D-031 — Proactive Attention uses durable Reminder claims

Context: PM-08 must pace proactive delivery across worker restarts and competing
worker attempts without changing PM-07 ranking or holding SQLite locks during
Telegram I/O.

Decision: derive the daily budget, minimum gap and same-Item cooldown from
successful Reminder history. Serialize per-user digest, snooze and proactive
send claims with short `BEGIN IMMEDIATE` transactions; a partial unique index
also enforces one open `PROACTIVE_ATTENTION` claim. Store a claim timestamp and
generation, bound Telegram retries below the lease, and fence recovery/finalize
updates by generation. The two-minute send deadline starts at `claimed_at`, so
pre-send delay consumes the window instead of extending a live sender past
lease recovery. Re-run PM-07 under the serialized prepare transaction so a
stale candidate cannot pass the threshold using an old rank. Record
`ATTENTION_SHOWN` with successful delivery finalization. Capture the prepare
timestamp under the same writer lock for quiet hours, ranking and cooldown, and
record successful `sent_at` when Telegram returns; PM-08 local-day budgets and
gaps therefore follow delivery completion across scheduler and timezone
boundaries. Existing users receive an explicit Attention OFF setting during
rollout.

Reason: Reminder rows are durable delivery facts; derived counters and
duplicated ranking state would drift from actual history.

Consequences: proactive claims can be re-evaluated after a bounded lease, and
an expired owner cannot alter the recovered claim. Digest and snooze retain
their no-replay behavior; their active claims temporarily reserve the user
against proactive sends. An ambiguous Telegram outcome or process stop after
acceptance but before SQLite finalization can still cause a duplicate; delivery
across Telegram and SQLite is not exactly once.

## D-032 — Attention hooks are grounded derived Content

Context: PM-09 needs to explain why a saved Item may be worth revisiting without
turning generated wording into a new source of factual truth.

Decision: persist hooks as `ContentKind.ATTENTION_HOOK`. Every hook points to an
allowed original Content row, carries a whitespace-exact evidence excerpt and
generator version, and inherits `source_id` from that evidence. Revalidate stored
hooks on reuse and exclude them from FTS. Generate lazily after a durable PM-08
claim, outside SQLite transactions, under the existing absolute send deadline;
run PM-08's final revalidation afterward and fall back to its deterministic
reason on hook/provider failure.

Reason: the `contents` table already owns durable source text and provenance, so
it can preserve evidence without another table or any repeated web extraction.

Consequences: generated hooks remain presentation derivatives, cannot ground
later hooks or pollute source search, and are reused across reminders with a
stable template attribution stored in the Reminder payload.

## D-033 — Generic motivation is a durable PM-08 intervention

Context: backlog-level nudges have no single Item, but remain unsolicited
notifications subject to the same interruption policy as proactive reminders.

Decision: derive generic candidates only from deterministic Item/Event facts.
Persist each intent as `MOTIVATION_NUDGE` with `item_id=NULL` and a local-day
slot identity guarded by partial unique indexes. Share PM-08's daily budget,
minimum gap, quiet hours and cross-type claim reservation; apply a separate
intensity-based generic cap. Prefer proactive at levels 1–3 and alternate
proactive/generic at levels 4–5. Recompute candidate facts at final preparation.

Reason: one durable ReminderWorker arbitration path prevents generic motivation
from becoming a second scheduler or bypassing notification fatigue controls.

Consequences: only successful delivery consumes budget/gap; generic rows contain
bounded facts and template identity and do not use PM-09 hooks. PM-11 records
delivery and explicit reminder feedback independently. The existing
Telegram/SQLite at-least-once boundary still allows a duplicate after a crash
between accepted Telegram send and finalization.

## D-034 — Reminder Events carry explicit delivery identity

Context: Item Events alone cannot distinguish an Item's ordinary lifecycle from
the outcome of one specific reminder, and generic motivation has no Item.

Decision: Reminder Events store `reminder_id` explicitly; Item reminders also
store `item_id`, while generic nudge events may rely on Reminder alone. Record
`REMINDER_SENT` with the successful Reminder finalization. Derive bounded
preference and fatigue adjustments from Event history rather than mutable Item
fields or persisted counters. Record `REMINDER_OPENED` only for bot-mediated
actions with an observable callback.

Reason: reminder attribution remains stable across mutable Item changes, generic
notifications fit the same event model, and Telegram URL clicks cannot be
observed by the bot.

Consequences: Event history is the source for feedback projections; the
database enforces one PM-11 event of each type per Reminder. Historical sends
are not synthesized during migration, and URL-button clicks do not affect
feedback calculations.

## D-035 — Weekly review is an on-demand read projection

Context: weekly reflection combines Item, Event, and Reminder facts but does not
own canonical user state.

Decision: compute `/weekly` on demand over the user's last seven local calendar
dates and current backlog. Reuse the existing calendar-window helper and PM-07
ranking, and emit no exposure Events or persisted report.

Reason: reflection stays deterministic and read-only without creating a second
analytics state model or changing future Attention selection.

Consequences: category history reflects current `Item.category`; v1 has no
scheduled weekly push or report snapshot.
