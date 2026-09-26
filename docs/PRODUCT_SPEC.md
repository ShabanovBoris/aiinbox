# Personal AI Inbox — Product Contract

> Этот файл описывает **текущее поддерживаемое поведение**, а не историю реализации.
> Пользовательская справка: `docs/BOT_USAGE.md`.
> Операционная эксплуатация: `docs/RUNBOOK.md`.
> Архитектурные инварианты: `docs/DECISIONS.md`.

## 1. Цель проекта

Personal AI Inbox принимает входящий материал, извлекает его содержимое, анализирует,
сохраняет, приоритизирует и возвращает пользователю небольшой actionable список.

Основной сценарий:

```text
увидел полезное → отправил боту → забыл
                         ↓
             система сама организует,
             оценивает и возвращает позже
```

## 2. Главный продуктовый принцип

Это не bookmark manager и не ручной TODO. Система должна минимизировать внимание,
которое пользователь тратит на сортировку входящего потока.

## 3. Ключевые архитектурные требования

- single-process modular monolith;
- SQLite — canonical durable state;
- тяжёлая обработка только в background workers;
- внешние системы изолированы adapters;
- restart/retry не должны терять уже выполненную дорогую работу;
- никаких Redis/Celery/Kafka/PostgreSQL без доказанной необходимости.

### 3.1. Минимизация сложности

Предпочитать простой код, явные транзакции, bounded concurrency и текущие
библиотеки проекта. Не строить placeholder architecture для будущих клиентов.

## 4. Технологический стек

Текущий стек:

```text
Python 3.12
aiogram
SQLAlchemy 2 + aiosqlite
Alembic
Pydantic 2 / pydantic-settings
httpx + trafilatura
yt-dlp
ffmpeg / ffprobe
OpenAI SDK-compatible providers
pytest / pytest-asyncio
ruff
```

Playwright runtime отключён: безопасной browser network boundary в проекте нет.

## 5. Архитектура верхнего уровня

```text
Telegram → ingestion → SQLite queue → ProcessingWorker
                                 ↓
                         extraction/normalization
                                 ↓
                           LLM Analyzer
                                 ↓
                        PriorityEngine
                                 ↓
                         READY + outbox
                                 ↓
                             Telegram
```

Напоминания обслуживает `ReminderWorker`, а READY/FAILED/profile/Ask results,
пользовательские export-файлы и явно запрошенная отправка видео используют
durable `DeliveryWorker`. `AskWorker` и `ExportWorker` обрабатывают отдельные
запросы пользователя вне Telegram handler.

## 6. Текущая поддерживаемая поверхность

Поддерживаются:

- plain text;
- web URL;
- YouTube URL;
- public Instagram Reel URL, processed best-effort through yt-dlp;
- Telegram voice;
- Telegram audio;
- Telegram video: transcript + optional representative-frame vision;
- Telegram PDF/TXT/Markdown/DOCX как document ItemSource; PDF поддерживается и по
  URL;
- forwarded Telegram text/URL/voice/audio/video/document с сохранением доступного
  provenance;
- forwarded photo-post с URL в caption/text_link: caption и ссылки сохраняются как
  единый source content, само изображение не анализируется;
- `/start`, `/menu`, and `/help` expose a compact inline menu; the Telegram
  command list is registered from code. Guided Ask/Search accept one text input
  without creating general conversation history;
- `/today`, `/attention`, `/weekly`, `/inbox`, `/category`, `/search`, `/ask`;
- `/export [compact|full]`;
- `/profile`, `/profile_update`;
- `/settings`, `/settings attention`;
- Done / Later / Archive / Retry;
- explicit READY Item feedback: Useful, Not interesting, category/type correction,
  priority direction and summary quality;
- daily digest, snooze resurfacing, PM-08 proactive Attention и PM-10 factual
  motivational nudges.

Direct Telegram video поддержан. Видео, которое Telegram прислал как `Document`,
тоже нормализуется в VIDEO по MIME/расширению. PDF, TXT, Markdown и DOCX
принимаются direct/forwarded как `DOCUMENT`; URL PDF проходит существующую
SSRF-safe web boundary и сохраняется как `DOCUMENT_TEXT`. OCR для сканированных
PDF не выполняется.
Video note, direct image и остальные document formats не поддерживаются.
Forwarded photo с caption сохраняет и анализирует caption/ссылки; само изображение
пока не анализируется. Photo без caption остаётся неподдерживаемым.
Instagram support is limited to one canonical Reel URL per source. Profile feeds,
Stories and protected/private media are not crawled or bypassed. Speech uses the
existing transcription provider; caption remains description context, and visual
analysis uses the shared representative-frame pipeline.

## 7. Текущие non-goals

Не реализованы и не должны появляться без отдельной задачи:

- Android client / HTTP API;
- web frontend;
- embeddings / vector database / RAG;
- ML ranking;
- Ollama/local provider;
- Calendar/Notion integrations;
- browser fallback для сложных страниц;
- playlists, DRM/paywall/CAPTCHA bypass;
- direct Telegram image ingestion;
- OCR, spreadsheets, presentations and other non-PM-03 document formats;
- Telegram video note.

## 8. Структура проекта

`app/` разделён на bot adapters, extractors, LLM adapters, services, workers,
storage и domain. Business logic не должна зависеть от Telegram/OpenAI SDK напрямую.

## 9. Модель Item

`Item` — пользовательская единица входа: одно Telegram message/post. Он хранит
processing/lifecycle/analysis state и provenance. Вложенные URL/media хранятся как
`item_sources`; extracted text/transcripts — в `contents` и могут ссылаться на
конкретный `source_id`.

## 10. Processing status и lifecycle state

Разделять:

```text
ProcessingStatus: QUEUED / PROCESSING / READY / FAILED
ItemState:        ACTIVE / SNOOZED / DONE / ARCHIVED
```

Retry меняет processing status; Done/Later/Archive — lifecycle state.

## 11. ItemType

Поддерживаются:

`ACTION`, `LEARN`, `READ`, `WATCH`, `IDEA`, `REFERENCE`, `SOMEDAY`.

## 12. Категории

Категория — динамическая строка из анализа, не enum. Она называет основную тему
сохранённого содержимого; профессия, цели и интересы пользователя не являются
тематическими доказательствами. История категорий — только подсказка для повторного
использования точного тематического названия, не закрытый справочник.

## 13. Обработка Telegram сообщения

