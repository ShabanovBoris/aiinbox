# Integration Sync Foundation

Type: Post-MVP Architecture Epic + Detailed Technical Specification  
Prerequisite: stable canonical Item services; PM-18 already DONE  
Status: PLANNED

## 1. Problem

AIInbox is useful as the canonical personal attention system, but users also work
inside visual boards, documents, note systems and automation tools.

Copying data into those tools ad hoc would create provider-specific business
logic, synchronous network calls inside Item mutations, duplicate remote objects
after retries and no restart-safe way to bootstrap all existing saves.

The project needs one deliberately small outbound synchronization foundation
before adding Miro and later destinations.

## 2. Goal

Provide a durable one-way projection mechanism:

~~~text
canonical AIInbox Item
        ↓
deterministic projection
        ↓
durable sync state
        ↓
destination adapter
        ↓
external object
~~~

Initial direction:

~~~text
AIInbox → external destination
~~~

External systems are projections, not canonical storage.

## 3. Architectural invariants

The integration layer must preserve:

- SQLite as canonical state;
- single-process modular monolith;
- no network I/O while holding a SQLite write transaction;
- bounded retries;
- restart-safe claims;
- deterministic provider-neutral projection;
- user/connection scoping;
- no LLM in synchronization;
- no implicit remote-to-AIInbox mutation;
- no coupling of Item processing success to destination availability.

## 4. Server-side integration does not use self-HTTP

PM-18 exists for external clients.

A connector running inside the AIInbox process must call application services and
read projections directly.

Do not implement:

~~~text
IntegrationWorker
→ http://127.0.0.1:8080/v1/items
~~~

That would add unnecessary authentication, serialization and failure modes inside
one modular monolith.

## 5. V1 synchronization unit

The synchronization unit is:

~~~text
one Item × one IntegrationConnection
~~~

Each destination gets independent projection state.

A Miro failure must not affect a future Notion or webhook connection.

## 6. Suggested data model

Exact names may evolve during implementation, but preserve these concepts.

### IntegrationConnection

Represents one configured destination.

Conceptual fields:

~~~text
id
user_id
provider
enabled
target_json
status
last_error_code
created_at
updated_at
~~~

Example non-secret target metadata for Miro:

~~~json
{
  "board_id": "..."
}
~~~

Do not put OAuth access tokens or client secrets into target_json.

### IntegrationProjection

Maps one canonical Item to one remote object and owns durable synchronization
state.

Conceptual fields:

~~~text
id
connection_id
item_id
remote_object_id nullable
desired_hash
synced_hash nullable
status
attempts
next_attempt_at nullable
claimed_at nullable
claim_generation
last_error_code nullable
last_synced_at nullable
placement_json nullable
created_at
updated_at
~~~

Required uniqueness:

~~~text
UNIQUE(connection_id, item_id)
~~~

## 7. Projection row as durable queue

V1 does not require a separate generic IntegrationSyncJob table if the projection
row itself can represent:

~~~text
PENDING
SYNCING
SYNCED
FAILED
DISABLED
~~~

A projection is already the durable per-Item/per-destination identity.

Avoid creating both a job table and mapping table unless implementation evidence
shows the split is necessary.

## 8. Projection status model

Recommended meaning:

- PENDING — local projection differs from last confirmed remote state;
- SYNCING — claimed by worker;
- SYNCED — remote object represents current desired_hash;
- FAILED — permanent/exhausted error requiring intervention;
- DISABLED — connection disabled or projection not scheduled.

A transient failure normally returns to PENDING with next_attempt_at.

## 9. Claim generation

Because network work occurs outside a DB transaction, stale workers must not mark
newer desired state as synced.

Use claim_generation or an equivalent fencing token.

Flow:

~~~text
short transaction:
  PENDING → SYNCING
  increment claim_generation
  capture desired_hash + generation
commit

load detached projection DTO
close DB

provider network call

short transaction:
  finalize only if generation is still current
  and desired_hash is still the hash that was sent
~~~

