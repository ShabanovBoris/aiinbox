# PM-18 — HTTP API

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: stable application services after PM-13+; PM-16/17 optional  
Status: PLANNED

## 1. Problem

Telegram is an effective primary client but it limits richer clients, automation and direct integration. The business logic already lives mostly below Telegram; the next step is to expose that application surface through HTTP without reimplementing ranking, lifecycle, Ask or ingestion behavior.

## 2. Goal

Add a versioned authenticated FastAPI surface over existing application services.

Initial target:

~~~text
POST   /v1/items
GET    /v1/items
GET    /v1/items/{id}
GET    /v1/today
GET    /v1/attention
GET    /v1/search
POST   /v1/ask
GET    /v1/asks/{id}
POST   /v1/items/{id}/done
POST   /v1/items/{id}/snooze
POST   /v1/items/{id}/archive
PATCH  /v1/items/{id}/interest
GET    /v1/weekly
GET    /v1/settings
PATCH  /v1/settings
~~~

The API becomes the server contract used by PM-19 Android.

## 3. Architectural rule

FastAPI is an interface adapter.

~~~text
HTTP
→ request validation/auth
→ existing application service
→ response projection
~~~

Do not copy business rules from Telegram handlers into API endpoints.

## 4. In scope

- FastAPI/ASGI server;
- `/v1` versioned routes;
- bearer authentication for the personal deployment;
- user-scoped CRUD/read actions listed above;
- text/URL capture through canonical ingestion;
- lifecycle actions through existing action services;
- `/today`, `/attention`, `/weekly`, `/search` reuse;
- asynchronous Ask job API;
- idempotent client request keys for write operations;
- structured error model;
- OpenAPI schema;
- tests.

## 5. Out of scope

- public multi-tenant SaaS auth;
- OAuth login/social identity;
- admin panel;
- websocket streaming;
- file/media upload in v1;
- push notification service;
- browser frontend;
- GraphQL;
- server-side sessions;
- arbitrary SQL/query language.

## 6. Authentication

AIInbox remains a personal system in PM-18.

Use a single operator-provisioned bearer token mapped to one configured AIInbox user.

Conceptual config:

~~~text
HTTP_API_ENABLED=true
HTTP_API_TOKEN=<secret>
HTTP_API_USER_TELEGRAM_ID=<existing allowed user>
~~~

Exact mapping may use another stable user reference if current code evolves before implementation, but do not create a full account/auth subsystem solely for PM-18.

## 7. Token security

The token:

- comes only from environment/secret management;
- is never stored in normal user settings;
- is compared with constant-time semantics where practical;
- is never logged;
- is never returned by the API;
- must not appear in export artifacts.

Production documentation should require TLS at the reverse proxy/transport boundary.

## 8. API server runtime

Run FastAPI/uvicorn as another supervised task in the existing single-process modular monolith where practical.

It must share:

- the same `Settings`;
- session factory;
- application services;
- workers;
- graceful shutdown signal.

Do not introduce a second business-service process merely to expose HTTP.

## 9. Optional startup

If HTTP API is disabled, current Telegram/worker deployment must behave exactly as before.

If enabled with invalid auth configuration, fail fast at startup.

## 10. API versioning

All initial endpoints live under:

~~~text
/v1
~~~

Breaking response/schema changes require a future explicit version change or compatible additive evolution.

## 11. Common error envelope

Use a bounded structured error, conceptually:

~~~json
{
  "error": {
    "code": "ITEM_NOT_FOUND",
    "message": "Item not found"
  }
}
~~~

Do not expose stack traces, SQL errors, provider responses or secrets.

## 12. User isolation

Every endpoint operates only on the authenticated configured user.

A numeric Item id belonging to another user must behave as not found/forbidden according to one consistent documented policy.

Never accept `user_id` from the request body as authorization.

## 13. Idempotency

Write endpoints must support a client-generated idempotency key because Android/offline retries are expected.

Use an HTTP header such as:

~~~text
Idempotency-Key: <opaque client UUID>
~~~

The key is scoped to:

- authenticated user;
- operation/resource where applicable.

Repeated identical retries return the existing result instead of creating duplicate Items/actions.

## 14. Capture identity

