# PM-33 — Browser Extension

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-18 HTTP API  
Status: IN_REVIEW

## 1. Problem

Telegram is still the lowest-friction existing capture surface, but saving material
from a desktop browser requires context switching. PM-18 now provides a stable,
authenticated, idempotent HTTP capture contract, so the next client should remove
that friction without creating another business-logic implementation.

The extension is a capture client, not a browser automation agent.

## 2. Current server baseline

PM-18 is DONE in PR #56.

The current API already provides:

~~~text
POST /v1/items
GET  /v1/items/{id}
GET  /v1/items
GET  /v1/search
GET  /v1/today
GET  /v1/attention
GET  /v1/weekly
POST /v1/ask
GET  /v1/asks/{id}
lifecycle/settings routes
~~~

Capture is asynchronous and requires:

~~~text
Authorization: Bearer <token>
Idempotency-Key: <opaque key>
~~~

Request:

~~~json
{
  "text": "captured text and/or URL",
  "user_note": "optional explicit note"
}
~~~

Current bounds:

- capture text: 20,000 characters;
- user note: 2,000 characters;
- HTTP request body: 64 KiB;
- create retries are deduplicated durably by the server;
- HTTP-created Items do not produce Telegram READY/FAILED deliveries;
- Item status is pollable with GET /v1/items/{id}.

PM-33 must use this contract rather than invent an extension-specific ingestion
endpoint.

## 3. Goal

Make saving a page or selected text to AIInbox take one deliberate browser action.

Primary flows:

~~~text
Toolbar button
→ Save current page
→ optional note
→ AIInbox

Text selection
→ context menu "Save to AIInbox"
→ selected text + page URL
→ AIInbox
~~~

The extension must remain useful during a temporary server/network outage by
persisting capture intents locally and retrying with the same idempotency key.

## 4. Target platform

V1 target:

- Chromium-based browsers;
- Manifest V3;
- standard WebExtensions-compatible architecture where practical.

Firefox support may follow after V1 behavior is stable. Do not weaken V1 security
or create a large compatibility abstraction before a second browser is actively
supported.

## 5. Architecture

~~~text
page
  │
  │ user gesture only
  ▼
popup / context-menu handler
  │
  ▼
extension service worker
  │
  ├── local durable pending queue
  ├── API credential access
  └── retry/status coordination
  │
  ▼
PM-18 HTTPS API
  │
  ▼
canonical AIInbox Item
~~~

The content page never owns the API token.

## 6. Recommended repository location

Keep the first-party extension in the same repository unless deployment workflow
later proves otherwise:

~~~text
clients/browser-extension/
~~~

Suggested structure:

~~~text
clients/browser-extension/
  manifest.json
  src/
    service-worker.*
    popup.*
    options.*
    capture.*
    api.*
    storage.*
  tests/
  icons/
  package.json
  README.md
~~~

Do not place browser business logic in the Python server.

## 7. Minimum permissions

Prefer the smallest permission set required by V1:

- storage;
- contextMenus;
- activeTab;
- scripting only when needed to read an explicit user selection;
- alarms for durable retry scheduling.

Avoid persistent broad page access.

Use activeTab/user gestures instead of a permanent content script on every site.

For the AIInbox server origin, request only the exact host permission needed for
the configured API base URL. If dynamic configuration requires broad optional
host patterns, request the exact selected origin at runtime rather than granting
all optional origins automatically.

## 8. No background page scraping

PM-33 does not:

- crawl sites;
- follow links;
- inject code continuously;
- monitor browsing history;
- capture page content without an explicit user action;
- run unrestricted page JavaScript.

The server remains responsible for URL extraction/fetching under its existing
SSRF and content limits.

## 9. Save-current-page flow

For an ordinary HTTP(S) page:

~~~json
{
  "text": "https://example.com/current-page",
  "user_note": null
}
~~~

The extension should not download and upload the complete DOM merely because it
can.

The server already knows how to fetch supported URLs safely.

## 10. Save-selection flow

For selected text:

~~~text
<selected text>

<page URL>
~~~

is sent in the request text.

An explicitly typed note remains separate in user_note.

This preserves:

- selected source material as source text;
- page provenance as a URL;
- explicit user intent as user_note.

Do not silently treat selected page text as a personal note.

## 11. Restricted pages

On pages where browser APIs forbid script injection, such as browser-internal
pages, the extension must fail cleanly.

If a normal HTTP(S) page URL is still available, saving the URL alone may remain
available.

Never attempt to bypass browser security restrictions.