- одно входящее Telegram message/post → ровно один `Item`;
- исходный text/caption сохраняется целиком как `USER_TEXT`;
- окружающий URL текст direct-message сохраняется также как `user_note`;
- каждый distinct URL внутри сообщения становится дочерним `ItemSource` типа
  `WEB`/`YOUTUBE`; повтор одного URL внутри того же сообщения схлопывается;
- voice/audio/video/document становятся media/file `ItemSource`; caption и URL
  рядом с ними принадлежат тому же Item;
- каждый ItemSource извлекается и checkpoint'ится независимо, после чего все
  успешные source contents объединяются в один `NormalizedContent` и один analysis;
- для multi-source Item Analyzer обязан учитывать каждый содержательный успешный
  source и исходный text/caption: итоговые title/summary описывают Item целиком,
  а не только первый или наиболее длинный source; если темы различаются, каждая
  тема должна быть кратко отражена в общем результате;
- ошибка одного вложенного source не роняет Item, если остаётся meaningful text
  или другой успешно извлечённый source: Item завершается `READY` с
  `analysis_completeness=PARTIAL`;
- видео без доступной транскрипции может завершиться `READY` с
  `analysis_completeness=VISUAL_ONLY`, если vision успешно разобрал кадры;
- если ни один source не извлечён и meaningful text отсутствует, Item становится `FAILED`;
- один и тот же URL в разных Telegram messages не склеивает Items: контекст сообщения
  является частью пользовательской единицы;
- forwarding не является отдельным source type: provenance хранится отдельно;
- доступный forward origin сохраняется в `items.source_metadata_json`;
- исходный forwarded текст/подпись хранится целиком как source content, включая
  видимые и восстановленные из Telegram entities ссылки, и не становится
  `user_note` пользователя AIInbox.

## 14. Работа Telegram handler

Handler обязан быстро:

1. проверить allowlist;
2. валидировать вход;
3. сохранить durable Item/job;
4. commit;
5. отправить ACK.

Extraction/LLM/ffmpeg в handler запрещены.

## 15. Фоновая обработка

`ProcessingWorker` атомарно claim'ит QUEUED Item, запускает pipeline и доводит
его до READY или FAILED. DB infrastructure failures выходят к process supervisor.

## 16. ContentExtractor API

Extractors переводят внешний source в единый `NormalizedContent`. Source-specific
детали не должны протекать в Analyzer/PriorityEngine.

## 17. NormalizedContent

Нормализованный контент содержит source type, text и опциональные title/url,
user note, author/language/duration/metadata.

## 18. Web page extraction

WEB использует SSRF-safe HTTP transport, bounded redirects/download, trafilatura.
PDF response сначала проверяется по MIME и signature, затем разбирается тем же
document parser без второго HTTP downloader. Playwright fallback отключён.

## 19. Критерий успешного extraction

Результат должен содержать содержательный текст. Слишком короткая/неизвлекаемая
страница завершается контролируемым `EXTRACTION_FAILED`.

## 20. Защита web extractor от SSRF

Разрешены только HTTP(S) публичные адреса. Private/loopback/link-local и иные
запрещённые диапазоны блокируются. DNS валидируется и соединение pin'ится к
проверенному IP; каждый redirect проверяется заново.

## 21. Prompt injection protection

External content — данные, не инструкции. LLM prompt должен отделять task/system
instructions от web/transcript/user content. LLM provider не получает tools.

## 22. YouTube/video pipeline

YouTube: metadata → пригодные subtitles → иначе audio download + STT. Если
подтверждено отсутствие audio track или STT вернул пустую транскрипцию,
применяется тот же visual-only fallback, что для Telegram VIDEO: при доступной
vision capability сохраняются `VISUAL_NOTES`, и Item получает completeness
`VISUAL_ONLY`. Ошибки загрузки или провайдера транскрипции не маскируются под
отсутствие аудио и идут по обычной retry/error policy.

Vision также остаётся опциональным обогащением успешной транскрипции.

Instagram Reel: only `/reel/<id>` URLs on `instagram.com` and `www.instagram.com`
are routed to the Instagram ItemSource before generic WEB routing. yt-dlp metadata
is fetched before media; audio/video, duration, output path and temporary storage
are bounded. The configured cookie file is optional and operator-provisioned;
private-account bypass and browser-cookie harvesting are unsupported. A
meaningful caption may become a `CAPTION_ONLY` source when speech is unavailable;
it is stored as `DESCRIPTION`, never `TRANSCRIPT`. Transcript and visual notes
reuse the existing source-local durable checkpoints and aggregate analysis.

## 23. Анализ визуальной части видео

Frame extraction bounded до materialization и сохраняет temporal coverage.
Кандидаты periodic/scene разрежаются и deduplicate-ятся до configured limit.

## 24. Vision должен быть capability

Vision выполняется только если provider declares `capabilities.vision`.
Ошибка vision не должна ломать валидный transcript-only результат.

## 25. Временные файлы

Media/frames и скачанные документы живут только в configured temp directory и
удаляются после extraction. Durable transcripts/document text остаются в SQLite.
В Docker temp directory — tmpfs.

## 26. Audio / Voice

Telegram voice/audio сохраняются как durable Item по `file_id`, скачиваются worker-ом,
транскрибируются provider adapter-ом и дальше проходят общий pipeline.

## 27. LLM abstraction

Domain/services работают через provider boundary. Model IDs, credentials и
base URLs принадлежат composition/config layer.

## 28. Providers

Поддерживаются:

- `LLM_PROVIDER=openai`;
- `LLM_PROVIDER=openrouter`.

OpenRouter использует OpenAI-compatible analysis/vision/STT adapters с отдельными
credentials/model IDs. Ollama не реализован.

## 29. Конфигурация модели

Все provider/model settings задаются environment variables.
Business code не содержит hardcoded model IDs.

## 30. Structured output

Analyzer обязан возвращать schema-validated `AnalysisResult`. Парсинг
произвольного prose регулярками запрещён.
Некорректный структурированный ответ получает до двух повторных попыток с
короткой задержкой и увеличенным лимитом генерации; после третьего невалидного
ответа Item переходит в `FAILED`.

## 31. Ограничения AnalysisResult

