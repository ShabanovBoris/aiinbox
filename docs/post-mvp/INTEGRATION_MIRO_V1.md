# Miro Connector v1

Type: Post-MVP Integration Epic + Detailed Technical Specification  
Prerequisite: Integration Sync Foundation  
Status: PLANNED — FIRST OUTBOUND DESTINATION

## 1. Goal

Mirror every canonical AIInbox Item into one configured Miro board without making
Miro a second source of truth.

Direction:

~~~text
AIInbox
  ↓
IntegrationProjection
  ↓
Miro REST API
  ↓
one stable Miro item per AIInbox Item
~~~

V1 is one-way.

## 2. Current Miro platform assumptions

Implementation must verify current Miro developer documentation before coding.

At the time this specification was prepared:

- Miro REST API v2 is the current REST surface;
- Miro applications use OAuth 2.0 authorization code flow;
- creating sticky notes requires board write permission;
- REST calls are rate-limited and 429 responses must be handled;
- Miro exposes stable remote item ids after creation.

Do not hardcode undocumented rate limits because Miro may change them.

Official references:

~~~text
https://developers.miro.com/reference/overview
https://developers.miro.com/reference/create-sticky-note-item-1
https://developers.miro.com/reference/rate-limiting
~~~

## 3. Representation choice

V1 uses:

~~~text
AIInbox Item
→ Miro sticky note
~~~

Reason:

- simple visual object;
- native board positioning;
- create/read/update/delete support;
- adequate for concise personal knowledge projection;
- low provider-specific complexity.

Do not use app cards, embeds or a Miro Web SDK app in V1 unless the actual REST
contract proves sticky notes insufficient.

## 4. One board per connection

One IntegrationConnection targets exactly one board in V1.

Non-secret target metadata:

~~~json
{
  "board_id": "..."
}
~~~

Multiple boards can later be represented as multiple connections.

Do not put board selection rules into Item business logic.

## 5. OAuth

Miro connection uses OAuth 2.0 authorization code flow.

Operator configuration:

~~~text
MIRO_CLIENT_ID
MIRO_CLIENT_SECRET
MIRO_REDIRECT_URI
~~~

remains environment/secret configuration.

Dynamic access/refresh token material uses the integration credential store from
the foundation.

Tokens never enter:

- Item;
- Content;
- Event;
- Profile;
- settings_json;
- PM-15 export;
- logs;
- LLM context.

## 6. OAuth management flow

A protected AIInbox integration-management action may initiate authorization:

~~~text
authenticated AIInbox user
→ request Miro connect
→ server generates state
→ authorization URL
→ Miro consent
→ callback
→ validate state
→ exchange authorization code
→ persist secret token material
→ create/enable IntegrationConnection
~~~

The callback cannot rely on the AIInbox bearer token being sent by Miro.

It must be protected by a short-lived, single-use OAuth state value tied to the
pending connection request.

Do not accept a callback without state validation.

## 7. OAuth scopes

Request the minimum current Miro scopes required by the exact endpoints used.

At minimum the connector needs permission to create/update board items on the
selected board.

Do not request team/admin scopes merely for convenience.

If listing boards requires an additional read scope, request it only when board
selection is implemented through the API.

## 8. Credential refresh

If Miro issues expiring access tokens and refresh tokens, refresh must be:

- automatic according to provider rules;
- serialized per connection;
- persisted atomically;
- bounded on failure;
- classified as AUTH_REQUIRED when refresh is no longer possible.

Do not let several Item sync tasks race token refresh.

## 9. Connection validation

Before bootstrap:

- verify credentials;
- verify target board exists;
- verify the authorized app can write to it.

Failure classes:

~~~text
INVALID_REMOTE_TARGET
AUTH_REQUIRED
~~~

must stop bootstrap without affecting core AIInbox operation.

## 10. Sticky content

Keep the sticky concise and useful.

Recommended projection:

~~~text
<Title>

<short summary>

Type: <item_type>
Category: <category>
State: <state>
Interest: <1..3>
Priority: <priority_score>

Source: <safe URL when useful>
AIInbox ID: <item_id>
~~~

Exact typography may be adapted to Miro-supported content format.

Do not include internal diagnostic fields.

## 11. Summary bounds

Remote summary should remain smaller than the canonical Item summary.

Recommended:

~~~text
max 500 visible summary chars
~~~

Do not push:

