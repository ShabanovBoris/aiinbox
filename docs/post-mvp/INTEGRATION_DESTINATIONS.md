# Additional Integration Destinations

Type: Post-MVP Strategy + Connector Selection Specification  
Prerequisite: Miro Connector v1 used in real workflow  
Status: PLANNED / EVIDENCE-DRIVEN

## 1. Goal

Define how AIInbox adds destinations after Miro without turning the codebase into
a generic integration platform.

Miro is the first proving ground.

The next provider is selected from actual workflow need, not from the size of its
API ecosystem.

## 2. Rule

Do not implement several destination adapters in parallel.

Sequence:

~~~text
Integration Sync Foundation
→ Miro
→ real usage
→ document friction/gaps
→ select next destination
→ add one adapter
~~~

The shared foundation may evolve only from concrete overlap between at least two
real connectors.

## 3. Provider selection criteria

Evaluate each candidate on:

1. real user value;
2. stable create/update API;
3. stable remote object id;
4. authentication complexity;
5. rate limits;
6. ability to store a stable AIInbox marker;
7. ability to update one existing object idempotently;
8. readable representation for an Item;
9. failure/recovery behavior;
10. privacy impact.

Do not select a destination only because it is popular.

## 4. Shared contract remains narrow

Every destination should consume the same ItemSyncProjection where possible.

Provider adapters own:

- authentication;
- remote object type;
- rendering;
- rate-limit parsing;
- remote error mapping;
- remote recovery.

Canonical Item/services must not branch on provider names.

Bad:

~~~text
if provider == "miro": ...
elif provider == "notion": ...
~~~

inside Item processing.

Good:

~~~text
destination.upsert_item(projection, mapping)
~~~

inside integration worker.

## 5. Capability differences

Do not force every provider into identical behavior.

Possible capabilities:

~~~text
UPSERT_ITEM
REMOTE_ARCHIVE
CUSTOM_METADATA
DEEP_LINK
BATCH_CREATE
BATCH_UPDATE
MARKDOWN_BODY
ATTACHMENTS
WEBHOOKS
~~~

Capabilities belong to the adapter/connection.

The V1 foundation should use only what Miro actually needs.

## 6. Candidate: Notion

Potential value:

- structured personal knowledge database;
- fields/properties;
- readable pages;
- useful filtering outside AIInbox.

Likely projection:

~~~text
one Item
→ one database row/page
~~~

Potential mapping:

- title;
- summary;
- category;
- type;
- state;
- interest;
- priority;
- created_at;
- source link.

Important risk:

Notion pages are naturally editable, which may create an expectation of
bidirectional sync.

V1 must clearly remain one-way unless a later spec explicitly introduces remote
mutation semantics.

## 7. Candidate: Generic Webhook

Potential value:

- enables n8n/Make/custom scripts;
- low provider-specific complexity;
- useful bridge before building many first-party adapters.

Possible V1:

~~~text
POST configured HTTPS endpoint
{
  event: item.upsert,
  item: <bounded ItemSyncProjection>
}
~~~

Security requirements:

- HTTPS only;
- per-connection signing secret;
- HMAC signature;
- timestamp/replay bound;
- bounded retries.

A generic webhook must not accept remote commands back into AIInbox in its first
version.

## 8. Candidate: Google Drive / Docs / Sheets

Potential value:

- durable user-owned document views;
- spreadsheet index of saved material;
- document-based review workflows.

Do not treat Google as one connector.

Potential independent adapters:

~~~text
Google Sheets index
Google Docs generated dossier
Drive export folder
~~~

Each should have one clear use case.

A spreadsheet mirror is usually a better first candidate than arbitrary document
generation because row identity/upsert is simpler.

## 9. Candidate: Obsidian / Markdown folder

Potential value:

- local-first personal knowledge;
- portable files;
- version control;
- no third-party API when server has filesystem access.

Possible representation:

~~~text
one Item
→ one Markdown file
~~~

Important rule:

This may be better modeled as a local export projection than a remote connector.

Do not force local filesystem sync through OAuth/provider abstractions.

## 10. Candidate: Notion vs Obsidian

These solve different problems.

Notion:

- remote collaborative database;
- OAuth/network API;
- remote identity/rate limits.

Obsidian/Markdown:

- local file projection;
- path/filename safety;
- filesystem atomicity;
- no rate limit.

The integration foundation may share ItemSyncProjection but not every transport
mechanism.

## 11. Candidate: task/project systems

Only add task/project destinations after PM-26/27 action/project semantics exist
or a clear current workflow requires simple save mirroring.

Do not map every Item to a task merely because an API supports tasks.

AIInbox Items are not automatically TODOs.

## 12. Candidate: messaging/chat destinations

