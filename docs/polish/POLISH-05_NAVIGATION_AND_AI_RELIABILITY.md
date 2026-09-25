# POLISH-05 — Telegram Navigation & AI Reliability

Type: Stabilization / Operability specification  
Depends on: POLISH-01…04 preferred  
Blocks: resuming PM-14+ product expansion  
Status: IN_REVIEW

## 1. Problem

Two problems remain after presentation cleanup:

1. users must remember slash commands such as /profile, /settings, /weekly, /ask and /export;
2. AI-backed operations, especially /ask, can fail after ACK with only a generic user-visible error and insufficiently specific operator diagnosis.

The product needs discoverable navigation and stronger AI failure handling before more AI features are added.

## 2. Goal

- expose main bot surfaces through Telegram-native command discovery and a compact menu;
- keep slash commands as stable power-user aliases;
- make AI failures internally classifiable and retryable where safe;
- improve diagnostics without logging private questions/source content;
- explicitly review polish quality before resuming PM-14+.

## 3. Native BotCommand menu

Register Telegram commands from code.

Minimum visible commands:

~~~text
/start
/menu
/today
/attention
/inbox
/search
/ask
/weekly
/category
/profile
/settings
/export
/help
~~~

Use concise Russian descriptions.

Do not rely only on manually configured BotFather state.

## 4. Command registration

Add an idempotent startup helper conceptually:

~~~text
configure_bot_commands(bot)
~~~

Requirements:

- only when Telegram bot is enabled;
- static bounded definitions;
- no SQLite dependency;
- no secrets in descriptions/logs;
- temporary Telegram failure follows a documented startup/warning policy.

## 5. Compact main menu

Add /menu or equivalent inline navigation reachable from /start and /help.

Recommended buttons:

- 🎯 Сегодня;
- ✨ Внимание;
- 📥 Inbox;
- 🔎 Поиск;
- 🧠 Ask;
- 📊 Неделя;
- 🏷 Категории;
- 👤 Профиль;
- ⚙️ Настройки;
- 📦 Экспорт.

Do not add a permanently visible giant ReplyKeyboard that consumes the chat screen.

## 6. Ask menu UX

A menu button cannot contain arbitrary question text.

On 🧠 Ask, use the smallest robust Telegram-native one-shot input flow:

~~~text
bot: Задай вопрос по сохранённым материалам.
user: <question>
→ existing durable AskJob
~~~

Requirements:

- one question only;
- cancel action available;
- slash commands still work;
- no general multi-turn conversation memory;
- handler remains thin;
- actual Ask work remains in AskWorker.

If aiogram FSM is disproportionate, use ForceReply or another smaller one-shot mechanism.

## 7. Search/category guided input

The menu may similarly guide the user into search/category input without memorizing syntax.

Existing slash commands remain valid.

Avoid persistent conversational state for basic navigation.

## 8. Settings navigation

⚙️ Настройки should lead to existing digest/quiet-hours/Attention controls, including the current Attention submenu.

No notification policy change in this PR.

## 9. Help redesign

/help should become short and task-oriented rather than a long command wall.

Recommended structure:

~~~text
Просто отправь
- текст, ссылку, видео, документ, forward

Найти
- Поиск
- Ask

Вернуться
- Сегодня
- Внимание
- Неделя

Настроить
- Профиль
- Настройки
- Экспорт
~~~

Include a ☰ Меню action.

## 10. Ask reliability audit

The observed user flow shows repeated:

~~~text
/ask ...
→ Ищу в сохранённых материалах…
→ Не удалось подготовить ответ...
~~~

Do not guess the production root cause.

Audit actual boundaries:

- provider configuration;
- timeout/network;
- rate limit;
- authentication failure;
- invalid structured output;
- FTS/search failure;
- context construction failure;
- citation validation exhaustion;
- database infrastructure error;
- Delivery transport failure.

## 11. Internal error taxonomy

After code inspection, distinguish at least the concepts that actually apply:

~~~text
NO_RESULTS
INSUFFICIENT_CONTEXT
SEARCH_FAILED
LLM_TIMEOUT
LLM_RATE_LIMITED
LLM_AUTH_FAILED
LLM_FAILED
INVALID_LLM_OUTPUT
INVALID_CITATIONS
DELIVERY_FAILED
~~~

Reuse current LlmError / AskJob fields where possible.

Do not create duplicate error concepts.

## 12. Provider exception mapping

OpenAI/OpenRouter adapter should map known SDK/HTTP failures into bounded application errors.

At minimum distinguish:

- timeout/network;
- rate limit;
- authentication/configuration;
- invalid structured output;
- other provider failure.

Never log provider response bodies when they can contain user content.

## 13. Bounded retry

For transient provider failures:

- retry only in worker/provider layer, never Telegram handler;
- use a small bounded number of attempts;
- use bounded delay;
- do not retry authentication/configuration failures;
- keep PM-13 citation retry separate;
- keep Telegram Delivery retry separate from LLM retry.

No unbounded retry loop.

## 14. Ask durability remains

Preserve:

~~~text
PENDING
→ RUNNING
→ DONE / FAILED
~~~

Startup recovery remains authoritative.

Do not create another queue system for retry.

## 15. User-visible failure copy

Differentiate useful product outcomes without exposing internals.

Examples:

### No matching material

Не нашёл достаточно подходящих сохранённых материалов.

### Grounding insufficient

В найденных материалах недостаточно данных для уверенного ответа.

### Temporary AI problem

Не смог подготовить ответ из-за временной ошибки ИИ. Попробуй ещё раз.

Do not expose HTTP codes, provider traces, raw responses or database internals.

## 16. Operator diagnostics

Safe logs should include:

