# AIInbox — Remaining Post-MVP Specifications (PM-14…PM-20)

Originally prepared after PM-13. Current status update:

~~~text
PM-13 Ask My Inbox — DONE
PM-15 Export / Ownership — DONE
PM-18 HTTP API — DONE (PR #56)
PM-33 Browser Extension — DONE (PR #58)
~~~

PM-33 uses the PM-18 API. Integration Sync Foundation is next in the browser and
integrations track. See
[BROWSER_INTEGRATIONS_INDEX.md](BROWSER_INTEGRATIONS_INDEX.md).

The remaining roadmap phases are documented in this package:

1. [PM-14 — Hybrid Semantic Search](PM-14_HYBRID_SEMANTIC_SEARCH.md)
2. [PM-15 — Export / Ownership](PM-15_EXPORT_OWNERSHIP.md)
3. [PM-16 — Per-operation LLM Routing](PM-16_PER_OPERATION_LLM_ROUTING.md)
4. [PM-17 — Ollama / Local Models](PM-17_OLLAMA_LOCAL_MODELS.md)
5. [PM-18 — HTTP API](PM-18_HTTP_API.md)
6. [PM-19 — Android Client](PM-19_ANDROID_CLIENT.md)
7. [PM-20 — Calendar-aware Attention](PM-20_CALENDAR_AWARE_ATTENTION.md)

## Dependency shape

~~~text
PM-13 Ask My Inbox (DONE)
   ↓
PM-14 Hybrid Semantic Search

PM-15 Export / Ownership

current provider boundary
   ↓
PM-16 Per-operation LLM Routing
   ↓
PM-17 Ollama / Local Models

stable application services
   ↓
PM-18 HTTP API (DONE)
   ├→ PM-33 Browser Extension (DONE)
   └→ PM-19 Android Client (deferred)
       ↓
PM-20 Calendar-aware Attention (deferred)

canonical Item services
   ↓
Integration Sync Foundation
   ↓
Miro
   ↓
additional destinations

PM-07/08 Attention ───────────────────────────────┘
~~~

PM-15 is complete and independent of PM-14.

## Cross-phase architectural decisions

### PM-14

- Semantic retrieval is gated by real PM-13 lexical misses.
- FTS5 is retained.
- Embeddings are a rebuildable SQLite-derived cache.
- Similarity is computed in-process at personal scale.
- `/search` remains lexical in PM-14 v1; Ask is the first hybrid consumer.
- PM-13 citation validation remains authoritative.

### PM-15

- Export is not backup.
- User export is JSON/JSONL + Markdown in a versioned ZIP.
- Full mode may contain original persisted evidence; compact mode does not.
- Secrets, worker internals, callback receipts, generated Ask answers and vectors are excluded.
- Export uses a durable background job and the existing Telegram Delivery outbox.

### PM-16

- Provider/model selection is deterministic per operation.
- Existing service contracts remain provider-agnostic.
- Legacy provider settings remain valid when routing config is absent.
- No LLM selects another LLM.
- No implicit cross-provider fallback in v1.

### PM-17

- Ollama is optional.
- Initial local operations: chunk summary, attention hooks, embeddings.
- Final analysis stays cloud/default until quality is demonstrated.
- Local-route failure must never silently disclose the same data to a cloud fallback.

### PM-18

- FastAPI is a thin `/v1` adapter over existing services.
- Personal bearer auth, not a new SaaS identity system.
- HTTP writes use idempotency keys for Android/offline retries.
- Ask stays durable/asynchronous.
- Telegram continues to use the same application core.

### PM-19

- Android is a thin client, not a second business-logic implementation.
- Share Sheet text/URL capture + durable local retry is the primary new capture value.
- Server remains canonical.
- Telegram notifications stay supported; native push is not required in v1.

### PM-20

- Calendar context is optional and privacy-minimal.
- Recommended v1: Android uploads busy intervals only.
- No calendar titles/descriptions/attendees/locations are needed server-side.
- Fresh busy state suppresses discretionary proactive/motivation sends.
- Calendar fit is a bounded derived Attention signal and never changes semantic priority.

## Suggested milestone grouping

### Milestone G — Knowledge

- PM-13 Ask My Inbox — DONE
- PM-14 Hybrid Semantic Search — EVIDENCE CONFIRMED; runtime ON HOLD pending a usage decision

### Milestone H — Portability & Cost

- PM-15 Export / Ownership — DONE (PR #44)
- PM-16 Per-operation LLM Routing — ON HOLD
- PM-17 Ollama / Local Models — ON HOLD; depends on PM-16

### Milestone I — Additional Clients & Context

- PM-18 HTTP API — DONE (PR #56)
- PM-19 Android Client — DEFERRED
- PM-20 Calendar-aware Attention — DEFERRED

### Milestone J — Browser & External Integrations

- PM-33 Browser Extension — DONE (PR #58)
- Integration Sync Foundation — NEXT, planned
- Miro Connector v1 — PLANNED
- Additional destinations — EVIDENCE-DRIVEN

## Current sequencing override

The historical numeric recommendation is no longer the active execution order.

Current direction:

~~~text
PM-18 HTTP API — DONE
→ PM-33 Browser Extension — DONE
→ Integration Sync Foundation — NEXT
→ Miro
→ additional destinations
~~~

PM-14 and PM-16/17 remain separately gated. PM-19/20 are deferred.

See [BROWSER_INTEGRATIONS_INDEX.md](BROWSER_INTEGRATIONS_INDEX.md).