If the Item changes while the network request is in flight, leave/requeue the
projection as PENDING.

## 10. Deterministic ItemSyncProjection

Build an immutable DTO from canonical data.

Suggested V1 fields:

~~~text
item_id
title
summary
category
item_type
state
processing_status
interest_level
priority_score
created_at
updated_at
safe_source_urls[]
~~~

Optional:

~~~text
next_action
estimated_action_minutes
~~~

Do not include:

- full Content.text;
- transcripts;
- document body;
- raw source_metadata_json;
- provider prompts;
- internal error_message;
- API idempotency keys;
- Telegram ids;
- Ask answers;
- profile;
- Event history.

## 11. Projection title fallback

Use the existing deterministic presentation-title semantics rather than creating
a connector-specific LLM title.

The analyzed Item.title remains canonical.

## 12. Safe URLs

Only project source URLs already considered safe for user-facing presentation.

Security-rejected URLs must not become external clickable links merely because a
connector exists.

## 13. Projection hash

Compute SHA-256 over canonical JSON of ItemSyncProjection using:

- sorted keys;
- UTF-8;
- deterministic datetime serialization;
- deterministic source order.

The hash answers only whether the remote representation needs an update.

It is derived state, not business identity.

## 14. desired_hash vs synced_hash

When canonical projection changes:

~~~text
desired_hash = new projection hash
~~~

If desired_hash differs from synced_hash, the projection is PENDING.

After confirmed remote upsert:

~~~text
synced_hash = sent hash
~~~

only if desired_hash has not changed.

This prevents lost updates without event sourcing.

## 15. Dirty marking

For low-latency synchronization, canonical mutation seams may call a small helper:

~~~text
mark_item_integrations_dirty(session, item_id)
~~~

inside the same business transaction.

Likely seams:

- new Item commit;
- processing READY;
- processing FAILED;
- lifecycle action;
- interest change;
- category/type/priority correction if represented remotely.

The helper never calls a provider.

## 16. Reconciliation guard

Do not rely solely on every future developer remembering to call dirty marking.

Add a bounded reconciliation pass that periodically recomputes projection hashes
for enabled connections and repairs missed/stale projection state.

Personal-scale SQLite makes this practical.

The reconciliation pass:

- runs in bounded pages;
- yields between batches;
- creates missing projection rows;
- changes only local derived integration state;
- performs no remote call in its scan transaction.

This is a correctness guard, not a second source of truth.

## 17. New Item semantics

All canonical Items are eligible, including:

~~~text
QUEUED
PROCESSING
READY
FAILED
ACTIVE
SNOOZED
DONE
ARCHIVED
~~~

This matches the requirement to synchronize all saves.

A destination may initially show Processing and later receive the analyzed
summary/category.

Do not wait for READY to create the first remote object if the destination is
configured to mirror all saves.

## 18. Bootstrap existing history

A new connection must be able to sync the entire existing Inbox.

Bootstrap:

~~~text
enabled connection
→ scan Items in deterministic bounded batches
→ ensure IntegrationProjection row
→ mark PENDING when not synced
→ worker upserts
~~~

Do not:

- load the entire archive in one transaction;
- enqueue unbounded in-memory tasks;
- hold a transaction while the remote API is called.

## 19. Bootstrap recovery

Persist enough local progress to resume after restart.

This may be connection bootstrap progress or simply durable projection rows
created page by page.

If projection rows themselves make bootstrap restart-safe, do not add another
cursor table without need.

## 20. Incremental synchronization

After bootstrap:

- dirty marking provides low latency;
- reconciliation catches missed changes;
- unchanged projection hashes generate zero remote calls.

This is the V1 incremental contract.

## 21. Destination adapter boundary

Keep the shared contract narrow.

Conceptually:

~~~text
SyncDestination
  validate_connection(...)
  upsert_item(projection, remote_object_id, placement)
  delete_or_archive_projection(...) only when provider behavior needs it
~~~

Do not create a universal SaaS object model.

## 22. Upsert semantics

Input:

- deterministic ItemSyncProjection;
- optional existing remote_object_id;
- provider-specific placement/config.