Структура включает title, summary, category, ItemType, tags, priority factors,
estimated action, next action, reason, language и confidence. Pydantic валидирует
shape/types и запрещает незнакомые поля, включая `priority_score`; итоговый score
считает приложение. Поля `next_action`, `priority_reason`, `estimated_action_minutes`
и ItemType остаются совместимыми с текущими потребителями.

Summary первым предложением сообщает вывод, результат, рекомендацию или resolved
claim, если источник их подтверждает; exploratory и inconclusive материал описывает
неопределённость без выдуманного победителя. Summary — короткая canonical prose для
поиска, Ask, export и других поверхностей, без Telegram markup. Title называет
содержательную тему, а не формат источника. `next_action` — конкретный шаг либо null;
когда шага нет, длительность тоже null. `priority_reason` остаётся внутренним
объяснением факторов и может учитывать профиль.

Для Item с Telegram VIDEO/YouTube title, summary, next action и reason создаются на
языке из профиля, даже если transcript на другом языке; поле `language` сохраняет
язык исходного материала. Для остальных источников остаётся правило языка самого
контента.

## 32. Long content

Длинный текст анализируется через bounded chunking + intermediate summaries,
после чего выполняется aggregate/final analysis. Каждый chunk summary сохраняет
сильные claims, результаты, рекомендации, изменения и противоречия; если в части
есть вывод, он помещается в начало её summary. Итоговый aggregate сохраняет порядок,
равномерно выделяет место каждой части и остаётся в пределах chunk-size bound.
Нумерация частей — только framing для final analysis и не добавляется в persisted
summary.

## 33. Chunking

Chunk boundaries paragraph-aware. Durable `CHUNK_SUMMARY` reuse разрешён только
при совпадении index, chunk-size/overlap settings, SHA-256 exact chunk text и
`generator_version`. Текущая версия семантики — 2; после изменения требований к
chunk summary version увеличивается. Устаревшая запись не переиспользуется: после
успешной пересборки она заменяется в своём chunk slot, а больше не нужные поколения
удаляются, чтобы не дублировать derived evidence в FTS/Ask fallback. Каждая часть
фиксируется отдельно; SQLite-транзакция не охватывает вызов LLM.

## 34. Пользовательский профиль

Профиль хранится per user и участвует в анализе будущих Items как контекст
персональной релевантности, но не как доказательство темы.

## 35. UserProfile

Профиль может содержать profession, domains, weighted goals, interests,
constraints, free text и `preferred_language` в формате BCP-47. По умолчанию
используется `ru`; `/profile_update` меняет язык, например, на `en`. Параметр
управляет ответом для видео-Items; язык исходного transcript его не переключает.

## 36. /profile

`/profile` показывает текущий профиль.
`/profile_update <instruction>` создаёт durable background job.
Старые Items автоматически не re-analyze-ятся.

## 37. Priority Engine

Финальный `priority_score` считает deterministic code, а не LLM. Профиль может
влиять на user-relative `goal_fit`, `interest_fit`, `importance` и внутренний
`priority_reason`; category и factual summary выводятся из сохранённого содержимого.

## 38. Базовая формула priority

Текущие веса:

```text
goal_fit        0.30
importance      0.20
urgency         0.15
long_term_value 0.15
interest_fit    0.10
quick_win       0.10
```

Результат clamp'ится в 0..100.

## 39. Quick win

`quick_win = max(0, 1 - estimated_action_minutes / 60)`.
Если duration неизвестна — нейтральное значение 0.5.

## 40. Приоритет ≠ тип

ItemType описывает характер материала; priority — персональную полезность/срочность.
Они не заменяют друг друга.

PM-06 добавляет вычисляемый по текущим category/type и Event-истории
`personal_rank`: сглаженная поведенческая affinity может изменить semantic
`priority_score` не более чем на 15 пунктов. Результат не хранится и не меняет
`priority_score`; `interest_level` остаётся отдельным сигналом для будущего
Attention Ranking.

PM-07 вычисляет on demand отдельный `attention_score` из PM-06
`personal_rank`, ручного интереса, возраста Item, suggested due date и истории
показов. Он не хранится и не меняет `priority_score`, `interest_level`, due date
или lifecycle. История показов использует user-scoped `TODAY_SHOWN` и
`ATTENTION_SHOWN`, записываемые только после успешной отправки соответствующего
списка или карточки в Telegram; PM-06 продолжает игнорировать `ATTENTION_SHOWN`
как сигнал предпочтений.

## 41. Today selection

`TodayService` выбирает только READY + ACTIVE Items типов ACTION/LEARN/READ/WATCH,
сортирует по `priority_score` desc и ограничивает результат. PM-06 не меняет
порядок `/today`.

## 42. /today

По умолчанию возвращает 3 Items, hard max — 5.

### `/attention [1-5]`

Отдельная ручная preview-команда ранжирует READY + ACTIVE Items типов
ACTION/LEARN/READ/WATCH по текущему attention score. По умолчанию показывает до
3 карточек, абсолютный максимум — 5. Рейтинг учитывает PM-06 `personal_rank`,
ручной интерес, возраст, neglect, persisted suggested due date, важные старые
Items и подавление недавно показанных Items. Каждая карточка сохраняет
существующие Item/ItemSource actions. После успешной отправки карточки
записывается `ATTENTION_SHOWN`; автоматических сообщений команда не планирует.
`/today` и daily digest остаются основаны на `priority_score`.

Ручные карточки `/attention` показывают позицию, title и ограниченный preview
сохранённого summary. Attention/priority score, интерес, возраст и ranking reason
не выводятся в карточке. Обычные source actions остаются доступны через компактную
клавиатуру Item; `ATTENTION_SHOWN` по-прежнему записывается только после успешной
отправки каждой карточки.

### `/weekly`

On-demand read-only обзор последних семи локальных календарных дней, включая
сегодня, в сохранённом `User.timezone`. Границы суток переводятся в UTC через
общий DST-safe helper. Создания считаются по `Item.created_at`, завершения и
архивы — по `DONE` и `ARCHIVED` Events; `REMINDER_DONE` не заменяет и не
дублирует обычный `DONE` в потоке. Возраст backlog измеряется прошедшим временем.

Категории завершённых и созданных Items читаются из текущего `Item.category`,
поскольку исторический снимок категории на каждом lifecycle Event не хранится.
Attention outcome counts берутся только из PM-11 Events, связанных с
`PROACTIVE_ATTENTION` Reminder. Эти агрегаты остаются внутренней частью read model;
Telegram-проекция показывает только до трёх concrete recommendations по текущему
PM-07 ranking. При отсутствии рекомендаций показывается нейтральное empty state,
независимо от aggregate activity.

