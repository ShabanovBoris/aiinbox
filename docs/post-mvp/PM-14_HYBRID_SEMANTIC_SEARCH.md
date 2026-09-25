# PM-14 — Hybrid Semantic Search

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-13 Ask My Inbox merged + documented lexical retrieval misses  
Status: PLANNED

## 1. Problem

PM-13 can answer questions only from Items retrieved by the existing SQLite FTS5 index. FTS is fast, deterministic and explainable, but it can miss relevant Items when the user's question and saved material use different vocabulary.

Examples:

- question: "What did I save about running models fully on-device?"
- source text: "offline inference with llama.cpp on Android"

The next step is not to replace FTS, but to add a bounded semantic signal when real PM-13 usage demonstrates systematic lexical misses.

## 2. Goal

Add hybrid retrieval for Ask My Inbox:

~~~text
query
→ FTS5 lexical candidates
+ query embedding against persisted Item embeddings
→ deterministic hybrid fusion
→ bounded top Items
→ existing PM-13 context builder / citation validation / Ask synthesis
~~~

The PM-13 grounding contract remains unchanged: the model may cite only Items/sources actually supplied in the bounded Ask context.

## 3. Evidence gate

Implementation must not start only because embeddings are fashionable.

Before enabling semantic retrieval, record concrete PM-13 failure cases showing:

- the desired Item exists in AIInbox;
- lexical FTS returns no useful Item or ranks it outside the practical top-N;
- vocabulary mismatch is the cause;
- the query is materially useful to the user.

A small markdown issue/list is enough. No telemetry platform is required.

If no convincing examples exist, keep PM-14 in PLANNED state.

## 4. In scope

- persisted Item-level embeddings;
- deterministic embedding input projection;
- stale/missing embedding refresh;
- query embedding inside background Ask processing;
- in-process cosine similarity;
- hybrid lexical + semantic candidate fusion;
- bounded candidate sets;
- deterministic fallback to lexical-only retrieval;
- tests and retrieval-quality fixtures;
- documentation of real lexical misses and whether PM-14 fixes them.

## 5. Out of scope

- vector database;
- FAISS/pgvector/Elasticsearch/Meilisearch;
- semantic replacement of `/search` in v1;
- semantic ranking for `/today` or `/attention`;
- web search;
- generated answer embeddings;
- embedding ATTENTION_HOOK;
- per-user learned ranking weights;
- ML rerankers;
- cross-user corpus search.

## 6. Initial surface

PM-14 v1 upgrades **Ask retrieval first**.

`/search` remains the current lexical FTS UI because its Telegram handler is intentionally lightweight and must not make an external embedding call.

The AskWorker already owns heavy background work, so query embedding belongs there.

## 7. Embedding boundary

Add a small replaceable protocol, conceptually:

~~~text
EmbeddingProvider.embed_texts(texts: list[str]) -> list[list[float]]
~~~

Do not import provider SDK objects in retrieval/domain code.

The OpenAI/OpenRouter-compatible implementation may live beside existing LLM adapters, but embeddings are a separate capability from generative structured output.

PM-16 may later route the `EMBEDDING` operation to a different provider/model without changing hybrid retrieval logic.

## 8. Configuration

Until PM-16 introduces general per-operation routing, PM-14 may add the minimum explicit embedding settings required to run:

- embedding provider;
- embedding model;
- timeout;
- optional semantic minimum similarity.

Do not reuse an analysis/chat model identifier as an embedding model by assumption.

Credentials continue to come from the existing provider configuration/environment. Never store credentials in SQLite.

## 9. Item embedding projection

Generate one deterministic Item-level embedding input from canonical searchable data.

Recommended inputs:

- title;
- summary;
- user_note;
- tags;
- bounded original persisted Content.

Allowed Content kinds should match the PM-13 evidence policy:

- USER_TEXT;
- WEB_TEXT;
- DOCUMENT_TEXT;
- TRANSCRIPT;
- VISUAL_NOTES;
- DESCRIPTION.

`CHUNK_SUMMARY` may be a fallback when original evidence is absent.

Never embed:

