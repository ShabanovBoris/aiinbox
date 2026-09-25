# PM-16 — Per-operation LLM Routing

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: existing provider boundaries; PM-13 Ask operation; PM-14 embedding operation if implemented  
Status: PLANNED

## 1. Problem

AIInbox currently has replaceable provider adapters, but most generative operations share one configured provider/analysis model. Different operations have different quality, privacy, latency and cost requirements.

Examples:

- final Item analysis needs high reliability;
- chunk summaries can be cheaper;
- vision requires a vision-capable model;
- transcription uses a different endpoint/model;
- Ask needs structured grounded synthesis;
- embeddings use a non-chat model.

## 2. Goal

Make model/provider selection deterministic **per operation**, without leaking routing decisions into business services.

Initial operation classes:

~~~text
FINAL_ANALYSIS
CHUNK_SUMMARY
VISION
TRANSCRIPTION
PROFILE_PATCH
ATTENTION_HOOK
ASK_INBOX
EMBEDDING
~~~

If PM-14 is not yet implemented, keep `EMBEDDING` as a routing contract only when it has a real consumer; do not add dead infrastructure purely for the enum.

## 3. Core rule

Routing is configuration/code.

Never ask an LLM:

> Which model should handle this request?

No dynamic model selection based on generated prose.

## 4. In scope

- stable operation identifiers;
- deterministic route configuration;
- composition-root provider registry/factories;
- backward-compatible defaults from current settings;
- capability validation at startup;
- operation-aware logging/metrics without private content;
- tests for every route;
- explicit privacy/fallback behavior.

## 5. Out of scope

- automatic cost optimization;
- autonomous provider selection;
- real-time price lookup;
- quality scoring by another LLM;
- multi-armed bandits;
- arbitrary user-selected model IDs from Telegram;
- provider failover unless explicitly configured;
- Ollama implementation (PM-17).

## 6. Preserve application contracts

Existing services should continue to depend on small capabilities:

- `LlmProvider.analyze()`;
- `summarize_chunk()`;
- `describe_images()`;
- `profile_update()`;
- `generate_attention_hooks()`;
- `answer_inbox()`;
- transcription provider;
- embedding provider if PM-14 exists.

Do not add `if settings.model == ...` branches to Analyzer, AskWorker, AttentionHookService, etc.

## 7. Routing architecture

Recommended shape:

~~~text
Settings
  ↓
ProviderRegistry / factories
  ↓
OperationRouter
  ├ FINAL_ANALYSIS   → provider/model A
  ├ CHUNK_SUMMARY    → provider/model B
  ├ VISION           → provider/model C
  ├ TRANSCRIPTION    → provider/model D
  ├ PROFILE_PATCH    → provider/model B
  ├ ATTENTION_HOOK   → provider/model B
  ├ ASK_INBOX        → provider/model A
  └ EMBEDDING        → embedding provider/model E
~~~

The composition layer returns adapters/facades that preserve existing service protocols.

## 8. Facade compatibility

A practical implementation is a routed facade implementing the current LLM protocol:

~~~text
RoutedLlmProvider.analyze()
  → route FINAL_ANALYSIS

RoutedLlmProvider.summarize_chunk()
  → route CHUNK_SUMMARY

RoutedLlmProvider.describe_images()
  → route VISION
...
~~~

Transcription and embeddings may remain separate protocols but use the same operation router/config registry.

## 9. No business-code route lookup

Bad:

~~~text
AskWorker
→ router.resolve("ASK_INBOX")
→ provider-specific call
~~~

Preferred:

~~~text
AskWorker
→ provider.answer_inbox()

Routed provider facade
→ configured ASK_INBOX route
~~~

This keeps routing as infrastructure/composition concern.

## 10. Route configuration

Use one documented structured setting rather than dozens of unrelated ad-hoc variables.

Example conceptual JSON:

~~~json
{
  "FINAL_ANALYSIS": {"provider": "openai", "model": "..."},
  "CHUNK_SUMMARY": {"provider": "openrouter", "model": "..."},
  "VISION": {"provider": "openai", "model": "..."},
  "TRANSCRIPTION": {"provider": "openai", "model": "..."},
  "PROFILE_PATCH": {"provider": "openrouter", "model": "..."},
  "ATTENTION_HOOK": {"provider": "openrouter", "model": "..."},
  "ASK_INBOX": {"provider": "openai", "model": "..."},
  "EMBEDDING": {"provider": "openai", "model": "..."}
}
~~~

Exact representation may use Pydantic nested settings instead of raw JSON.

## 11. Backward compatibility

When no per-operation routing is configured, preserve current behavior.

Map legacy settings conceptually:

~~~text
FINAL_ANALYSIS
CHUNK_SUMMARY
PROFILE_PATCH
ATTENTION_HOOK
ASK_INBOX
→ current analysis provider/model

VISION
→ current vision model/provider

TRANSCRIPTION
→ current transcription model/provider

EMBEDDING
→ PM-14 embedding configuration if present
~~~

Existing deployment must not become unstartable just because the new routing setting is absent.

## 12. Provider identities

Supported initial route providers remain the providers already implemented:

- openai;
- openrouter.

PM-17 later adds `ollama`.

Do not support arbitrary Python module/class names in configuration.

## 13. Credential ownership

Routes refer to provider names/model IDs only.

Secrets stay in existing provider-specific environment settings.

Never put API keys into route JSON, SQLite, logs or user settings.

## 14. Startup validation

Fail fast before workers start if a configured route is impossible.

Examples:

- unknown operation;
- unknown provider;
- missing credentials;
- missing model ID;
- VISION routed to adapter with no vision capability;
- TRANSCRIPTION routed to a non-transcription adapter;
- EMBEDDING routed to provider without embedding capability.