PM-12 v1 не пишет Item/Event/Reminder, не меняет профиль или settings, не
записывает `TODAY_SHOWN`, `ATTENTION_SHOWN` или `WEEKLY_SHOWN`, не сохраняет
отчёт, не вызывает LLM и не отправляет запланированные weekly notifications.

## 43. /inbox

Возвращает последние Items без lifecycle-фильтра через страницы по 10 Items;
порядок — `created_at DESC, id DESC`. Запрос страницы использует ограниченный
`LIMIT page_size + 1`, а кнопки перехода позволяют дойти до любого сохранённого
Item. Размер страницы ограничивает один ответ Telegram, но не доступное число
Items и не является storage/user quota.

## 44. /category

Без аргумента — алфавитные страницы динамических категорий и их counts; с
аргументом — priority-ordered страницы Items категории. Категории и Items не
имеют presentation-level total cap. Callback содержит стабильный короткий token;
handler разрешает его повторно против текущих категорий действующего owner и
отвергает stale/colliding token.

## 45. /search

SQLite FTS5 ищет title, summary, user note, tags и persisted original source
content. Derived `ATTENTION_HOOK` text не индексируется.
DONE/ARCHIVED остаются searchable. Default limit — 10.

### `/ask`

`/ask <вопрос>` создаёт standalone `AskJob`; Telegram handler только проверяет
пустой ввод/лимит 2000 символов, сохраняет job и отправляет быстрый ACK.
`AskWorker` использует текущий provider и SQLite FTS5, по умолчанию получает до
8 Items (абсолютный максимум — 10) без фильтра lifecycle state. Контекст строится
только из уже сохранённых Item/ItemSource/Content, с пределом 6000 символов на
Item и 40000 суммарно; URL не загружаются. `ATTENTION_HOOK` и
`TRANSCRIPT_CHUNK` не являются evidence, `CHUNK_SUMMARY` — только fallback при
отсутствии исходного persisted content.

LLM возвращает строгую схему с Item/source IDs. Application принимает ссылки
только из точного набора контекста, переданного provider, и строит названия и
HTTP(S)-кнопки из persisted rows. Ответ в `preferred_language` доставляется через
durable outbox; после успешной Telegram delivery сгенерированный текст удаляется
из payload. Ответ не записывается в Item, Content, FTS, Profile или Event.
Пустая выдача завершает job без LLM-вызова; `/search` сохраняет прежний список
результатов.

## 46. Почему хранить extracted content

Сохранять именно тот текст/transcript, который анализировался: source может
измениться или исчезнуть; persisted content нужен для resume/search/re-analysis.

## 47. Database schema

Canonical SQLite tables:

- `users`;
- `items`;
- `item_sources`;
- `contents`;
- `events`;
- `reminders`;
- `profile_update_jobs`;
- `ask_jobs`;
- `export_jobs`;
- `deliveries`;
- FTS5 virtual table `item_search`.

`items.source_metadata_json` хранит необязательный source-envelope provenance,
например нормализованный Telegram forward origin. Отсутствующие origin-поля не
восстанавливаются догадками.

`item_sources` хранит independently extractable части Item и их локальный
`PENDING/READY/FAILED` extraction result. `contents.source_id` связывает durable
WEB text/transcript/visual checkpoint с конкретным source; `NULL` означает
Item-level content.

Schema changes — только Alembic migrations.

## 48. contents

Поддерживаемые kinds:

`USER_TEXT`, `WEB_TEXT`, `TRANSCRIPT`, `TRANSCRIPT_CHUNK`,
`VISUAL_NOTES`, `DESCRIPTION`, `CHUNK_SUMMARY`, `DOCUMENT_TEXT`,
`ATTENTION_HOOK`.

`TRANSCRIPT_CHUNK` — retry checkpoint и удаляется после успешной сборки final transcript.
`ATTENTION_HOOK` — производный presentation-контент с metadata, указывающей на
проверенный исходный `Content`; `source_id` наследуется от supporting Content.
Hooks не являются доказательством для других hooks и исключены из FTS.

## 49. events

Lifecycle/user feedback events пишутся в той же транзакции, что выигравший state
transition. Повторный callback не должен создавать второй event.

PM-05 добавляет USEFUL, NOT_INTERESTING, CATEGORY_CORRECTED, TYPE_CORRECTED,
PRIORITY_HIGHER, PRIORITY_LOWER и SUMMARY_REPORTED_WRONG. Event остаётся
auxiliary history; исправления категории и типа меняют canonical Item вместе с
Event в одной транзакции, остальные сигналы Item не меняют. Nullable
idempotency_key с уникальностью по паре user_id/idempotency_key схлопывает
повторную доставку Telegram callback, сохраняя возможность нового события от
последующего нажатия. Для category/type correction отдельная
feedback_callback_receipts сохраняет callback receipt даже при no-op, который
не должен создавать семантический Event; receipt и реальное исправление
фиксируются одной SQLite-транзакцией. Проверка READY/category-token и запись
receipt выполняются в одной транзакции. Распознанный callback со stale или
временно недоступной целью также потребляется без Event, если Item принадлежит
пользователю.

PM-11 reminder Events (`REMINDER_SENT`, `REMINDER_OPENED`, `REMINDER_SNOOZED`,
`REMINDER_DONE`, `REMINDER_DISMISSED`, `REMINDER_DISLIKED`) ссылаются на
конкретный `reminder_id`; события к конкретному материалу также сохраняют
`item_id`. `MOTIVATION_NUDGE` оставляет `Reminder.item_id=NULL` как ключ
пользовательского claim, но новые focused reminders записывают выбранный `item_id`
в Reminder Events. Исторические reminders без focus остаются только с `reminder_id`.
Обычные `DONE`, `SNOOZED`, `ARCHIVED` и PM-05 feedback не выводятся из reminder
reactions и остаются отдельными сигналами. Для успешной доставки `REMINDER_SENT`
фиксируется в той же транзакции, что и `Reminder.SENT`; исторические SENT rows не
backfill-ятся.

## 50. reminders