Output:

~~~text
remote_object_id
provider metadata needed for next update
~~~

The adapter should update the existing object whenever possible.

Retries must converge on one remote representation.

## 23. Remote idempotency

Providers may not support AIInbox idempotency keys natively.

Therefore local mapping is authoritative:

~~~text
(connection_id, item_id)
→ remote_object_id
~~~

If create succeeds remotely but the process dies before local persistence, the
adapter needs a recovery strategy.

Preferred options:

1. provider-supported stable AIInbox marker or metadata;
2. bounded lookup by a deterministic marker;
3. provider-specific reconciliation;
4. only as a documented last resort, duplicate repair tooling.

The Miro spec must choose an actual strategy.

## 24. Provider marker

Where supported, every remote projection should carry a stable non-secret marker:

~~~text
aiinbox:item:<item_id>
~~~

or a structured equivalent.

Do not expose:

- user id;
- API token;
- idempotency key;
- database path.

## 25. Remote deletion

V1 rule:

~~~text
remote deletion never deletes the AIInbox Item
~~~

If an update finds the mapped remote object missing:

- classify as REMOTE_MISSING;
- recreate only according to provider-specific documented behavior;
- update remote_object_id when recreated;
- keep canonical Item unchanged.

## 26. One-way means no conflict engine

V1 does not ingest remote edits.

Therefore it does not need:

- field-level conflict resolution;
- CRDTs;
- last-write-wins policy;
- remote webhook mutation handlers.

Provider-side edits may be overwritten by the next AIInbox projection update.

The UI/docs must state this.

## 27. Credentials

Connector credentials are secrets, not canonical user data.

Requirements:

- never put secrets into Item/Content/Event/Profile;
- never include them in PM-15 user export;
- never log access/refresh tokens;
- never send them to an LLM;
- keep provider client secrets out of settings_json.

## 28. Dynamic OAuth token persistence

OAuth refresh/access tokens cannot always live only in environment variables.

Introduce a deliberately small credential-store abstraction.

For the personal deployment, a reasonable V1 is a local persistent secret store
outside canonical SQLite/export data, with restrictive filesystem permissions and
atomic writes.

Operator-level OAuth client id/secret may remain in environment configuration.

A restore may require reconnecting a provider unless secret-store backup is
explicitly configured.

## 29. Connection disable

Disabling one IntegrationConnection:

- stops new claims;
- does not delete canonical Items;
- does not delete remote objects automatically;
- preserves local mapping so re-enable can resume.

## 30. Error classes

Normalize provider failures into a small set:

~~~text
AUTH_REQUIRED
RATE_LIMITED
TRANSIENT_PROVIDER_FAILURE
INVALID_REMOTE_TARGET
REMOTE_MISSING
PERMANENT_PROVIDER_REJECTION
~~~

Unknown programming errors should still fail the critical worker/process
according to repository supervision rules rather than being swallowed as
provider noise.

## 31. Retry policy

Retry automatically:

- transport timeout;
- 429/rate limiting;
- provider 5xx;
- known temporary errors.

Do not endlessly retry:

- invalid credentials;
- deleted/inaccessible target;
- invalid provider configuration;
- deterministic invalid payload.

Use bounded exponential backoff with jitter and provider retry/reset headers when
available.

## 32. Network timeout

Every provider request has an explicit timeout.

Do not inherit an unbounded SDK default.

## 33. Provider dependency policy

Before adding a provider SDK, compare it with the existing httpx dependency.

If REST calls are simple and the SDK adds dependency/async complexity, prefer
httpx.

Use an SDK only if it materially improves OAuth/request correctness and remains
well maintained.

## 34. Worker supervision

The integration worker is a critical background task when integrations are
enabled.

If it dies unexpectedly because of a programming/DB error, the process should
fail and rely on existing process supervision/restart recovery.

A remote provider outage is not a worker-crash condition.

## 35. Startup recovery

At startup:

~~~text
stale SYNCING
→ PENDING
~~~