## 12. Local extension configuration

V1 settings:

- AIInbox API base URL;
- API bearer token;
- connection status.

Optional convenience:

- default popup behavior;
- enable/disable context-menu action.

Do not duplicate AIInbox user preferences such as Attention intensity or quiet
hours inside extension storage.

## 13. API URL policy

Allowed examples:

~~~text
http://127.0.0.1:8080
http://localhost:8080
https://aiinbox.example.net
~~~

Reject plaintext HTTP to non-loopback hosts by default.

Production remote use should require HTTPS because PM-18 uses bearer
authentication.

## 14. Credential isolation

The API token:

- is stored only in extension-owned storage;
- is read only by trusted extension contexts;
- is never inserted into page DOM;
- is never sent to a content script;
- is never included in console messages;
- is never included in telemetry;
- is never placed in an idempotency key.

Where the browser supports restricting storage access to trusted extension
contexts, use it.

## 15. No remote code

The extension package must contain its executable code.

Do not fetch and execute JavaScript from AIInbox or another remote server.

## 16. Local durable capture queue

A capture intent must survive service-worker suspension and browser restart.

Conceptual local record:

~~~json
{
  "local_id": "uuid",
  "idempotency_key": "uuid",
  "text": "...",
  "user_note": "...",
  "created_at": "...",
  "attempts": 0,
  "next_attempt_at": "...",
  "state": "PENDING"
}
~~~

Use browser extension local storage, not an in-memory queue.

## 17. Idempotency

Generate one idempotency key when the local capture intent is created.

~~~text
crypto.randomUUID()
~~~

or equivalent cryptographically strong UUID generation is appropriate.

Every retry of that exact capture uses the same key.

Never generate a fresh key merely because:

- the service worker restarted;
- the HTTP request timed out;
- the response was lost;
- the machine went offline.

This lets PM-18 resolve ambiguous network outcomes safely.

## 18. Idempotency conflict

HTTP:

~~~text
409 IDEMPOTENCY_CONFLICT
~~~

means the local record is corrupted or a key has been reused for different
content.

Do not retry automatically.

Show a bounded actionable error and retain the local record for inspection.

## 19. Retry classes

Retry automatically:

- network unavailable;
- connection timeout;
- HTTP 408 if produced by infrastructure;
- HTTP 429;
- controlled 5xx/503.

Do not automatically retry forever:

- 401;
- 409;
- 413;
- 422.

Use bounded exponential backoff with jitter.

Keep the queue visible so the user can manually retry after correcting
configuration.

## 20. Offline semantics

Offline save must feel successful only as a local queue action.

Use distinct UI wording/states:

~~~text
Saved locally — waiting for AIInbox
Accepted by AIInbox
Processing
Ready
Failed
~~~

Do not say "saved to AIInbox" before POST /v1/items is durably accepted.

## 21. Server acceptance

On 202:

- record returned Item id;
- persist Location if useful;
- remove private capture payload from the pending retry queue;
- retain only a small recent-status record needed for UI.

The canonical copy is now the server Item.

## 22. Processing status

The extension may poll:

~~~text
GET /v1/items/{id}
~~~

after acceptance.

Avoid continuous aggressive background polling.

Recommended behavior:

- a few short polls immediately after capture;
- refresh status when popup opens;
- optionally use a low-frequency alarm for recent pending server Items;
- stop automatic polling at READY or FAILED.

## 23. Popup V1

The popup should be intentionally small:

- current page title/URL preview;
- optional note;
- Save button;
- current connection state;
- latest capture state;
- pending-local count if non-zero.

Do not build Inbox/Search/Ask into V1.

Those already exist through API but are not required to validate the capture
surface.

## 24. Context menu V1

Add:

~~~text
Save to AIInbox
~~~

for:

- page;
- selection.

Selection capture should include the current page URL when available.

## 25. Keyboard shortcut

A keyboard shortcut for Save current page is useful but optional for V1.

If added, it must invoke the same capture function and local idempotency queue as
the popup/context menu.

No parallel save implementation.

## 26. Extension boundary

The extension owns:

- browser UI;
- current page/selection acquisition;
- local retry queue;
- API transport;
- local credentials.

The server owns:

- URL normalization;
- source routing;
- duplicate retry identity;
- extraction;
- LLM analysis;
- classification;
- ranking;
- search;
- canonical lifecycle.

## 27. Browser title

Do not persist the browser tab title as canonical Item.title.

The analyzed Item.title remains server-owned.

The browser title may be shown as an ephemeral local preview.