- ATTENTION_HOOK;
- TRANSCRIPT_CHUNK;
- Ask generated answers;
- Reminder payloads;
- Delivery payloads.

## 10. Input bounds

Embedding input must be bounded before provider I/O.

Choose and document an exact v1 maximum. A reasonable initial bound is in the same order as PM-13 per-Item context rather than the full 500k-character document/transcript.

Selection must be deterministic and source-aware so one huge first source does not silently eliminate all later source themes.

## 11. Embedding identity

An embedding is reusable only when its full identity matches:

~~~text
item_id
input_sha256
provider
model
dimension
format_version
~~~

`input_sha256` is computed over the exact normalized embedding input.

Changing Item title/summary/tags/content must naturally invalidate the prior derived embedding through a hash mismatch.

## 12. Storage

Add a derived table, for example:

~~~text
item_embeddings
- item_id PK/FK
- input_sha256
- provider
- model
- dimension
- vector_blob
- format_version
- created_at
- updated_at
~~~

This table is a **derived cache**, not canonical user state.

Do not put vectors in `items` and do not create a vector database.

For personal scale, store a compact float representation in SQLite and compute similarity in process.

## 13. Vector validation

Before persistence, validate:

- non-empty vector;
- finite numeric values only;
- dimension within a sane configured/provider-reported bound;
- dimension consistent for one model identity.

Reject NaN/Inf or malformed provider responses.

## 14. Refresh model

Because semantic retrieval must discover Items even when lexical retrieval misses them, embeddings need coverage beyond current FTS hits.

Use a lightweight background refresh loop over Items whose embedding is:

- missing;
- stale by content hash;
- generated by a different configured embedding identity.

The table is reconstructible, so a separate durable job table is not required by default.

A single-process worker may repeatedly select the next stale/missing Item, release SQLite before external embedding I/O, then persist only if the canonical input hash still matches.

## 15. Eligible Items

Embedding coverage should follow current knowledge search semantics:

- ACTIVE;
- SNOOZED;
- DONE;
- ARCHIVED;
- Items with useful persisted content even if their current processing status is not READY.

Do not index another user's data.

## 16. Crash/restart

If the process crashes during embedding generation, the row simply remains missing/stale and the refresh worker finds it again.

Do not persist an embedding before the provider call finishes and validation succeeds.

## 17. Query embedding

AskWorker computes one embedding for the normalized user question.

This provider call occurs outside a SQLite transaction/session holding write state.

If query embedding fails:

~~~text
fall back to PM-13 lexical retrieval
~~~

unless configuration explicitly requires semantic retrieval. Default v1 should degrade gracefully.

## 18. Similarity

Use cosine similarity in application code over the user's valid current Item embeddings.

No ANN index is needed at personal scale.

Ignore malformed/stale vectors rather than crashing the Ask request.

## 19. Candidate bounds

Do not feed the entire user corpus into fusion.

Initial bounded candidate pools may be, for example:

- top lexical candidates: 20;
- top semantic candidates: 20;
- fused result passed to PM-13 context builder: existing Ask limit (default 8, max 10).

Exact constants must be code-level, documented and tested.

## 20. Hybrid fusion

Do not add raw BM25 and cosine values directly without normalization; the scales are unrelated.

Use a deterministic fusion strategy such as reciprocal-rank fusion or another documented normalization.

Requirements:

- lexical matches remain meaningful;
- semantically relevant lexical misses can enter the top result set;
- exact strong lexical matches should not be displaced by weak semantic noise;
- tie-breaks are deterministic;
- no user-specific learned weights in v1.

## 21. Metadata signal

A small deterministic metadata tie-break/boost may use already persisted fields such as title/tag overlap.

Do not use priority/attention score as a knowledge-retrieval relevance signal unless a later spec explicitly requires it.

## 22. Semantic threshold

Very low cosine matches should not flood Ask context.

If a minimum similarity threshold is used, it must be:

- explicit configuration/code constant;
- model-aware where necessary;
- covered by fixtures;
- not presented as a universal semantic truth.

Lexical candidates remain available regardless of semantic threshold.