- transcript;
- full document text;
- Ask output;
- raw tags dump;
- internal error_message.

## 12. AIInbox marker

Every Miro projection needs a stable recovery marker.

Visible fallback:

~~~text
AIInbox ID: 123
~~~

If Miro provides stable app metadata/custom data suitable for REST-created items,
prefer a non-visible structured marker.

Implementation must confirm actual supported metadata before relying on it.

Never encode secrets in the marker.

## 13. Remote mapping

Canonical local mapping:

~~~text
IntegrationProjection.remote_object_id
~~~

stores the Miro sticky note id.

Normal update path:

~~~text
remote_object_id exists
→ update same sticky
~~~

Do not create a fresh note for every canonical Item update.

## 14. Crash after remote create

Critical ambiguity:

~~~text
Miro create succeeds
process dies
before remote_object_id commit
~~~

V1 must have a recovery path.

Preferred strategy:

1. use/search a stable AIInbox marker if Miro API permits bounded lookup;
2. otherwise implement provider-specific bounded board reconciliation;
3. document any residual duplicate window and provide repair tooling.

Do not pretend HTTP retry alone solves this because the remote provider does not
share AIInbox idempotency state.

## 15. Board layout

Start simple.

Use deterministic grid placement.

Conceptual:

~~~text
column = slot % columns
row    = floor(slot / columns)

x = origin_x + column * horizontal_spacing
y = origin_y + row    * vertical_spacing
~~~

Persist the chosen slot/position in IntegrationProjection.placement_json.

Updates preserve existing position.

## 16. Placement allocation

Allocate placement locally before remote creation.

Requirements:

- deterministic;
- restart-safe;
- no two Items intentionally get the same slot in one connection;
- no full-board scan merely to find free space.

A monotonic connection-local next slot is acceptable.

## 17. User movement

If the user manually moves a sticky note in Miro, V1 should not reset its position
on every content update.

Therefore update existing content without sending position unless placement is
being repaired/recreated.

AIInbox does not need to read the moved position back into canonical state.

## 18. Category grouping

Not required for V1.

Possible later enhancements:

- frames per category;
- Miro tags;
- lanes by lifecycle.

Do not block reliable bootstrap/upsert on automatic organization.

## 19. State representation

Represent canonical lifecycle visibly as text in V1.

Potential later styling may distinguish:

- ACTIVE;
- SNOOZED;
- DONE;
- ARCHIVED.

Style remains presentation only.

## 20. Processing status

Because all saves should sync, a new Item may initially appear before analysis.

For QUEUED/PROCESSING:

~~~text
Title: deterministic fallback
Status: Processing
~~~

When READY:

- update the same sticky;
- add analyzed title/summary/category/type/priority.

When FAILED:

- keep the save visible;
- show a safe user-facing failed-processing state;
- do not expose raw internal error details.

## 21. Source links

Include only safe user-facing source URLs.

For multiple sources, V1 may include the first useful safe source or a small
bounded list.

Do not overflow the sticky with all child sources.

## 22. Link back to AIInbox

PM-18 exposes API resources, not a human-facing web Item page.

Do not invent a clickable web deep link until such a UI exists.

The projection may show AIInbox ID.

When a future web client provides stable human URLs, the connector can add them.

## 23. Full bootstrap

First successful connection triggers:

~~~text
all existing Items
→ bounded IntegrationProjection creation
→ Miro upserts
~~~

Every lifecycle and processing state is included.

Progress survives restart.

## 24. Bootstrap status

Expose at least operator-visible:

~~~text
bootstrap pending
bootstrap active
X synced
Y pending
Z failed
~~~

No rich Telegram UI is required in V1.

Protected integration status or logs are sufficient.

## 25. Incremental updates

After bootstrap, canonical changes produce or repair PENDING projections.

Miro updates reflect changes to:

- title/fallback title;
- summary;
- category;
- item_type;
- processing status;
- lifecycle state;
- interest;
- priority;
- safe source links.

No provider call is required if projection hash is unchanged.

## 26. Remote deletion

One-way rule:

~~~text
deleting a Miro sticky never deletes the AIInbox Item
~~~

If an update gets provider 404 for the mapped sticky:

- classify REMOTE_MISSING;
- recreate a sticky for the canonical Item;
- update remote_object_id;
- preserve canonical Item.

This recreation behavior must be documented to the user.

## 27. Remote content edits

If the user edits Miro sticky text manually, AIInbox does not ingest it.

