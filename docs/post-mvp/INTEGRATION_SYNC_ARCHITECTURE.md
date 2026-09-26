# External Integration Sync Architecture

Status: planning document  
Initial destination: Miro  
Direction: outbound projections from canonical AIInbox state.

## 1. Purpose

Define the smallest durable synchronization architecture needed to mirror AIInbox
saves into Miro and later other platforms without turning those platforms into
canonical storage.

## 2. Invariants

- AIInbox remains source of truth.
- Sync is one-way in v1.
- Network work is never performed inside ingestion/action DB transactions.
- Every remote object has a durable local mapping.
- Retrying a sync is idempotent.
- A broken connector cannot block core AIInbox processing.
- Provider credentials never enter Item/Content/Event data.
- LLMs receive no connector credentials or tool authority.

## 3. Suggested persistence seam

Exact schema should be finalized during implementation, but the durable concepts
should be equivalent to:

### IntegrationConnection

Represents one configured target.

Fields conceptually include:

- id;
- user_id;
- provider;
- enabled;
- non-secret target metadata, e.g. board/database identifier;
- created_at / updated_at.

Secrets remain in environment/secret storage for the current personal deployment.

### IntegrationProjection

Maps one canonical Item to one remote object.

Conceptually:

- connection_id;
- item_id;
- remote_object_id;
- last_projection_hash/version;
- last_synced_at;
- status/error metadata.

Unique:

~~~text
(connection_id, item_id)
~~~

### IntegrationSyncJob

Durable queue entry for bootstrap/incremental work.

It should support:

- PENDING/RUNNING/DONE/FAILED or equivalent;
- bounded retry metadata;
- restart recovery;
- optional bootstrap cursor/batch identity.

Do not introduce a generic distributed queue.

## 4. Projection contract

Build an immutable Item projection before provider I/O.

Conceptually:

~~~text
ItemSyncProjection
  item_id
  title
  summary
  category
  item_type
  state
  interest_level
  priority_score
  created_at
  safe source links
~~~

The adapter consumes this DTO, not ORM objects.

## 5. Change detection

V1 can compute a deterministic hash over the fields represented remotely.

If:

~~~text
current projection hash == last synced hash
~~~

no provider call is needed.

This avoids creating an Item version/event-sourcing subsystem only for sync.

## 6. Bootstrap

A new connection needs a resumable initial sync of all existing Items.

Requirements:

- deterministic bounded batches;
- restart-safe cursor/progress;
- remote idempotent upsert;
- mapping persisted after successful upsert;
- no single transaction over the entire archive.

## 7. Incremental triggers

Events that may require a projection update include:

- Item created;
- analysis becomes READY/PARTIAL;
- title/summary/category/type changes;
- interest changes;
- lifecycle changes;
- source/provenance changes relevant to the projection.

Do not sync every internal technical Event if the remote projection is unchanged.

## 8. Miro mapping

V1 recommendation:

~~~text
AIInbox Item
→ one Miro card/shape
~~~

Keep rendering concise.

Suggested visible fields:

- title;
- short summary;
- category/type;
- state;
- interest/priority;
- source link.

Use provider metadata/custom data only if stable and necessary.

## 9. Placement

Start with deterministic simple placement.

For example, append cards in rows/columns within a configured board region.

Do not block V1 on semantic clustering or automatic spatial layout.

Category grouping may be added after reliable idempotent sync exists.

## 10. Remote deletion

V1 rule:

remote deletion does not delete canonical AIInbox data.

On the next sync, choose one documented behavior:
- recreate the projection; or
- mark mapping as missing and require explicit resync.

Do not infer "delete Item" from remote deletion.

## 11. AIInbox lifecycle

DONE/ARCHIVED should be represented remotely as state, styling or a destination
area if useful, but the remote object remains a projection.

Whether archived Items remain visible on the board is a connector-level
presentation choice, not a canonical lifecycle change.

## 12. Other destinations

The connector seam is justified because more than one implementation is an
explicit product requirement.

Do not over-generalize beyond the actual shared operations:

- upsert a projection;
- optionally archive/remove a projection;
- validate connection.

Provider-specific capabilities remain provider-specific.

## 13. API interaction

Server-side sync adapters use application services/read projections directly.

External third-party clients may use PM-18 HTTP API.

Do not make the server call its own HTTP API merely to feed a connector.

## 14. Security

For Miro and future platforms:

- least-privilege token/scopes;
- TLS;
- bounded payloads;
- no secrets in logs;
- no full transcript/body unless explicitly enabled later;
- no arbitrary provider-returned URL trusted without validation where it becomes
  user-facing.

## 15. Failure model

Map provider errors into a small controlled set such as:

- AUTH_REQUIRED;
- RATE_LIMITED;
- TRANSIENT_PROVIDER_FAILURE;
- INVALID_REMOTE_TARGET;
- PERMANENT_PROVIDER_REJECTION.

Retries are finite and only for transient classes.

## 16. Acceptance direction for Miro V1

Miro sync is ready when:

1. one board can be configured;
2. all existing Items can be bootstrapped in bounded batches;
3. bootstrap resumes after restart;
4. each Item maps to one stable remote object;
5. repeated sync is idempotent;
6. a changed Item updates the same remote object;
7. a new Item is synced incrementally;
8. connector outage does not block Item processing;
9. credentials do not enter canonical/exported data;
10. default tests use a deterministic fake adapter.