## 23. PM-13 grounding stays authoritative

Hybrid retrieval only changes **which Items become candidates**.

Everything after retrieval remains PM-13:

~~~text
bounded Item/source context
→ LLM synthesis
→ strict structured result
→ citation validation against actual supplied context
→ Telegram delivery
~~~

Semantic similarity itself is never a citation.

## 24. Privacy

Embeddings are derived from private Inbox content and must be treated as private user data.

Do not:

- log vectors;
- export vectors by default in PM-15;
- share vectors between users;
- send embeddings to analytics services.

## 25. `/search`

PM-14 v1 does not change `/search` output or latency contract.

A later explicit feature may offer semantic search UI if needed.

## 26. Reindexing

Changing the configured embedding provider/model/format version makes old rows stale.

Do not run a destructive migration rewriting all vectors in one transaction.

Let the derived refresh worker converge incrementally.

## 27. Tests — identity/storage

Cover:

- exact input hash reuse;
- Item/content change invalidates hash;
- model/provider change invalidates row;
- malformed vector rejected;
- dimension mismatch rejected;
- vectors remain user-scoped.

## 28. Tests — refresh

Cover:

- missing Item gets embedding;
- stale Item gets replaced;
- crash/interrupted call leaves reconstructible missing state;
- canonical content changing during provider call prevents stale vector commit;
- provider failure leaves prior valid vector untouched where appropriate.

## 29. Tests — retrieval

Use deterministic fake vectors.

Cover:

- lexical-only strong match;
- semantic-only vocabulary-mismatch match;
- candidate union/fusion order;
- low-similarity exclusion;
- deterministic ties;
- top-N bounds;
- DONE/ARCHIVED retrieval;
- cross-user isolation.

## 30. PM-13 regression tests

Ensure:

- no semantic provider available → lexical Ask still works;
- query embedding failure → lexical fallback;
- citation allowlist still uses actual supplied context;
- no generated answer enters embedding input;
- ATTENTION_HOOK remains excluded.

## 31. Evidence verification

For each real lexical miss that justified PM-14, add a regression fixture or documented manual check showing whether hybrid retrieval surfaces the intended Item.

Do not declare PM-14 useful solely because cosine similarity code works.

## 32. Documentation

Update:

- PRODUCT_SPEC;
- RUNBOOK;
- PM-13/PM-14 relationship;
- provider configuration;
- derived cache rebuild behavior.

Document that deleting `item_embeddings` is recoverable; deleting Items/Contents is not.

## 33. Acceptance criteria

1. PM-14 has documented real lexical-miss evidence before activation.
2. Existing FTS5 remains available and authoritative as lexical retrieval.
3. `/search` remains lexical in v1.
4. Ask can use lexical + semantic candidates.
5. No vector database is introduced.
6. Item embeddings are derived/rebuildable SQLite data.
7. Embedding identity includes exact input hash + provider/model/version.
8. Original evidence policy matches PM-13.
9. ATTENTION_HOOK/TRANSCRIPT_CHUNK/generated Ask answers are excluded.
10. Embedding input is bounded and source-aware.
11. Query embedding runs outside DB transactions.
12. Semantic failure degrades to lexical Ask by default.
13. Similarity is user-scoped and computed in process.
14. Candidate pools are bounded.
15. Fusion is deterministic and documented.
16. Semantic similarity never bypasses PM-13 citation validation.
17. DONE/ARCHIVED knowledge remains searchable.
18. Real documented lexical misses have regression coverage.
19. Full quality gate passes.

## 34. Definition of Done

~~~text
persisted Inbox
   ↓
deterministic embedding projection
   ↓
SQLite item_embeddings (derived)
   ↓
query ──→ FTS5 lexical rank
  │
  └────→ query embedding → cosine rank
                    ↓
             deterministic fusion
                    ↓
               top 8 Items
                    ↓
         existing PM-13 grounding
                    ↓
             validated answer
~~~

PM-14 is complete when semantic retrieval fixes demonstrated lexical gaps without replacing FTS, weakening provenance, or adding infrastructure disproportionate to a personal inbox.