A later canonical AIInbox update may overwrite the sticky text.

This is expected V1 behavior.

Do not add webhook-based conflict resolution.

## 28. Rate limiting

Miro REST API uses rate limits and can return 429.

The adapter must:

- classify 429 as RATE_LIMITED;
- honor provider reset/retry information when available;
- use bounded backoff;
- avoid hot-loop retries;
- continue syncing other connections when possible.

Do not hardcode an assumed requests-per-minute value into business logic.

## 29. HTTP client

Use an explicit timeout.

The existing httpx dependency is likely sufficient for Miro REST calls.

Do not add a provider SDK unless it materially improves OAuth correctness or API
maintenance.

## 30. Payload sanitization

Do not persist/log raw provider responses containing private board content or
token information.

Persist only:

- safe error class;
- bounded operator-safe message if needed;
- HTTP status;
- retry timing.

## 31. Miro adapter boundary

Conceptually:

~~~text
validate_connection(connection)
create_sticky(projection, placement)
update_sticky(remote_object_id, projection)
get_sticky(remote_object_id) only when recovery needs it
~~~

Map provider-specific errors inside the adapter.

The foundation should not know Miro endpoint paths.

## 32. No Miro Web SDK

This connector is server-side and uses REST.

Do not build an in-board frontend/Web SDK application for V1.

That is a separate product surface and unnecessary for one-way projection.

## 33. Tests — fake Miro

Default tests use fake HTTP responses.

Cover:

- OAuth state validation;
- token exchange abstraction;
- token refresh;
- board validation;
- create sticky;
- update sticky;
- 404 mapped to REMOTE_MISSING;
- 401 mapped to AUTH_REQUIRED;
- 429 mapped to RATE_LIMITED;
- 5xx retry;
- malformed provider response.

No live Miro account required in CI.

## 34. Tests — mapping

Cover:

- one Item creates one sticky;
- second sync updates same remote id;
- unchanged projection causes no request;
- remote deletion triggers documented recreate path;
- user-moved position is not overwritten on content update;
- bootstrap includes DONE/ARCHIVED/FAILED Items.

## 35. Tests — restart ambiguity

Simulate:

~~~text
remote create succeeded
local finalize failed
~~~

Verify the chosen marker/reconciliation mechanism prevents or repairs duplicate
remote objects.

This is a blocker-level connector test.

## 36. Live verification

Before merge, if Miro credentials are available, perform a manual smoke on a test
board:

1. authorize;
2. validate board;
3. sync several existing Items;
4. change one Item;
5. verify same sticky updates;
6. restart worker;
7. verify no duplicates;
8. trigger/simulate rate-limit or auth failure if practical.

Live Miro access is not required by the default suite.

## 37. Explicit non-goals

Not Miro V1:

- two-way Item edits;
- delete AIInbox Item from Miro;
- sync Miro comments;
- Miro mentions;
- collaborative conflict handling;
- semantic board clustering;
- LLM board layout;
- full transcripts;
- screenshots/images;
- multiple boards in one connection;
- arbitrary Miro object templates.

## 38. Acceptance criteria

1. OAuth authorization is state-protected.
2. Credentials are outside canonical Item data/export.
3. One configured connection maps to one board.
4. All existing Items can bootstrap.
5. Bootstrap survives restart.
6. One AIInbox Item maps to one stable Miro object.
7. Canonical update updates that same object.
8. All Item lifecycle states can be represented.
9. QUEUED/PROCESSING Items can be projected and enriched later.
10. Provider calls occur outside SQLite write transactions.
11. Miro outage does not block AIInbox.
12. 429 is handled without hot looping.
13. Remote delete never deletes AIInbox.
14. Missing remote object follows documented recreate behavior.
15. User-moved board position is preserved during content update.
16. Full content/transcripts are not synced by default.
17. Provider response bodies/tokens are not logged.
18. Default tests use a fake Miro transport.
19. Crash-after-create ambiguity has a tested recovery path.
20. No Miro business rule leaks into Item processing/actions.

## 39. Definition of Done

~~~text
AIInbox Item
    ↓
ItemSyncProjection
    ↓
IntegrationProjection PENDING
    ↓
Miro adapter
    ↓
sticky note create/update
    ↓
remote_object_id + synced_hash
~~~

Miro V1 is complete when the board behaves as a reliable visual mirror of the
entire AIInbox archive without becoming a second editable database.