Telegram identity currently uses `telegram_message_id`. HTTP capture needs its own durable request identity.

Add the smallest schema seam needed, for example:

~~~text
Item.external_idempotency_key nullable
UNIQUE(user_id, external_idempotency_key) WHERE non-null
~~~

or a small generic request-receipt table.

Do not overload Telegram message ids with synthetic values.

## 15. POST /v1/items

V1 supports text/URL capture only.

Request concept:

~~~json
{
  "text": "Read this https://example.com",
  "user_note": "optional explicit note"
}
~~~

Or a narrower shape consistent with existing ingestion service.

Requirements:

- one API request creates one Item;
- URLs route through the existing source parser;
- child ItemSources remain canonical;
- processing is durable/async;
- endpoint returns quickly with `202 Accepted` and Item identity/status.

No extraction/LLM in request handler.

## 16. Media upload

Not PM-18 v1.

PM-19 Share Sheet initially sends text/URL. Add file upload later with the same ingestion boundary once needed.

## 17. GET /v1/items

Provide bounded pagination/filtering.

Initial useful query parameters:

- state;
- processing_status;
- category;
- item_type;
- limit;
- cursor.

Avoid arbitrary sort expressions or SQL-like filters.

Default to a deterministic recent ordering.

## 18. Cursor pagination

Prefer stable cursor pagination over unbounded offset for growing histories.

A cursor can encode `(created_at, id)` in a signed/opaque representation.

If offset is chosen for simplicity at personal scale, hard-bound it and document the tradeoff.

## 19. GET /v1/items/{id}

Return a presentation DTO, not the ORM row.

Include:

- canonical Item fields;
- source metadata/provenance;
- lifecycle state;
- analysis fields;
- safe source URLs;
- optionally bounded source/content previews.

Do not dump unlimited transcript/document content by default.

## 20. GET /v1/today

Call existing TodayService.

Do not add PM-06/07 personal ranking to `/today` merely because the API exists.

## 21. GET /v1/attention

Call canonical AttentionRankingService.

This manual API preview should follow the same semantics as Telegram `/attention` but must not accidentally record Telegram-specific exposure Events simply because the client fetched the list.

PM-18 v1 rule:

~~~text
GET /attention is read-only
~~~

If a future client needs explicit exposure tracking, add a separate endpoint/event with deliberate semantics.

## 22. GET /v1/weekly

Reuse WeeklyReviewService.

Read-only, no exposure Event.

## 23. GET /v1/search

Reuse lexical search semantics initially.

If PM-14 is implemented, do not silently change this endpoint unless PM-14 explicitly defines a user-facing hybrid search surface.

## 24. POST /v1/ask

HTTP Ask remains asynchronous to preserve the durable PM-13 job/restart model.

Request:

~~~json
{"question": "..."}
~~~

Response:

~~~text
202 Accepted
ask_job_id
status=PENDING
~~~

Use `Idempotency-Key` for retries.

## 25. Ask transport evolution

PM-13 Telegram Ask currently stores generated answer transiently in Delivery payload until Telegram accepts it.

HTTP clients need a pollable result. Evolve Ask transport without turning generated answers into canonical knowledge.

Recommended:

- add a bounded transient `result_json` / result-expiry field to AskJob **or** a dedicated transient API result row;
- preserve answer only until the API client retrieves it or a bounded TTL expires;
- never index it in FTS/embeddings;
- never write it into Item/Content/Event/Profile.

Document the exact retention policy.

## 26. GET /v1/asks/{id}

Return:

~~~text
PENDING/RUNNING
DONE + validated answer/references
FAILED + controlled error
~~~

Only the owning authenticated user may read it.

If result has expired after successful retrieval/TTL, return an explicit expired state rather than hallucinating/re-running Ask automatically.

## 27. Telegram Ask compatibility

Telegram `/ask` continues to use durable DeliveryWorker.

Adding HTTP result transport must not cause:

- a second LLM call;
- duplicate canonical storage;
- different citation validation rules.

One Ask computation should be presentation-channel agnostic after synthesis.

## 28. Lifecycle actions

Endpoints:

~~~text
POST /items/{id}/done
POST /items/{id}/archive
POST /items/{id}/snooze
PATCH /items/{id}/interest
~~~