- ask_job_id;
- user_id;
- provider/model;
- processing stage;
- error code;
- attempt number;
- latency;
- retrieved Item count/context size where already safe.

Never log:

- full question;
- source excerpts;
- generated answer;
- API keys/auth headers.

## 17. Durable AskJob error fields

Review current AskJob error_code/error_message behavior.

Persist bounded technical classification useful for diagnosis.

Do not persist raw provider response, private prompt or context.

## 18. Recent failure inspection

No external telemetry platform.

A simple operator query or current status/ops surface over AskJob states/error codes is enough.

If a safe current status surface exists, extend it minimally. Otherwise document SQL/operator commands in RUNBOOK.

## 19. Other AI operations

Review error-mapping consistency for:

- final analysis;
- chunk summary;
- profile update;
- attention hook generation;
- vision.

This PR need not add retry everywhere. The goal is to avoid opaque SDK exceptions and classify known boundaries consistently.

## 20. Ingestion failure UX

When extraction succeeded but analysis failed:

- existing Retry remains;
- user-facing failure stays concise;
- permanent vs retryable follows actual application knowledge;
- raw provider errors stay hidden.

## 21. Command compatibility

All existing slash commands continue to work.

Menu/navigation is additive discoverability, not a breaking replacement.

## 22. Authorization

All menu/input flows follow current allowlist semantics.

Unauthorized users must not receive private menus, Ask state or Item information.

## 23. No general chat state

Menu-guided Ask remains:

~~~text
one user question
→ one AskJob
→ one answer
~~~

Do not create assistant history/session memory.

## 24. Tests — navigation

Cover:

- BotCommand registration list;
- /menu top-level buttons;
- settings reachable without typed /settings attention;
- Ask menu starts one-shot question input;
- cancel exits;
- slash command interaction leaves no broken pending state;
- unauthorized user gets no private flow;
- help remains bounded.

## 25. Tests — Ask failures

Use fakes for:

- timeout → transient code + bounded retry;
- rate limit → transient code + bounded retry;
- auth failure → no pointless retry;
- malformed output → structured-output failure;
- invalid citations → existing PM-13 bounded retry;
- search failure → controlled Ask failure;
- DB infrastructure exception → critical supervisor;
- Delivery failure after durable answer → no second LLM synthesis.

## 26. Tests — privacy

Capture logs with unique private markers in question/source/generated answer and assert they are absent from error/diagnostic logs.

## 27. RUNBOOK

Add troubleshooting matrix:

~~~text
symptom | AskJob state | error_code | likely layer | operator action
~~~

Examples:

- repeated LLM_AUTH_FAILED → credentials/provider configuration;
- LLM_RATE_LIMITED → provider quota/rate policy;
- INVALID_LLM_OUTPUT → model/schema compatibility;
- SEARCH_FAILED → FTS/SQLite diagnostics;
- Delivery stuck → Telegram/outbox diagnostics.

## 28. Resume gate for PM-14+

After POLISH-05 explicitly review:

- outcome/category fixture quality;
- default Item UI density;
- original/source accessibility;
- hook/reminder quality;
- real Ask success/failure behavior.

Only after that review remove ON HOLD from PM-14+.

Do not start PM-14 in this PR.

## 29. Acceptance criteria

1. Telegram native commands are registered from code.
2. Core features are reachable from a compact inline menu.
3. Slash commands remain supported.
4. Ask can be initiated without memorizing /ask <text>.
5. No giant persistent ReplyKeyboard is introduced.
6. Help is shorter/task-oriented.
7. Ask failures are internally distinguishable.
8. Known transient provider failures have bounded retry.
9. Auth/config failures are not pointlessly retried.
10. DB infrastructure failures still reach critical supervision.
11. User-facing failures remain concise.
12. Logs contain no private question/source/answer content.
13. Delivery retry never recomputes an already durable Ask answer.
14. RUNBOOK provides concrete Ask troubleshooting.
15. PM-14+ remains paused until explicit quality review.
16. Full pytest/ruff gate passes.

## 30. Definition of Done

POLISH-05 is complete when users can discover AIInbox's main functions without memorizing slash commands and repeated AI failures can be diagnosed and retried safely without weakening durable job semantics or leaking private content.

## 31. Current implementation (IN_REVIEW)

- Code registers the bounded Russian BotCommand list at Telegram startup. A
  temporary `set_my_commands` failure logs only the operation and exception
  type and does not stop workers or polling; workers-only mode makes no Telegram
  setup call.
- `/start`, `/menu`, and `/help` expose the compact inline menu. Menu callbacks
  authorize with `callback.from_user`; command and callback views share their
  actor-explicit presentation path. Today/Attention exposure remains after the
  relevant Telegram send succeeds.
- Guided Ask/Search use in-memory one-shot FSM state. A user text command clears
  the pending prompt; forwarded text and media continue through ingestion.
  Guided Ask stores the user's question message ID in AskJob and remains enqueue
  only.
- The export chooser uses its Telegram message ID as the existing ExportJob
  idempotency key. If mode callbacks race, the first persisted mode is shown and
  both chooser buttons are removed.
- OpenAI-compatible calls map timeout, rate limit, authentication, request
  configuration, and generic provider failures to static LLM codes/messages.
  Provider request/response text is not retained or chained into logs. Ask
  retries known transient provider failures for at most two logical attempts
  with a 0.5-second delay; schema validation and citation repair remain separate
  bounded retries. The worst-case Ask synthesis is bounded to eight provider
  requests across the initial call, schema retry, citation repair, and
  transient retry.
- `NO_RESULTS` and `INSUFFICIENT_CONTEXT` remain successful DONE outcomes.
  SQLite failures remain fail-fast, and Telegram delivery does not recompute
  completed synthesis. PM-14+ stays ON HOLD until the separate quality review.