`reminders` хранит daily digest, snooze, PM-08 proactive scheduling и PM-10
user-level motivation claims. `MOTIVATION_NUDGE` использует `item_id=NULL` и
отдельную уникальность локального дневного слота; новый focus хранится в payload.
Events могут независимо указывать Item и Reminder, но хотя бы одна ссылка обязательна; один Reminder
может иметь не более одного Event каждого PM-11 типа.
`deliveries` — отдельный durable outbox для READY/FAILED/profile notifications,
Ask outcomes, export-файлов и других явных Telegram delivery.

## 51. Daily digest

Настройки: enabled, local time, quiet hours + user timezone.
Подборка создаётся не чаще одного раза за локальный день и использует TodayService.
Ручной `/today` и scheduled daily используют один object-centric formatter; меняется
только заголовок. Пользователь видит названия конкретных сохранений, next action или
короткую сводку и оценку времени при её наличии. Номера позиций, priority score и
aggregate counters в тексте не показываются.

## 52. Snooze

Later предлагает tomorrow/week/month. Item становится SNOOZED.
При due time возвращается ACTIVE и получает reminder notification вне quiet hours.

PM-08 также может отправить один proactive reminder за worker cycle на основе
PM-07 ranking. Existing users начинают с Attention OFF; новые users — ON с
интенсивностью 3. Дневной лимит интенсивности общий для digest и proactive
уведомлений, а snooze не расходует его, но влияет на минимальный интервал.
Quiet hours блокируют proactive delivery; после них Items ранжируются заново,
без догоняющей очереди. Только успешный proactive send создаёт `SENT` Reminder
и `ATTENTION_SHOWN`; derived `attention_score` не сохраняется.

PM-09 может дополнить такой reminder сохранённым contextual hook. Генератор v2
работает лениво после durable claim, только по уже сохранённому исходному
Content; он не запускается на ingestion или `/attention` и не загружает URL
повторно. Evidence валидируется как точный source excerpt, а `source_id` берётся
из подтверждающего Content. Hook generation выполняется вне SQLite-транзакции
в оставшемся PM-08 send window; затем worker выполняет обычную финальную
revalidation. Старые версии hook остаются историческим Content и не используются
повторно.

Proactive Reminder показывает `🎯` и title, затем напрямую grounded hook. Если
валидного hook нет, formatter использует whitespace-нормализованный preview
outcome-first `Item.summary` до 700 символов, а при пустом summary оставляет
только title. Summary используется только для показа и никогда не становится
evidence новой генерации hook. В тексте уведомления нет attention/priority score,
interest, возраста Item или объяснения ranking; эти данные остаются внутри
selection, claim и Reminder snapshot.

Основная клавиатура Reminder оставляет Original и максимум два source actions;
overflow открывается в `Источники`, а реакции PM-11 находятся за `••• Ещё`.
Навигация меню не создаёт Events и не меняет Item/Reminder. Наблюдаемость
Original и video resend, а также необозримость прямых URL-click сохраняются.

PM-10 вычисляет только детерминированные факты из подходящих сохранений и
lifecycle Events. Эти факты остаются внутренними сигналами; каждое новое
sendable мотивационное напоминание связано с одним сохранением, выбранным в
существующем PM-07 порядке. Telegram показывает его название и сохранённую сводку,
а не агрегатную мотивационную фразу. Текст не использует LLM, профиль, исходное
содержимое или `ATTENTION_HOOK`. Факты охватывают старые важные и интересные
сохранения, короткие задачи, дневную разницу новых/разрешённых записей, текущую
серию завершений и последние семь локальных календарных дней. Календарные границы
используют `User.timezone` и IANA timezone, включая DST.

`generic_motivation_enabled` независимо выключает generic нуджи, сохраняя
proactive Attention. Attention Manager OFF блокирует оба автоматических
intervention. Новые пользователи получают generic motivation ON; migration
добавляет явный OFF существующим пользователям, у которых ключ отсутствовал.

`MOTIVATION_NUDGE` входит в общий дневной budget и minimum gap, но имеет
дополнительный предел: Calm — 0, Light/Normal/Active — 1, Aggressive — 2 за
локальный день. Уровни 1–3 выбирают sendable Item-specific proactive reminder
перед generic; на уровнях 4–5 arbitration учитывает последнюю успешную
Attention-family delivery, но generic может конкурировать повторно после
четырёх часов без обязательного proactive между отправками. Worker выбирает не
более одного Attention-family intervention за цикл.

Generic intent хранится как Reminder с `item_id=NULL`. `scheduled_at` кодирует
локальную дату и номер слота, а partial unique indexes защищают слот и один
открытый claim пользователя от SQLite NULL-уникальности. Claim/final prepare
короткие и сериализованные: факты, opt-in, тихие часы, общий budget, generic cap,
интервал и повтор kind перепроверяются до Telegram; сетевой вызов происходит
после commit. Только успешный возврат Telegram переводит Reminder в `SENT` и
расходует лимиты. PM-11 добавляет `REMINDER_SENT` для всех четырёх типов
успешных delivery: digest, snooze resurfacing, proactive Attention и motivation.
Event хранит bounded snapshot отправки, а не source content. Delivery сохраняет
текущую PM-08 at-least-once семантику при сбое между Telegram и SQLite.

Proactive reminder позволяет Done, Later, Not now и Fewer like this через More;
generic nudge открывает три материала Attention и сохраняет Fewer like this.
Reminder Done/Snooze фиксируют дополнительный
outcome в транзакции с canonical lifecycle event; normal Item Done/Snooze не
приписываются задним числом к напоминанию. Не наблюдаемый Telegram URL-click не
создаёт `REMINDER_OPENED`; этот Event означает только наблюдаемый bot-mediated
source action.

`REMINDER_DISMISSED` добавляет 24-часовой scheduler cooldown, не меняя Item
state или attention score; действует более длинный PM-08 same-Item cooldown.
Item-specific `REMINDER_DISLIKED` даёт ограниченную, не накапливающуюся
категорийную/типовую поправку из send-time snapshot. Outcome history за
последние семь суток даёт user-level fatigue penalty от 0 до -10; положительное
взаимодействие не повышает score. Dislike generic reminder подавляет только ту
же MotivationKind семь elapsed дней и не меняет настройки пользователя.

## 53. Done

Atomic transition → `DONE` + `completed_at` + event.

## 54. Archive

Atomic transition → `ARCHIVED` + `archived_at` + event.
Archived Item остаётся searchable.

## 55. Retry