## 28. Page HTML

Do not upload complete HTML in V1.

Potential later explicit features such as "save readable snapshot" require a
separate privacy/size design.

## 29. Authentication failure UX

401 should:

- mark connection as unauthenticated;
- stop automatic capture retries until credentials change;
- retain pending local captures;
- offer "Test connection".

Do not delete pending material because the token expired/changed.

## 30. Health check

"Test connection" should call:

~~~text
GET /healthz
~~~

first for reachability and then one authenticated lightweight endpoint to verify
the token.

A healthy public /healthz alone does not prove credentials are valid.

## 31. PM-18 CORS boundary

PM-18 intentionally does not ship permissive CORS.

The extension must use extension host permissions and make API requests from a
trusted extension context.

Do not solve extension networking by changing the server to:

~~~text
Access-Control-Allow-Origin: *
~~~

## 32. Privacy

Never log:

- bearer token;
- selected text;
- user_note;
- full capture body.

Developer-mode logs may contain:

- local queue id;
- HTTP status;
- Item id;
- retry count;
- generic error class.

## 33. Telemetry

No external analytics SDK is required in V1.

If product analytics are later added, they require an explicit separate privacy
decision.

## 34. Build/release

The repository must document:

- supported browser/version family;
- local unpacked install;
- production build;
- generated package path;
- API configuration;
- permissions requested;
- update procedure.

Pin JavaScript dependencies through the chosen package lock.

Avoid a heavy frontend framework for the small V1 surface unless it clearly
reduces complexity.

## 35. Tests — local capture queue

Cover:

- new capture creates one UUID/idempotency key;
- worker restart preserves pending intent;
- retry preserves key;
- accepted capture removes payload from retry queue;
- 401 pauses retries;
- 409 becomes terminal local error;
- network failure remains retryable.

## 36. Tests — capture composition

Cover:

- page URL only;
- selected text + URL;
- explicit note separate from selected text;
- unsupported browser URL;
- empty selection fallback;
- oversized server input gets controlled UI error.

## 37. Tests — credential boundary

Cover:

- token is not passed to injected page code;
- token absent from logs;
- unauthenticated response does not delete queued captures.

## 38. Tests — PM-18 contract

Use a fake/mock HTTP transport for default extension tests.

At least one integration smoke may run against a local PM-18 test server:

~~~text
extension capture
→ POST /v1/items
→ 202
→ GET /v1/items/{id}
~~~

No public internet is required by the default suite.

## 39. Explicit non-goals

Not PM-33 V1:

- browser Inbox;
- Ask UI;
- Attention UI;
- full page snapshot upload;
- screenshots;
- video/file upload;
- background browsing;
- automatic capture rules;
- page modification;
- password/account system;
- Miro;
- general connector framework.

## 40. Acceptance criteria

1. V1 is a browser capture client over PM-18.
2. Save current HTTP(S) page works.
3. Save selected text with page URL works.
4. Optional explicit note stays separate.
5. No capture occurs without a user action.
6. API token never enters page JavaScript.
7. API token is not logged.
8. No permissive server CORS change is required.
9. Pending capture survives service-worker/browser restart.
10. Every local intent gets one stable idempotency key.
11. Ambiguous network retry cannot create duplicate server Items.
12. 401/409/413/422 are not infinite-retry conditions.
13. Temporary network/5xx failures are retryable.
14. 202 stores the canonical server Item id.
15. Server status can be inspected via GET /v1/items/{id}.
16. Local UI distinguishes queued-local vs server-accepted.
17. No DOM snapshot is uploaded in V1.
18. No broad permanent page access is required.
19. PM-18 remains the only server ingestion contract.
20. Browser code contains no server business rules.

## 41. Definition of Done

~~~text
browser user gesture
        ↓
page URL / selection / explicit note
        ↓
durable local capture intent
        ↓ same Idempotency-Key across retries
PM-18 POST /v1/items
        ↓
canonical Item
        ↓
ProcessingWorker
        ↓
READY / FAILED
        ↓
GET /v1/items/{id}
~~~

PM-33 is complete when saving from the browser is materially easier than
switching to Telegram, while server ownership, processing semantics, security and
idempotency remain unchanged.

## 42. External implementation references

Use current browser vendor documentation during implementation rather than
copying stale API assumptions into code.

Relevant current concepts:

- Chromium Manifest V3;
- activeTab / scripting;
- optional host permissions;
- extension storage;
- service-worker lifecycle.

Official reference root:

~~~text
https://developer.chrome.com/docs/extensions/
~~~
