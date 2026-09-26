# Current Focus — HTTP API, Browser Capture, and External Sync

Status: active sequencing decision  
Scope: near-term implementation direction after the Telegram/Attention core.

## 1. Current priority

The near-term product focus is:

~~~text
PM-18 HTTP API
    ↓
PM-33 Browser Extension
    ↓
Integration Sync Foundation
    ↓
Miro one-way sync
    ↓
additional outbound destinations
~~~

PM numbers are roadmap identifiers, not strict execution order.

The existing Telegram client remains supported throughout.

## 2. Why this direction

The current core already captures, understands, ranks, resurfaces, searches and
answers over saved material. The next leverage point is not another ranking layer;
it is making AIInbox easy to use from more surfaces and easy to project into the
tools where the user already works.

This direction has three goals:

1. remove Telegram as the only practical capture/interface surface;
2. make browser capture nearly frictionless;
3. let AIInbox remain canonical while saved knowledge is mirrored into external
   workspaces such as Miro.

## 3. Phase A — PM-18 HTTP API

PM-18 becomes the immediate implementation foundation.

Minimum vertical slice required before browser/integration work:

- authenticated `/v1`;
- text/URL capture;
- Item list/detail;
- processing status;
- lifecycle actions;
- search;
- Ask job submission/result;
- stable OpenAPI;
- client idempotency keys.

The API must remain a thin adapter over existing application services.

Do not put Telegram-specific logic into the API and do not move canonical business
rules into route handlers.

Detailed existing spec:
[PM-18_HTTP_API.md](PM-18_HTTP_API.md).

## 4. Phase B — PM-33 Browser Extension

Implement the browser extension immediately after the minimal capture/auth API
contract is stable.

### V1 capture surface

The extension should support:

- Save current page;
- Save selected text + page URL;
- optional short user note;
- visible queued/saved/error state;
- idempotent retry;
- keyboard shortcut/context-menu capture where browser APIs permit.

### V1 non-goals

Do not initially build:

- full Inbox browsing inside the extension;
- Ask chat UI;
- local LLM execution;
- background crawling;
- arbitrary page automation.

The extension is primarily a low-friction capture client.

### Security

- API credential must not be exposed to page JavaScript;
- content scripts receive the minimum needed privileges;
- host permissions should be bounded;
- captured page content remains untrusted input;
- duplicate extension retries must not create duplicate Items.

## 5. Phase C — Integration Sync Foundation

External platforms are projections, not canonical storage.

~~~text
AIInbox Item
   ↓
durable sync intent
   ↓
destination adapter
   ↓
Miro / future platform
~~~

AIInbox remains the source of truth for:

- Item identity;
- source provenance;
- analysis;
- lifecycle;
- interest;
- priority/attention;
- feedback history.

Remote objects are presentation/integration projections.

## 6. Sync direction

V1 is intentionally one-way:

~~~text
AIInbox → external platform
~~~

Changes made in Miro or another target do not silently mutate the canonical
AIInbox Item.

Bidirectional synchronization is a later explicit phase because it requires
conflict semantics and provider-specific mapping rules.

## 7. What "sync all saves" means

Every canonical Item should be eligible for projection, regardless of source type.

Initial projection can be created from durable capture data and enriched after
processing completes.

At minimum the external representation can contain:

- AIInbox Item id;
- title or safe source fallback;
- summary when available;
- category;
- Item type;
- lifecycle state;
- interest level;
- semantic priority;
- source URL(s) where safe;
- saved/created timestamp;
- link back to AIInbox once a web/client URL exists.

Do not copy unlimited transcript/document body by default.

## 8. Durable synchronization

External synchronization is network work and must survive restart.

Use a durable job/outbox-style path rather than synchronous writes in ingestion
or Telegram/API handlers.

Conceptually:

~~~text
Item created/updated
    ↓
durable sync intent
    ↓
sync worker
    ↓
remote upsert
    ↓
persist remote mapping + result
~~~

Important requirements:

- bounded retries;
- explicit permanent vs transient errors;
- idempotent remote upsert;
- no long SQLite write transaction during provider network I/O;
- restart recovery;
- per-user/per-connection scoping.

## 9. Provider-neutral boundary

Because Miro is explicitly the first of multiple destinations, a small connector
contract is justified.

Keep it narrow, e.g. conceptually:

~~~text
SyncDestination
  upsert_item(...)
  archive_item(...)   # only if the provider representation needs it
  health/check auth
