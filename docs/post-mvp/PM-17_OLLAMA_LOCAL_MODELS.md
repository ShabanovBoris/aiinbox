# PM-17 — Ollama / Local Models

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-16 Per-operation LLM Routing  
Status: PLANNED

## 1. Problem

Some AIInbox operations are cheap, repetitive or privacy-sensitive enough to run locally. Today all supported analysis routes are cloud/OpenAI-compatible providers.

A local provider can reduce recurring cost and keep selected content on the user's machine, but it must not silently lower quality for canonical final analysis.

## 2. Goal

Add Ollama as an optional routed provider for explicitly selected operations while preserving all application-level validation and grounding.

Initial recommended operations:

~~~text
CHUNK_SUMMARY
ATTENTION_HOOK
EMBEDDING
~~~

Do not require local models for `FINAL_ANALYSIS` in v1.

## 3. In scope

- Ollama provider adapter;
- explicit PM-16 `provider=ollama` route;
- local chat/structured-output handling where supported;
- local embedding support;
- startup capability/health validation only when Ollama is configured;
- privacy-safe fallback semantics;
- deterministic fake tests;
- manual quality fixtures for selected operations.

## 4. Out of scope

- installing/managing GPU drivers;
- downloading multi-GB models automatically without operator intent;
- model marketplace UI;
- choosing models automatically;
- fine-tuning;
- local speech-to-text unless separately implemented and routed later;
- making Ollama mandatory;
- silently sending local-routed data to cloud on failure.

## 5. Provider identity

PM-16 route provider name:

~~~text
ollama
~~~

Operator config supplies:

- Ollama base URL;
- model names through per-operation routes;
- timeouts.

Default local endpoint may be documented as `http://127.0.0.1:11434`, but business code must not hardcode it.

## 6. Endpoint ownership

The Ollama base URL is operator configuration, not user input.

Telegram/API users cannot submit arbitrary Ollama endpoints.

This is not a general HTTP fetch feature and must not reuse the web extractor.

## 7. Adapter boundary

Create provider implementation behind current PM-16 routing contracts.

Business services continue to call:

- summarize chunk;
- generate attention hooks;
- embedding provider.

No `if provider == "ollama"` branches in Analyzer, AttentionHookService or hybrid retrieval.

## 8. HTTP client

Use the existing `httpx` dependency unless a concrete requirement justifies another client.

Apply:

- bounded timeout;
- bounded response size where practical;
- explicit JSON parsing;
- controlled provider error mapping.

## 9. Structured output

Application Pydantic validation remains mandatory even if Ollama/model claims structured JSON support.

For operations with schema output:

~~~text
provider constrained JSON if available
→ Pydantic validation
→ bounded retry on invalid output
~~~

Never regex-parse free prose into an AttentionHook schema.

## 10. Prompt injection

Local execution does not weaken trust boundaries.

Persisted source content remains untrusted data:

- chunk summarizer ignores embedded instructions;
- attention hook generation is still grounded against exact persisted excerpts;
- no tools are exposed.

## 11. CHUNK_SUMMARY

Ollama may handle `CHUNK_SUMMARY` when explicitly configured.

Required behavior:

- non-empty summary;
- preserve substantive source themes;
- bounded output;
- no instructions from source text followed;
- current chunk checkpoint compatibility remains correct.

Provider/model identity should be considered if changing model can invalidate compatible summary reuse semantics.

## 12. ATTENTION_HOOK

Local model may propose hooks, but PM-09 application validation remains authoritative:

- exact `source_content_id` allowlist;
- exact evidence excerpt validation;
- source ownership;
- bounded hook types/text;
- zero candidates allowed when grounding is weak.

A local model cannot bypass grounding by being "trusted".

## 13. EMBEDDING

If PM-14 is implemented, Ollama may provide the EMBEDDING route.

Embedding identity continues to include:

- provider=`ollama`;
- model;
- dimension;
- input hash;
- format version.

Switching cloud → local embedding model triggers derived-cache refresh, not an in-place reinterpretation of old vectors.

## 14. FINAL_ANALYSIS

Explicitly out of the initial rollout.

Do not route FINAL_ANALYSIS to Ollama by default.

It may be enabled later only after a documented quality evaluation shows acceptable:

- structured AnalysisResult compliance;
- category/type stability;
- multi-source coverage;
- language behavior;
- priority-factor quality.

## 15. ASK_INBOX

Not required for PM-17 v1.

If tested later, it must preserve PM-13 citation/insufficient-context contract. Local execution alone is not evidence of answer quality.

