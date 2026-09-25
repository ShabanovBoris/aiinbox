# AIInbox — Remaining Post-MVP Specifications (PM-14…PM-20)

Prepared against current `main` baseline:

~~~text
8912eca079fd3da31d32c4303c9aa921464ca704
Merge PR #42 — PM-13: add grounded Ask My Inbox
~~~

PM-13 is already merged in this baseline and introduced durable `AskJob`/`AskWorker`, bounded FTS5-grounded context, validated Item/source citations and transient Ask delivery.

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
PM-18 HTTP API
   ↓
PM-19 Android Client
   ↓
PM-20 Calendar-aware Attention (recommended initial calendar source)

PM-07/08 Attention ───────────────────────────────┘
~~~

PM-15 is largely independent and can be scheduled before or after PM-14 based on product priority.

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
- PM-14 Hybrid Semantic Search — PLANNED, evidence-gated

### Milestone H — Portability & Cost

- PM-15 Export / Ownership
- PM-16 Per-operation LLM Routing
- PM-17 Ollama / Local Models

### Milestone I — Additional Clients & Context

- PM-18 HTTP API
- PM-19 Android Client
- PM-20 Calendar-aware Attention

## Implementation-order recommendation

A low-risk practical order is:

~~~text
PM-15
→ PM-16
→ PM-17 (optional depending on local-model need)
→ PM-18
→ PM-19
→ PM-20
~~~

PM-14 should begin when PM-13 usage provides the retrieval-failure evidence required by its spec; it can run before or alongside PM-15/16 without blocking the portability/client track.

This is a sequencing suggestion, not a new product dependency.