~~~

Do not introduce a universal workflow/integration framework.

Provider-specific object mapping stays in the adapter.

## 10. Miro first connector

Miro is the first concrete sync target.

V1 should optimize for a readable personal knowledge board rather than expose
every internal field.

Recommended representation:

- one card/shape per AIInbox Item;
- stable remote object mapping;
- title as primary text;
- concise summary/category/type metadata;
- source link where available;
- deterministic placement strategy;
- optional grouping by category or board area after the base sync works.

### Miro V1 requirements

- configure one target board;
- initial full sync of existing Items;
- incremental sync for new/changed Items;
- idempotent rerun;
- restart-safe continuation;
- controlled auth/rate-limit failures;
- remote object id stored as integration projection metadata.

### Miro V1 non-goals

- arbitrary board layout AI;
- two-way Item editing;
- deleting AIInbox Items because a Miro card was deleted;
- syncing full transcripts;
- comments/mentions;
- collaborative conflict resolution.

## 11. Initial full sync

The connector must support bootstrap:

~~~text
existing AIInbox Items
→ bounded batches
→ remote upsert
→ persisted cursor/mapping
→ resume after restart
~~~

Do not enqueue the whole database as one unbounded transaction.

## 12. Incremental sync

After bootstrap, sync only Items whose projected representation changed.

The implementation may use an Item projection hash/version rather than adding
complex event sourcing.

Exact persistence is implementation-specific, but it must make "already synced"
and "needs update" durable and testable.

## 13. Candidate future destinations

After Miro, evaluate destinations based on actual user workflow.

Likely categories:

- Notion databases/pages;
- Google Drive/Docs/Sheets;
- Obsidian/Markdown export folder;
- generic webhook/JSON endpoint;
- other knowledge/project tools with stable APIs.

Do not implement all connectors simultaneously.

Miro should prove the connector boundary first.

## 14. API vs connector responsibilities

The HTTP API is for clients and explicit external consumers.

Server-side connectors should call application/read-model services directly where
they run inside the same modular monolith. They do not need to call the local HTTP
API just to reach AIInbox data.

This prevents unnecessary self-HTTP and duplicate authorization layers.

## 15. Authentication and secrets

External connector credentials:

- come from operator secret/config storage;
- never live in ordinary user-facing settings JSON;
- never appear in export;
- never appear in logs;
- are not passed to LLMs.

A future multi-user system may replace this with OAuth connection records, but the
personal deployment does not need a SaaS auth platform now.

## 16. Failure semantics

One broken destination must not block:

- ingestion;
- analysis;
- Telegram;
- HTTP API;
- other connectors.

Integration failures are isolated and recoverable.

## 17. Observability

Useful sync logs/metrics:

- connection/destination;
- item_id;
- remote_object_id where safe;
- attempt/result;
- duration;
- error class;
- bootstrap cursor/progress.

Do not log full private Item content.

## 18. Testing strategy

Use deterministic fake destinations.

Required behavior:

- full bootstrap sync;
- incremental update;
- duplicate job/idempotent upsert;
- provider timeout/retry;
- permanent auth failure;
- restart recovery;
- another user's mapping isolation;
- Item lifecycle update projection;
- source URL safety;
- connector failure does not block core processing.

Live Miro verification is valuable but must not be required by the default suite.

## 19. Sequencing

Current intended sequence:

~~~text
1. close/review the current POLISH-08 work
2. implement the minimum complete PM-18 API vertical slice
3. implement PM-33 browser capture extension
4. implement durable Integration Sync Foundation
5. ship Miro connector + full bootstrap sync
6. use it in real workflow
7. select the next destination based on actual need
~~~

POLISH-08 review may complete in parallel with API design/implementation. The old
"all PM-16+ are blocked by polish" sequencing rule no longer blocks PM-18 because
the current explicit product priority is API/integration expansion.

PM-14 semantic retrieval and PM-16/17 provider-routing work remain independently
gated until explicitly resumed.

## 20. Success criteria

This direction succeeds when:

- a browser page can be saved without opening Telegram;
- duplicate browser retries do not create duplicate Items;
- AIInbox remains canonical;
- all existing saved Items can be bootstrapped into a Miro board;
- new/changed Items propagate incrementally;
- Miro failures do not break capture or processing;
- adding a second destination does not require changing Item business logic;
- external projections do not become hidden sources of truth.