Slack/Teams/Discord-like destinations are poor default full-archive mirrors.

They may later be useful for explicit notifications or team sharing, which is a
different product capability from sync all saves.

Do not mix notification destinations into the Item projection connector unless a
real use case justifies it.

## 13. Full archive support

A destination called a save mirror must support:

- bootstrap existing Items;
- incremental new Items;
- updates;
- restart recovery;
- mapping persistence.

If a provider only supports fire-and-forget posting, document it as an event sink,
not a full synchronization destination.

## 14. Delete semantics

Default across destinations:

~~~text
remote delete
≠
AIInbox delete
~~~

Remote deletion never mutates canonical data.

Provider-specific replacement/archive behavior belongs in its spec.

## 15. Bidirectional gate

Two-way sync is not enabled merely because a second provider supports webhooks.

Before bidirectional sync, define:

- field ownership;
- allowed remote mutations;
- conflict semantics;
- deletion semantics;
- provenance;
- idempotency;
- webhook authentication;
- replay handling;
- offline conflicts.

This requires a separate PM/spec.

## 16. Destination-specific content limits

Each adapter must define:

- title bound;
- body/summary bound;
- source-link count;
- supported formatting;
- attachment policy.

Do not let provider maximum sizes determine canonical AIInbox data.

## 17. Credential isolation

Every provider credential follows the same security invariants:

- separate from canonical content;
- separate from PM-15 export;
- never sent to LLM;
- never logged;
- least privilege scopes;
- explicit reconnect on invalid grant.

## 18. Provider library policy

Do not automatically install an SDK for every destination.

Prefer existing httpx for small REST adapters.

An SDK must justify:

- active maintenance;
- async behavior;
- OAuth correctness;
- dependency weight.

## 19. New connector checklist

Before implementation, create a provider-specific spec answering:

~~~text
What remote object represents one Item?
What is the stable remote id?
What is the upsert path?
How is auth stored/refreshed?
How do we recover crash-after-create?
How are rate limits handled?
How is remote deletion handled?
Which fields are projected?
What is explicitly not synced?
What test fake is required?
~~~

No connector should start without those answers.

## 20. Evaluation after Miro

Collect real evidence:

- number of synced Items;
- bootstrap duration;
- duplicate incidents;
- remote edits overwritten;
- provider auth failures;
- rate-limit frequency;
- fields actually useful in the remote view;
- whether full archive or only active Items are actually wanted;
- whether remote links back to AIInbox are needed.

Use this evidence to decide the second adapter and refine the common boundary.

## 21. Likely evaluation order

This is not a commitment, but a reasonable order to evaluate:

~~~text
1. Generic signed webhook
2. Notion
3. Google Sheets / Drive projection
4. Obsidian / Markdown local projection
~~~

The actual order follows usage.

## 22. Why generic webhook is attractive

A signed webhook can validate whether users actually need many destinations
before first-party adapters are written for each.

It can connect AIInbox to external automation tools while preserving:

~~~text
AIInbox → outbound event/projection only
~~~

It must not become an arbitrary inbound tool-execution endpoint.

## 23. Why not build a Zapier/n8n-style engine

AIInbox core value is attention and knowledge, not workflow orchestration.

Avoid:

- visual workflow builder;
- arbitrary triggers;
- conditional scripting;
- credential marketplace;
- hundreds of connectors.

A small set of useful projections is sufficient until demand proves otherwise.

## 24. Testing requirements per provider

Every new provider adds:

- deterministic fake adapter tests;
- auth error tests;
- rate-limit tests;
- create/update mapping tests;
- crash/restart recovery;
- remote deletion behavior;
- no-core-blocking test.

Live smoke remains optional/operator-run.

## 25. Documentation requirements per provider

Each connector spec documents:

- setup;
- auth/scopes;
- target selection;
- projected fields;
- bootstrap behavior;
- incremental behavior;
- retry semantics;
- one-way warning;
- disconnect behavior;
- credential removal;
- known provider limits.

## 26. Acceptance criteria for a second destination

The second destination is ready only if:

1. no Item business logic branches on provider;
2. foundation adapter boundary is reused;
3. connection/projection data model remains valid;
4. no Miro-specific assumptions leak into common services;
5. provider-specific crash-after-create recovery exists;
6. credentials remain isolated;
7. core application remains independent of provider availability;
8. remote mutations remain non-canonical;
9. tests require no live provider;
10. the adapter solves a demonstrated workflow.

## 27. Long-term direction

~~~text
                          ┌→ Miro
canonical AIInbox Item → Sync Foundation ─→ Notion
                          ├→ Webhook
                          ├→ Google projection
                          └→ local Markdown
~~~

The center remains small.

The edges remain provider-specific.

AIInbox stays the source of truth.