must call existing lifecycle/interest services and retain CAS/idempotent Event semantics.

No direct ORM mutation in endpoint functions.

## 29. Snooze request

Use explicit supported durations or an absolute ISO timestamp after validation.

Do not accept arbitrary timezone-naive date strings.

Return canonical new state/snoozed_until.

## 30. Interest

Accept only integer:

~~~text
1 | 2 | 3
~~~

Preserve existing `INTEREST_CHANGED` behavior.

## 31. Settings

Expose only user-facing settings, for example:

- timezone;
- digest enabled/time;
- quiet hours;
- attention enabled;
- attention intensity;
- generic motivation enabled.

Never expose provider credentials/model routing or operator secrets.

## 32. PATCH settings

Reuse existing atomic JSON patch/update semantics.

Concurrent independent settings changes must not overwrite each other.

## 33. CORS

Disabled/restrictive by default.

Native Android does not need permissive browser CORS.

Do not ship `*` origin defaults.

## 34. Rate/size bounds

Even for a personal API, apply hard application limits:

- request body size;
- text/question length;
- pagination limit;
- filter string length.

Do not rely only on reverse proxy limits.

## 35. OpenAPI

FastAPI-generated OpenAPI is part of PM-19 integration contract.

Document authentication and major asynchronous response patterns.

Do not expose internal-only health/debug schemas with secrets.

## 36. Health

A simple unauthenticated `/healthz` may report process readiness without private data.

Detailed status/queue metrics remain protected/operator-only.

## 37. Thread/concurrency

All DB work uses the existing async SQLAlchemy session factory.

Do not keep write transactions open during:

- LLM;
- HTTP source fetch;
- Telegram;
- other network calls.

Most API endpoints should be reads/short mutations only.

## 38. Tests — auth

Cover:

- no token → 401;
- wrong token → 401;
- correct token → allowed;
- token never appears in response/log fixtures.

## 39. Tests — capture

Cover:

- text capture;
- URL capture;
- one request → one Item;
- child source routing;
- idempotent retry;
- no extraction/LLM in request handler.

## 40. Tests — reads

Cover:

- list/detail user scope;
- pagination bounds;
- today semantics;
- attention read-only behavior;
- weekly read-only behavior;
- lexical search compatibility.

## 41. Tests — actions

Cover:

- done/archive/snooze/interest;
- duplicate action idempotency;
- invalid state transitions;
- another user's Item inaccessible.

## 42. Tests — Ask

Cover:

- POST queues one AskJob;
- idempotent retry;
- poll pending/running/done/failed;
- citation/reference result identical to PM-13 validated output;
- generated result is transient and never enters Content/FTS;
- expiry behavior.

## 43. Telegram regression

All Telegram flows remain operational with HTTP API enabled or disabled.

The API must not become the new implementation location for Telegram business logic.

## 44. Documentation

Update:

- PRODUCT_SPEC;
- RUNBOOK deployment/auth/TLS notes;
- API reference/OpenAPI instructions;
- PM-19 integration assumptions.

## 45. Acceptance criteria

1. FastAPI is an interface adapter over existing services.
2. API is optional and disabled safely by default if desired.
3. `/v1` versioning is used.
4. Bearer authentication protects all private routes.
5. No request-supplied user_id controls authorization.
6. POST Item is durable/async and idempotent.
7. Existing Telegram ingestion semantics are reused.
8. List/detail endpoints are bounded/user-scoped.
9. `/today`, `/attention`, `/weekly`, `/search` reuse canonical services.
10. API attention/weekly reads create no accidental exposure Events.
11. Ask uses the existing durable Ask computation/citation contract.
12. HTTP Ask result is transient/non-canonical.
13. Lifecycle actions reuse CAS/Event services.
14. User settings reuse atomic update semantics.
15. No permissive CORS default.
16. Request sizes/pagination are bounded.
17. Telegram behavior remains unchanged.
18. Full quality gate passes.

## 46. Definition of Done

~~~text
Android / other client
      ↓ HTTPS + bearer token
FastAPI /v1 adapter
      ↓
existing application services
      ↓
SQLite + workers + providers
~~~

PM-18 is complete when a non-Telegram client can capture, browse, search, ask and act on Items without duplicating business logic or weakening the project's durable background-work model.