## 16. PROFILE_PATCH

Not required in initial allowlist because an invalid local patch can alter long-term personalization.

Can be considered later with schema/quality fixtures.

## 17. TRANSCRIPTION

Not part of initial Ollama integration.

Ollama is not treated as a generic replacement for the current transcription adapter.

## 18. Vision

Not required in PM-17 v1.

If a future local multimodal model is added, it enters through PM-16 `VISION` capability and must preserve current graceful visual-enrichment semantics.

## 19. Health validation

Only validate Ollama connectivity/models when at least one PM-16 operation route uses `ollama`.

A deployment using only OpenAI/OpenRouter must not fail because Ollama is absent.

## 20. Startup behavior

For configured Ollama routes, fail fast on clearly invalid operator configuration such as:

- malformed base URL;
- required model not configured;
- local service unreachable if strict startup validation is selected.

If health checking is intentionally non-fatal, document exactly which runtime error path will occur instead.

## 21. Model availability

Do not automatically pull large models during application startup.

RUNBOOK should provide explicit operator commands to install/pull the configured model before AIInbox starts.

## 22. Privacy/fallback

This is a blocker-level invariant:

~~~text
local route failure
≠ automatic cloud disclosure
~~~

If an operation is routed to Ollama and no explicit fallback is configured, failure follows the normal operation error/fallback behavior without sending the same private input to OpenAI/OpenRouter.

If PM-16 later supports configured fallbacks, cloud fallback must be opt-in and documented.

## 23. Network exposure

Recommend binding Ollama to loopback/private trusted network only.

AIInbox must not expose Ollama directly through its future HTTP API.

## 24. Docker/deployment

Ollama can remain an external host service in v1.

Optional Docker Compose profile may be documented only if it does not make GPU/runtime management part of AIInbox core.

Do not bundle model files inside the application image.

## 25. Resource bounds

Local models can saturate CPU/GPU/RAM.

Keep:

- existing worker concurrency bounded;
- one embedding/LLM call per operation task as today;
- configured timeouts;
- shutdown cancellation behavior.

Do not increase processing concurrency automatically because the provider is local.

## 26. Error mapping

Map Ollama-specific transport/model failures into existing application/provider errors.

User-facing layers should not receive raw local HTTP stack traces.

## 27. Logging

Log:

- operation;
- provider=ollama;
- model;
- latency;
- error code.

Do not log prompts/source text/generated private output.

## 28. Quality fixtures

Create deterministic representative fixtures for each enabled local operation.

Examples:

### Chunk summary
- multi-topic source;
- prompt injection text;
- long chunk;
- non-English source.

### Attention hook
- strong grounded excerpt;
- no-grounding case;
- fake instruction requesting outside facts.

### Embedding
- deterministic fake vector tests in unit suite;
- optional live smoke for configured local model.

## 29. Live tests

CI must not require Ollama or downloaded models.

Live integration smoke is optional/operator-run.

All application behavior must be unit/integration testable through fake adapters.

## 30. Performance check

Before declaring a local route suitable, record approximate local latency on the intended hardware for representative requests.

No universal SLA is required, but an operation that blocks the single-process workload for unacceptable periods should not become the default route.

## 31. Documentation

Update:

- `.env.example`/config docs;
- RUNBOOK model preparation;
- PM-16 route examples;
- privacy/fallback semantics;
- supported operation matrix.

## 32. Acceptance criteria

1. `ollama` is a valid PM-16 provider only when implemented/configured.
2. Existing cloud-only deployment remains unchanged when no Ollama route exists.
3. CHUNK_SUMMARY can run through Ollama.
4. ATTENTION_HOOK can run through Ollama with unchanged grounding validation.
5. EMBEDDING can run through Ollama when PM-14 exists.
6. FINAL_ANALYSIS is not moved to Ollama by default.
7. No silent cloud fallback occurs.
8. Application Pydantic validation remains authoritative.
9. Source prompt injection remains untrusted.
10. Model files are not bundled/pulled automatically by core runtime.
11. CI does not require a live Ollama daemon.
12. Full existing cloud-provider regression suite passes.

## 33. Definition of Done

~~~text
PM-16 route
   ↓
operation = CHUNK_SUMMARY / ATTENTION_HOOK / EMBEDDING
   ↓
provider = ollama
   ↓
local HTTP adapter
   ↓
strict application validation
   ↓
existing persistence/business semantics
~~~

PM-17 is complete when selected low-risk operations can run locally without changing business logic, weakening grounding, or silently exporting data to a cloud fallback.