# POLISH-03 — Unified Provenance & Original Access

Type: Stabilization / Navigation & provenance specification  
Depends on: POLISH-02 preferred  
Blocks: POLISH-04 final reminder UX  
Status: IN_REVIEW

## 1. Problem

AIInbox preserves source metadata internally, but user-facing resurfacing is inconsistent:

- READY cards may expose source URLs/media actions;
- Ask references show titles and generic numbered open buttons;
- daily digest is largely title-only;
- weekly recommendations are mostly title-only;
- reminders do not provide one consistent “original capture” action;
- generic `Open` labels do not explain destination;
- the user cannot reliably return to the Telegram message that originally created an Item.

## 2. Goal

Establish one product invariant:

> Every user-facing Item reference should provide a clear path back to the original saved material whenever that path is technically available.

Applies to:

- READY result;
- Inbox/category/search drill-down;
- Ask references;
- Today;
- Attention;
- daily digest;
- proactive reminder;
- weekly recommendations.

## 3. Telegram original identity

For Telegram-origin Items, current persisted Telegram message identity and owner chat identify the original capture message.

Do not fabricate a public `t.me/...` link for a private bot chat.

Introduce an application-level `Original` action instead.

## 4. Original action

Recommended Item callback:

~~~text
item:original:<item_id>
~~~

Reminder-aware equivalent where attribution matters:

~~~text
reminder:original:<reminder_id>
~~~

On tap:

1. verify Telegram user owns the target;
2. load the persisted original message/chat identity;
3. use Bot API copy/forward semantics to reproduce the original message in the current bot chat;
4. if original message is no longer available, fall back to persisted source actions where possible;
5. otherwise return a concise controlled error.

Never expose another user's message identity.

## 5. Product abstraction

The product label is:

~~~text
↩️ Оригинал
~~~

It means “bring back the message I originally saved”, not necessarily “open a public URL”.

This avoids pretending private Telegram bot messages have a universal navigable public link.

## 6. READY reply anchoring

Where the existing durable Delivery architecture can support it safely, READY analysis should reply to the original capture message.

This visually binds:

~~~text
original capture
↳ analysis result
~~~

If the original message was deleted or Telegram rejects reply anchoring:

- fall back to a normal delivery;
- do not fail Item processing solely because reply anchoring failed.

No SQLite transaction may remain open during Telegram I/O.

## 7. Shared source projection

Create one reusable source-reference projection used across Telegram presentation surfaces.

Conceptually:

~~~text
ItemReference
- item_id
- title
- original_available
- source_actions[]
~~~

Each source action is based only on persisted trusted metadata:

~~~text
source_id nullable
source_type
source_url nullable
label
can_resend_media
~~~

Do not let each formatter independently guess which URL/source represents an Item.

## 8. Shared source labels

Examples:

- YouTube → `↗ YouTube`;
- Instagram → `↗ Instagram Reel`;
- WEB → `↗ Статья — host` or `↗ Сайт — host`;
- forwarded public Telegram post → `↗ Оригинальный пост`;
- Telegram capture → `↩️ Оригинал`;
- document/video with no public URL → Original or resend action, no fake URL.

## 9. URL safety

Direct URL buttons are created only from persisted, validated HTTP(S) URLs.

No LLM/provider-generated URL may become a button.

No arbitrary URL scheme.

## 10. Ask My Inbox

PM-13 citation validation remains unchanged.

Improve Ask presentation from generic:

~~~text
[1] Открыть
~~~

to destination-aware actions such as:

~~~text
[1] ↗ YouTube
[2] ↩️ Оригинал
~~~

Reference display title still comes from validated persisted Item data.

For Item-level citations (`source_id=NULL`), only resolve unambiguous trusted actions.

Do not arbitrarily choose one source URL for a composite Item.

## 11. Ask original access

If an accepted Ask reference identifies an Item whose cited source has no direct URL, allow Original action against that already validated Item.

Navigation must not widen the PM-13 citation namespace.

## 12. Daily digest

A digest must not remain a dead title list with no way to reach Items.

Preferred implementation:

~~~text
digest heading/summary
+
bounded compact Item cards or actionable rows
+
source/original actions
~~~

The exact rendering may be chosen to keep Telegram output compact.

Preserve current daily candidate limits and ranking semantics.

Exposure Events must still correspond to successfully delivered user-visible Items.

## 13. Today

`/today` should use the same source/original projection.

Each returned Item must be actionable, not just numbered text.

Do not change Today ranking in this PR.

## 14. Weekly Review

Weekly aggregate facts remain one compact report.

Only concrete Item recommendations need navigation.

Recommended:

- report text;
- up to three recommendation actions/cards linked to Item/source/original.

Do not attach an Item URL to aggregate category statistics.

## 15. Inbox/category/search

Do not convert up to 20-item result lists into 20 huge cards.

Use a compact drill-down model:

~~~text
list
→ choose Item
→ compact Item card
→ original/source actions
~~~

Choose the smallest consistent Telegram interaction.

## 16. Reminder attribution

A bot-mediated Original callback is observable.

For an Item-specific reminder it may create `REMINDER_OPENED` under existing PM-11 semantics.

Direct URL button clicks remain unobservable and must not create fake `REMINDER_OPENED`.

Keep this distinction explicit in code/tests/docs.

## 17. Media resend

Existing YouTube/Instagram resend behavior remains source-specific.

Reuse it in the shared source projection rather than building another media path.

## 18. Forwarded provenance

Existing public forward-origin URLs remain a separate action:

~~~text
↗ Оригинальный пост
~~~

This differs from:

~~~text
↩️ Оригинал
~~~

which refers to the message the user sent/forwarded into the AIInbox bot chat.

## 19. Deleted original

If Bot API cannot copy/forward the original:

- answer `Оригинальное сообщение больше недоступно`;
- preserve Item provenance;
- offer persisted direct source/resend actions if available;
- do not mutate Item state.

## 20. No fabricated Telegram URL storage

Do not persist a synthesized private-chat URL.

Current IDs are enough for bot-mediated retrieval.

## 21. Delivery architecture

If READY reply anchoring needs additional delivery metadata, extend the existing durable Delivery projection.

Do not bypass DeliveryWorker.

Processing workers must not send READY Telegram messages directly.

## 22. Tests

Cover:

- Original callback owner isolation;
- correct copy/forward Bot API call;
- deleted-original fallback;
- no fabricated private message URL;
- forwarded public-original URL remains distinct;
- Ask source labels describe destination;
- composite Item-level Ask citation does not pick arbitrary URL;
- Today/digest Items expose source navigation;
- weekly recommendation navigation;
- reminder Original callback creates `REMINDER_OPENED` exactly once where appropriate;
- direct URL still creates no opened Event;
- READY reply anchoring falls back safely if implemented.

## 23. Acceptance criteria

1. Principal Item resurfacing surfaces expose original/source access when available.
2. Private Telegram captures use a bot-mediated Original action instead of fake URL.
3. READY result is anchored to original capture where safely possible.
4. Ask actions identify their destination.
5. Daily digest is no longer a title-only dead end.
6. Weekly concrete recommendations are navigable.
7. Composite Items never guess one arbitrary source.
8. Direct source URLs remain validated persisted HTTP(S) metadata.
9. Reminder-open attribution remains observable-only.
10. Full pytest/ruff gate passes.

## 24. Definition of Done

POLISH-03 is complete when an Item is not a dead title: wherever AIInbox resurfaces it, the user can reach the saved source or reproduce the original Telegram capture with one clear action.