or equivalent generation-safe recovery.

Do not leave abandoned claims after process death.

## 36. Core isolation

Integration errors must never prevent:

- HTTP capture;
- Telegram capture;
- Item extraction;
- LLM analysis;
- search;
- Ask;
- reminders;
- export.

Canonical commits succeed independently from provider availability.

## 37. Logging

Useful bounded fields:

- connection_id;
- provider;
- item_id;
- projection status;
- remote object id where non-sensitive;
- attempt;
- duration;
- controlled error class.

Never log:

- OAuth token;
- full title/summary;
- source content;
- provider response bodies by default.

## 38. Observability

At minimum expose operator logs/status for:

~~~text
bootstrap progress
pending count
synced count
failed count
rate-limited state
last successful sync
~~~

Do not add a metrics stack solely for V1.

## 39. Integration management surface

The server may expose protected /v1/integrations/... management endpoints for
external UI clients.

Server-side synchronization itself remains application-service driven.

Connection-management HTTP routes are adapters, not sync business logic.

## 40. Suggested application layout

Conceptually:

~~~text
app/integrations/base.py
app/integrations/credentials.py
app/integrations/projection.py
app/integrations/miro.py

app/services/integration_sync.py
app/workers/integration_sync.py
~~~

Do not split one connector into a microservice.

## 41. Tests — projection

Cover deterministic hash for:

- title/summary changes;
- lifecycle change;
- interest/priority change;
- source URL change;
- unchanged data.

No provider calls in projection tests.

## 42. Tests — durable state

Cover:

- missing mapping creates PENDING projection;
- successful upsert becomes SYNCED;
- changed desired hash during in-flight request remains PENDING;
- stale SYNCING recovered after restart;
- transient retry;
- permanent failure;
- connection disable.

## 43. Tests — bootstrap

Create enough Items for multiple batches.

Assert:

- all Items get projection state;
- bounded batches;
- restart resumes;
- no duplicates;
- all lifecycle states are included.

## 44. Tests — isolation

A provider timeout must not roll back:

- new Item;
- READY state;
- Done/Archive/Interest change.

Different connections fail independently.

## 45. Tests — fake destination

Default suite uses a deterministic fake adapter.

No live Miro call is required by default tests.

## 46. Migration expectations

Likely new tables:

~~~text
integration_connections
integration_projections
~~~

Do not add a third generic queue table without evidence.

One Alembic head only.

Fresh and upgrade migration tests are required.

## 47. Explicit non-goals

Not foundation V1:

- bidirectional sync;
- remote edits mutating AIInbox;
- arbitrary workflow automation;
- generic trigger/action builder;
- Kafka/Redis/Celery;
- provider webhooks;
- multi-user OAuth SaaS;
- semantic clustering;
- AI-generated board layout;
- syncing full transcripts by default.

## 48. Acceptance criteria

1. AIInbox remains canonical.
2. One Item × one connection has one durable projection identity.
3. Bootstrap covers all existing Items.
4. New Items become eligible incrementally.
5. Changed Items update existing projections.
6. Unchanged hashes cause no provider call.
7. Provider calls occur outside write transactions.
8. Claims survive/recover from restart.
9. Stale workers cannot mark newer data synced.
10. Provider outage does not block core application behavior.
11. Retries are bounded and classified.
12. Connection disable is safe.
13. Remote deletion never deletes AIInbox data.
14. Credentials never enter canonical Item data.
15. Credentials never enter export.
16. Default tests require no live provider.
17. A second destination can implement the same small adapter boundary.
18. No generic integration platform is introduced.

## 49. Definition of Done

~~~text
canonical Item mutation
        ↓
projection hash / dirty state
        ↓ commit
IntegrationProjection=PENDING
        ↓ claim
detached ItemSyncProjection
        ↓ close DB
destination.upsert(...)
        ↓
short finalize transaction
        ↓
SYNCED / PENDING / FAILED
~~~

The foundation is complete when Miro can be implemented without adding
provider-specific logic to Item, processing, lifecycle or API handlers.