FAILED → QUEUED. `READY/PARTIAL` с failed child source также можно вернуть в QUEUED:
повторно извлекаются только failed sources, READY checkpoints переиспользуются, после
чего общий analysis пересобирается. Error fields очищаются, processing checkpoints
сохраняются. Pending stale `ITEM_FAILED` delivery отменяется атомарно; новый READY
результат после partial-retry переоткрывает durable READY delivery.

## 56. Error handling

Пользователь получает короткое сообщение без stacktrace; техническая причина —
в structured logs и durable error fields. Item не исчезает.

## 57. Error codes

Основные коды: `UNSUPPORTED_SOURCE`, `DOWNLOAD_FAILED`, `TOO_LARGE`,
`EXTRACTION_FAILED`, `TRANSCRIPTION_FAILED`, `LLM_FAILED`,
`LLM_TIMEOUT`, `LLM_RATE_LIMITED`, `LLM_AUTH_FAILED`, `LLM_CONFIG_FAILED`,
`INVALID_LLM_OUTPUT`, `TIMEOUT`, `PROCESSING_TIMEOUT`,
`SECURITY_REJECTED`, `AUTH_REQUIRED`, `RATE_LIMITED`, `UNKNOWN`.

## 58. Retry policy

Transient external failures: bounded attempts + exponential backoff.
Permanent 4xx/security/unsupported/invalid input не retry-ятся.
Instagram `AUTH_REQUIRED` permits an explicit user Retry after the operator updates
the optional cookie file; workers do not automatically requeue failed Items.

## 59. LLM failure

LLM failure переводит Item в FAILED, не удаляя extraction/transcript/checkpoints.
Retry продолжает с максимально глубокого compatible durable checkpoint.
Текст невалидного ответа модели не включается в диагностическую ошибку.

## 60. Processing stages

`processing_stage` отражает глубину прогресса отдельно от status.
Resume обязан использовать persisted content/analysis вместо повторения дорогих calls.
`TOPIC_CLASSIFYING` означает, что профиль-aware analysis уже сохранён без
канонической категории; повторный запуск восстанавливает его и повторяет только
строгий source-only classifier. При отсутствии успешно извлечённого source
контента пользовательская заметка не используется как замена evidence: если
нет и forwarded source context, Item может завершить primary analysis без вызова
classifier и сохранить `category = NULL`.

## 61. Content deduplication

Telegram update replay dedup — по identity сообщения. URL нормализуются и
схлопываются внутри одного сообщения, но одинаковый URL в разных messages не
объединяет Items. Semantic duplicate detection отсутствует.

## 62. URL normalization

Удалять fragment и tracking-параметры, сохранять meaningful query.
Нормализованный URL участвует в message-local source identity.

## 63. Security Telegram

Только IDs из `ALLOWED_TELEGRAM_USER_IDS`. Неавторизованные updates молча игнорируются.

## 64. Secrets

Tokens/API keys только environment/.env; `.env` не коммитится и не логируется.

## 65. Subprocess security

yt-dlp вызывается Python API. ffmpeg запускается argv-list без shell interpolation.

## 66. Ограничения файлов

Defaults:

- Telegram voice/audio: 20 MB;
- web download: 5 MB;
- YouTube duration: 7200 s;
- YouTube audio/video: 50 MB;
- YouTube media download timeout: 300 s;
- Instagram Reel duration: 7200 s;
- Instagram Reel audio/video: 50 MB;
- subtitles: 2 MB.

Все значения задаются config и валидируются на startup.

## 67. Логи

Логировать IDs/source/stage/duration/result/error code, но не secrets и не полный
private content.

## 68. Performance

Background concurrency bounded. Один slow Item не должен блокировать Telegram handler.
No unbounded in-memory/media fan-out.

## 69. UX обработки длинного видео

Ingestion отвечает быстро. Длинная работа идёт в фоне; результат приходит после READY.
Если vision недоступен, UI должен честно обозначить transcript-only analysis.

## 70. Telegram Item presentation

По умолчанию результат показывает подтверждение сохранения, title и summary.
Существенное ограничение полноты анализа остаётся коротким предупреждением;
category, локализованный type, interest и next action доступны через `ℹ️ Детали`.
На экране деталей также может отображаться локализованный охват анализа; внутренние
значения completeness enum не показываются.
Числовой priority score и ranking reason в обычной Telegram-карточке не показываются.

Первичная inline-клавиатура отдаёт приоритет возврату к содержимому: кнопки
открытия и повторной отправки источника и `••• Ещё`. Жизненный цикл Item, уровень интереса,
явная обратная связь и Details находятся за дополнительным меню. Interest,
feedback, lifecycle Events, retry и durable video Delivery сохраняют прежние
application-service semantics. Навигация по меню — read-only projection и не
создаёт в SQLite состояние открытого меню.

URL source action показывается только для корректного HTTP(S) URL с hostname; на
кнопке отображается bounded destination label без query/path. Public forwarded
channel получает `↗ Оригинальный пост` только если Telegram дал public username и
original message id.

`↩️ Оригинал` воспроизводит через Bot API сообщение, отправленное пользователем
в AIInbox. Его identity — `Item.telegram_message_id` и
`User.telegram_chat_id`; private-chat URL не создаётся. Для пересланного
публичного Telegram-поста `↗ Оригинальный пост` остаётся отдельной кнопкой:
первая возвращает capture из чата с ботом, вторая открывает публичный источник.
Если capture удалён, Item сохраняется, а доступные persisted source actions
показываются как fallback.

Уведомления `READY` и `FAILED` отвечают на исходное Telegram-сообщение для любого
захвата с известным `telegram_message_id`, а не только для видео. Если Telegram
отклонил только reply target, доставка один раз повторяется без reply; другие
ошибки остаются в durable Delivery retry path.

Списки `/today`, `/inbox`, `/category <имя>`, `/search`, daily digest и конкретные
рекомендации `/weekly` показывают ограниченные полнострочные title-кнопки в
порядке текста; каждая открывает соответствующий owner-scoped `item:view`.
Нажатие присылает отдельную компактную карточку Item, сохраняя сообщение списка.
Каждая такая карточка, ручной Attention, snooze и proactive Reminder дают доступ
к оригинальному capture и/или безопасным сохранённым источникам. Ask показывает
source action и Original только для уже подтверждённых цитат; составной Item без
единственного source URL не выбирает произвольную ссылку.