A bad production routing config should not be discovered only after a user sends an Item.

## 15. Capability model

Extend capability description only where useful.

Possible capabilities:

~~~text
structured_output
vision
transcription
embedding
~~~

Do not create dozens of speculative capability flags.

## 16. Structured output

Operations requiring strict schemas keep their existing Pydantic validation regardless of route.

Routing must not weaken:

- AnalysisResult validation;
- AttentionHookGeneration validation;
- AskInboxResult validation;
- prompt-injection boundaries.

## 17. Timeout

Allow operation-appropriate timeout configuration without duplicating retry loops inside business services.

Existing transcription timeout remains separate where endpoint behavior differs.

Keep defaults backward-compatible.

## 18. Retries

PM-16 does not redesign application retry semantics.

Existing bounded retries remain owned by the current operation implementation.

Do not add provider-hopping retries by default.

## 19. Fallback policy

Cloud/provider fallback can create privacy and cost surprises.

Therefore v1 rule:

~~~text
one explicit primary route per operation
~~~

If fallback is later supported, it must be configured explicitly and preserve a clear rule such as:

- never send a locally-routed private operation to cloud unless fallback is explicitly enabled;
- log which configured route was actually used;
- keep retries bounded.

Do not silently fallback across providers in PM-16 v1.

## 20. Operation identity in logs

Log bounded operational metadata:

~~~text
operation
provider
model
success/failure
latency
error code
~~~

Never log:

- source content;
- prompt text;
- generated answer;
- API keys.

## 21. Cost accounting

Exact token/cost accounting is optional in v1.

If the SDK exposes usage, it may be logged as numeric operational metadata, but do not build a billing warehouse.

## 22. Model IDs

Business code contains no hardcoded provider model IDs.

Test fixtures may use fake identifiers.

## 23. FINAL_ANALYSIS

Must continue to provide:

- strict structured output;
- quality adequate for canonical Item analysis;
- current language semantics;
- multi-source synthesis.

PM-16 must not lower final-analysis quality merely to prove routing works.

## 24. CHUNK_SUMMARY

Can be routed to a cheaper model, but must preserve:

- untrusted-content instruction isolation;
- multi-source themes;
- non-empty bounded summary.

Durable chunk identity continues to depend on chunk/settings/input hash, not only provider route.

If provider/model becomes part of reuse compatibility, document it explicitly so summaries are not silently reused across incompatible generation contracts.

## 25. VISION

Route must expose vision capability.

Vision remains optional enrichment; a vision provider failure must keep current transcript-only fallback behavior where applicable.

## 26. TRANSCRIPTION

Keep separate audio endpoint/adapter semantics.

PM-16 routes which transcription provider/model to use; it does not make chat adapters transcribe audio.

Existing segment checkpoint compatibility must include provider/model identity as today.

## 27. PROFILE_PATCH

Must preserve strict ProfilePatch schema and atomic DB-side merge.

No provider route may receive authority to mutate fields outside the validated patch.

## 28. ATTENTION_HOOK

Must preserve evidence grounding and exact persisted-content validation.

A cheaper routed model may generate candidates, but application validation remains authoritative.

## 29. ASK_INBOX

Must preserve PM-13:

- bounded context;
- no tools/web;
- structured citations;
- application citation validation;
- insufficient-context fallback.

## 30. EMBEDDING

If PM-14 exists, route the embedding operation without changing ItemEmbedding identity rules.

Embedding provider/model remains part of derived vector identity.

## 31. Test doubles

Provide deterministic fake providers per capability/operation.

Tests must not require live OpenAI/OpenRouter credentials.

## 32. Tests — defaults

With no new routing config:

- existing OpenAI deployment behaves as before;
- existing OpenRouter deployment behaves as before;
- all current tests remain green.

## 33. Tests — operation routing

Configure different fake models/providers and assert:

- analyze uses FINAL_ANALYSIS;
- summarize uses CHUNK_SUMMARY;
- vision uses VISION;
- profile uses PROFILE_PATCH;
- hooks use ATTENTION_HOOK;
- Ask uses ASK_INBOX;
- transcription uses TRANSCRIPTION;
- embeddings use EMBEDDING if available.

## 34. Tests — invalid configuration

Startup fails for:

- unknown operation/provider;
- missing model;
- missing credentials;
- capability mismatch;
- malformed route configuration.

Failure messages must identify operation/provider without printing secrets.

## 35. No routing mutation at runtime

User Telegram/API calls cannot modify routing config in PM-16.

No `/settings model` command.

Operator configuration requires process restart unless a later feature explicitly adds safe reload.

## 36. Documentation

Update:

- PRODUCT_SPEC provider section;
- RUNBOOK configuration examples;
- `.env.example` or equivalent;
- DECISIONS if route facade is a non-obvious architecture change.

## 37. Acceptance criteria

1. Stable operation identifiers exist only for real supported operations.
2. Routing is deterministic configuration/code.
3. No LLM selects another LLM.
4. Existing application service interfaces remain provider-agnostic.
5. Legacy config remains valid.
6. Each operation can select provider/model independently.
7. Startup validates all configured routes.
8. Capability mismatch fails fast.
9. Secrets are not stored in route config/DB/logs.
10. Structured-output validation is unchanged.
11. Current retry/recovery semantics remain owned by each feature.
12. No implicit cross-provider fallback exists in v1.
13. Logs expose operation/provider/model without content.
14. Full regression suite passes.

## 38. Definition of Done

~~~text
Business services
      ↓
existing capability methods
      ↓
Routed provider facade
      ↓
operation key
      ↓
validated route config
      ↓
provider adapter + model
~~~

PM-16 is complete when model/provider choice can change per operation without changing application business logic or weakening current validation, privacy and recovery guarantees.