READY YouTube/Instagram ItemSource получает отдельное действие отправки этого
источника в Telegram. Callback только ставит source-scoped intent в durable
outbox; DeliveryWorker ограничивает скачивание размером Bot API, отправляет
аудио вместе с видео, удаляет локальную временную копию и сохраняет Telegram
`file_id` для повторной отправки. MP4 уходит как video preview; другие контейнеры
сохраняются как document.

## 71. Персонализация

Текущая персонализация: profile factors + deterministic priority.
Events собираются, но не обучают модель автоматически.

## 72. Будущая персонализация

Behavior-aware ranking возможен только отдельной задачей после наличия реальных данных.

## 73. Testing strategy

Default suite offline: fake providers/local fixtures, без OpenAI/Telegram/YouTube network.
Concurrency, restart, security и persistence paths покрываются regression tests.

## 74. Integration tests

Проверять pipeline end-to-end с fake adapters, migrations и transactional semantics.

## 75. Extractor tests

Проверять limits, retries, SSRF, subtitle/STT fallback, ffmpeg command contracts и cleanup.

## 76. Quality gate

Перед merge required:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions job `quality` — required status check для `main`.

## 77. Code style

Прямолинейный typed Python, минимальные abstractions, без premature generalization
и скрытой инфраструктуры.

## 78. README

README содержит setup/run/Docker/config/quality обзор и ссылки на подробные docs.

## 79. .env.example

Все поддерживаемые runtime settings должны иметь понятный пример без secrets.

## 80. Docker

Production image:

- pinned Python 3.12 base;
- dependencies из `uv.lock` через frozen install;
- `ffmpeg`;
- non-root user;
- SQLite volume `/data`;
- temp media в `/tmp/aiinbox` tmpfs;
- без Chromium/Playwright dependencies.

## 81. Graceful shutdown

SIGTERM/SIGINT останавливает приём новой работы, даёт workers bounded drain,
затем отменяет оставшиеся tasks и закрывает Telegram/DB resources.
Infrastructure failure критического task приводит к process exit; restart
восстанавливает PROCESSING/SENDING/RUNNING durable state.

## 82. Operational health

`python -m app.ops status` показывает размер DB, QUEUED/PROCESSING/FAILED,
pending deliveries, configured provider/model, worker concurrency и Telegram
configuration без credentials. Docker healthcheck использует тот же локальный
DB/config boundary; unexpected critical worker exit завершает основной процесс,
после чего внешний supervisor выполняет restart.

## 83. SQLite backup / restore

Live SQLite backup создаётся через Online Backup API и после создания проходит
`PRAGMA integrity_check` + `PRAGMA foreign_key_check`. Backup generations
хранятся отдельно от live DB и ротируются bounded числом. Restore создаёт новый
DB-файл и тоже проверяет его; замена canonical database выполняется только после
остановки приложения.

## 84. Deployment smoke

`python -m app.ops smoke` — явная live-проверка configured analysis provider
(OpenAI или OpenRouter) и Telegram API. Default test suite остаётся полностью
offline; live smoke запускается оператором после deployment с production env.

## 85. Off-host backup verification

Каждое backup generation имеет SHA-256 sidecar. Production backup должен
реплицировать `.db` + `.sha256` в отдельный failure domain, скачать тот же
generation обратно и до объявления успеха выполнить checksum verification,
`PRAGMA integrity_check`, `PRAGMA foreign_key_check` и restore в отдельный
временный DB-файл. Off-host credentials принадлежат deployment host и не
передаются application runtime.

## 86. `/export` — portable ownership copy

`/export` и `/export compact` ставят durable `ExportJob` в очередь; `/export full`
добавляет первичные сохранённые тексты и транскрипты. Handler только валидирует
режим, фиксирует idempotent job по Telegram message identity и отправляет ACK.
`ExportWorker` создаёт versioned ZIP из user-scoped allowlist всех Item lifecycle
states, profile/effective settings, источников, sanitized Events и Reminder history.
Compact не содержит Content; full включает только `USER_TEXT`, `WEB_TEXT`,
`DOCUMENT_TEXT`, `TRANSCRIPT`, `VISUAL_NOTES` и `DESCRIPTION`.

`EXPORT_DIR` хранится отдельно от `BACKUP_DIR`, full export ограничивается по
суммарному числу Content-символов, а готовый архив — лимитом Telegram upload.
`DeliveryWorker` повторно отправляет тот же артефакт при transient failure и
удаляет его только после durable `SENT`; startup recovery возвращает прерванную
генерацию в очередь. Export — portable user data, а не backup или import. Он не
меняет Items, Contents, Events, Reminders, профиль, настройки или FTS; секреты,
transport IDs, worker jobs, outbox, callback receipts, checkpoints, derived
Content и Ask answers не экспортируются.

## 99. Основной критерий успеха продукта

После нескольких недель бессистемного сохранения материалов `/today` должен
выдавать небольшой, адекватный и персонально полезный список того, чем стоит
заняться сейчас.

## 100. Главная продуктовая идея

Строить не AI bookmark manager, а **Personal Attention Manager**: система сама
понимает входящий материал, оценивает его, предлагает действие и возвращает в
подходящий момент.

## 101. POLISH-05 — Telegram navigation and AI reliability

Telegram owns a bounded native `BotCommand` list and a compact inline main menu.
`set_my_commands` is best-effort presentation setup: it runs only when the bot
token is configured, requires no database access, and a temporary failure does
not stop workers or polling. The menu is additive; existing slash commands stay
supported and no persistent ReplyKeyboard is used.

Command and callback entry points share the existing user-scoped projections.
Navigation callbacks authorize from the human `callback.from_user`; the
bot-authored callback message supplies only the chat and presentation target.
Today/Attention exposure remains durable only after the corresponding Telegram
send succeeds. Category callbacks resolve short tokens against the current
owner-scoped category list and reject stale or colliding tokens.

Guided Ask/Search use only in-process one-shot input state. A normal slash
command clears a pending prompt, while forwarded content and media keep their
existing ingestion precedence. Ask stores the actual question message ID and
enqueues the existing durable AskJob; FTS retrieval and synthesis remain in
AskWorker. Search uses the existing SQLite FTS path. Neither flow stores chat
history. Export mode callbacks share the chooser message ID as the existing
ExportJob idempotency key and acknowledge the mode that was actually persisted.

OpenAI-compatible provider errors map to `LLM_TIMEOUT`, `LLM_RATE_LIMITED`,
`LLM_AUTH_FAILED`, `LLM_CONFIG_FAILED`, or `LLM_FAILED`. Authentication and
request-configuration failures are permanent for immediate retry; known timeout,
rate-limit, connection, and server failures are transient. Ask permits at most
two logical provider attempts with a 0.5-second delay for transient failures.
Structured-output retry and PM-13 citation repair remain separate bounded
mechanisms. `NO_RESULTS` and `INSUFFICIENT_CONTEXT` are successful DONE outcomes;
SQLite failures still reach fail-fast supervision. Ask answers remain transient
delivery payloads, and retrying Telegram delivery never reruns retrieval or LLM
synthesis.

Provider exception text and causes are excluded from logs and durable errors.
Safe logs carry operation, provider/model, exception type, status code, Ask job
and user identity, stage, attempt and latency; they do not contain questions,
evidence, answers or response bodies. `app.ops status` reports only bounded Ask
state and outstanding ASK_FAILED delivery counts.

## 102. POLISH-06 — Bounded Telegram lists and analysis-independent Item identity

Inbox and category browsing use bounded database pages. `page_size` limits one
Telegram response and SQL result; it does not limit how many Items the owner may
save or reach. Page callbacks are validated and clamped to the last currently
available page before their offset is used. Category choices are pageable as
well, including the existing feedback category chooser.

Each list Item is represented by one full-width title action which reuses the
existing owner-scoped `item:view:<id>` callback. Search remains bounded and
FTS-ranked; Today keeps its existing small actionable selection. Today exposure
is still recorded only after Telegram accepts the send, digest delivery claims
remain durable, and Weekly Review creates no Events or Reminder writes.

The analyzed `Item.title` stays canonical. When it is absent, Telegram
presentation derives a deterministic title from the Item's persisted source
metadata, reusing the same public-URL validation used by source buttons. A
`SECURITY_REJECTED` or local URL yields the generic `Ссылка` label. Document
filenames are taken only from the persisted safe filename metadata. The
projection does not fetch, call an LLM, mutate an Item, or alter export fields;
list pages batch-load their ItemSources.

FAILED cards use localized explanations and source-derived titles, while
technical error codes, durable retry policy, Original/source access and Item
state remain unchanged. Main-menu Export queues COMPACT directly; Help and
`/export full` keep explicit FULL choice. Profile exposes one-shot guided input
over the existing durable `ProfileUpdateJob`; a command clears that prompt
before normal command dispatch. POLISH-06 adds no migration or dependency.

## 103. POLISH-07 — Interaction consistency and Attention diagnostics

No-argument `/ask`, `/search`, and `/profile_update` open one-shot guided input;
explicit arguments remain direct. Guided text continues through the existing
Ask/Search/ProfileUpdate paths and does not fall through to Item ingestion.
No-argument `/export` and the menu action use the same Compact/Full chooser;
explicit modes remain direct. No-argument `/attention` and the menu action use
the same count chooser, while explicit counts remain direct.

Settings expose one-shot, validated editing for timezone, digest time, and quiet
hours. The settings projection shows current local time from the persisted IANA
timezone. Attention status is a read-only projection of the worker's gate,
candidate qualification, and arbitration rules. It reports local time, quiet
hours, intensity, caps, latest/next delivery context, eligible candidates, and
blockers without creating Reminders, Events, claims, or Item changes.

Search remains SQLite FTS5 lexical retrieval. Empty results explain vocabulary
mismatch such as translation or transliteration; Ask states that its answer is
grounded in found saved material and does not claim semantic retrieval. The
confirmed `Андроид` → `Android` miss is recorded as PM-14 evidence, while
embeddings and semantic runtime remain on hold.

Analysis category is finalized by a dedicated structured topic classifier that
receives saved source content and category hints, never `UserProfile` or
`user_note`. Its result is authoritative, and classifier failure follows the
normal analysis error path without falling back to the profile-aware analysis
category.

Attention intensity changes candidate eligibility thresholds to 60/60/60/55/50
for Calm/Light/Normal/Active/Aggressive without changing AttentionRank scoring.
Generic motivational reminders retain their caps and minimum gap; a second
generic reminder may compete after four hours without requiring an intervening
proactive reminder. Generic copy remains fact-based and LLM-free, and its
`🎯 Показать` action opens three Attention items. Reminder polling uses the
independent `REMINDER_POLL_SECONDS` setting, defaulting to 30 seconds. No schema
migration or dependency is introduced.

## 104. POLISH-08 — Human-facing reminders and Russian object-centric UX

Ordinary Telegram copy uses Russian product vocabulary: сохранение/материал,
`✅ Сделано`, `⏰ Отложить`, `🗄 В архив`, `🔄 Повторить`, `📥 Сохранённое`,
`🧠 Спросить`, `✨ Внимание`, `Ежедневная подборка`, and `Дополнительные напоминания`.
Internal Python/SQLite identifiers and event types remain unchanged.

`/today` and scheduled daily share one formatter. Their messages contain concrete
titles and, when available, a saved next action or bounded summary; a known duration
may be shown. They do not show ordinal positions, priority scores, or aggregate
counts. Weekly presentation uses the existing read-only `WeeklyReview` and exposes
only up to three existing `WeeklyRecommendation` rows; an empty recommendation set
does not fall back to weekly flow/backlog/reminder statistics. Manual Attention
cards show title plus persisted summary without `index/count` metadata. Ordinary
Inbox, category, and search lists do not show priority scores, category counts, or
page totals.

Every new `MOTIVATION_NUDGE` candidate has a concrete `focus_item_id` selected in
existing PM-07 order. The id is stored in the existing Reminder payload while the
Reminder retains its user-level `item_id=NULL` claim identity; Reminder Events
resolve and record the focused Item. The message projects the saved title and
persisted summary, and uses the same source/original and lifecycle controls as an
Item-specific reminder. No eligible concrete save means no send. Historical
`MOTIVATION_NUDGE` rows with no focus remain valid; no migration or new dependency
is required. Motivation remains deterministic and LLM-free.

Normal settings show translated names and intensity labels without scheduler budgets.
The explicit read-only `📊 Статус` screen remains the sole surface for operational
counts and delivery blockers. Daily/weekly/Attention presentation, source identity,
Reminder claim/finalization, PM-11 feedback, TodayService and ranking formula remain
unchanged beyond the described copy/projection behavior.
