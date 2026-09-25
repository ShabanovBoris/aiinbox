# PM-19 — Android Client

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-18 HTTP API  
Status: PLANNED

## 1. Problem

Telegram is fast for capture and reminders, but becomes limiting for:

- Android Share Sheet capture;
- richer browsing/filtering;
- source/detail views;
- weekly review/statistics;
- home-screen widgets;
- client-side offline retry;
- app-native settings/history.

## 2. Goal

Build a thin native Android client over the PM-18 `/v1` API while keeping the server as the source of truth.

Telegram remains supported and continues to handle existing reminders unless a separate Android push phase is approved.

## 3. Technology

Recommended:

~~~text
Kotlin
Jetpack Compose
Coroutines
Retrofit/OkHttp or equivalent small HTTP stack
WorkManager
Android Keystore-backed secret storage
~~~

Do not embed Python business logic in the app.

## 4. Core rule

Android is a presentation/capture client.

It does not reimplement:

- PriorityEngine;
- BehaviourAffinity;
- AttentionRanking;
- Reminder policy;
- FTS/hybrid search ranking;
- Ask citation validation;
- lifecycle transition rules.

Those remain server-side.

## 5. In scope v1

- server connection setup;
- secure API token storage;
- Share Sheet text/URL capture;
- offline/retry queue for captures;
- Inbox list/detail;
- Today;
- Attention preview;
- Search;
- Ask My Inbox;
- Weekly Review;
- Done/Snooze/Archive;
- interest 1–3;
- basic Attention/settings controls;
- source opening;
- one useful home-screen widget;
- tests and release documentation.

## 6. Out of scope v1

- replacing Telegram reminder delivery;
- Firebase push infrastructure;
- voice/video/document upload;
- local LLM execution inside Android;
- full offline database mirror;
- conflict-heavy bidirectional sync;
- multi-account UI;
- public Play Store backend/service;
- Calendar integration (PM-20).

## 7. Server setup

User configures:

- AIInbox HTTPS base URL;
- API bearer token.

No model/provider credentials are entered into Android.

## 8. Token storage

Store the API token using Android Keystore-backed secure storage.

Never:

- hardcode token in APK;
- write it to logs;
- expose it in screenshots/debug UI by default;
- include it in analytics/crash breadcrumbs.

## 9. TLS

Production connection requires HTTPS.

Allow plain HTTP only for an explicit local-development mode if implemented, with a visible warning and no silent downgrade from HTTPS.

## 10. Share Sheet

Register Android share target for:

- `text/plain`;
- URLs shared as text.

Initial flow:

~~~text
Share from app/browser
→ AIInbox share activity
→ normalize text
→ create client request UUID
→ enqueue local capture
→ POST /v1/items
→ success state
~~~

## 11. Share Sheet UX

Common capture should require minimal interaction.

A valid shared URL/text can be sent immediately with an optional note field rather than forcing category/type/manual metadata.

The server remains responsible for extraction/analysis/classification.

## 12. Offline capture queue

Network loss must not discard a Share Sheet capture.

Persist locally only the small outbound request needed for retry:

- client request UUID;
- text/URL;
- optional note;
- created_at;
- retry state.

Use WorkManager to retry with PM-18 `Idempotency-Key`.

## 13. Offline queue privacy

Local queued capture may contain sensitive text.

Use app-private storage; do not expose it through public files/media storage.

Delete queue entry after server acknowledges the idempotent capture.

## 14. No local canonical Item database v1

The server is canonical.

A bounded UI cache is acceptable, but do not implement complex sync/conflict resolution before a real offline browsing requirement appears.

## 15. Main navigation

Recommended top-level screens:

~~~text
Inbox
Today
Attention
Search / Ask
Weekly
Settings
~~~

Exact navigation can be adapted to Android conventions.

## 16. Inbox

Use paginated `/v1/items`.

Support practical filters already exposed by the API:

- state;
- category;
- ItemType.

Do not invent client-only categories or ranking.

## 17. Item detail

Show server-projected canonical fields:

- title;
- summary;
- category/type;
- priority;
- interest;
- state;
- next action;
- source provenance/URLs;
- created/due timestamps where relevant.

Long transcript/document content can be a later detail endpoint/expansion if PM-18 supports it safely.

## 18. Item actions

Use API lifecycle endpoints.

Optimistic UI is allowed only if failure cleanly rolls back/reloads canonical state.

Never manufacture local DONE/SNOOZED state without server confirmation as final truth.

## 19. Today

Render `/v1/today` as server order.

Do not re-sort by Android preferences.

## 20. Attention

Render `/v1/attention` as server order with explanation fields provided by the API.

Fetching Attention remains a read-only preview under PM-18 v1.

## 21. Search

Use `/v1/search`.

If PM-14 leaves user-facing search lexical, Android does the same.

Do not add local semantic search over cached Items.

## 22. Ask

Flow:

~~~text
POST /v1/ask
→ 202 + ask id
→ poll GET /v1/asks/{id}
→ render answer + validated references
~~~

Polling must be bounded/backed off.

Do not call any LLM provider directly from Android.

## 23. Ask references

Titles/URLs come from API validated results.

Open HTTP(S) links through Android intents/custom tabs.

Do not reinterpret source ids client-side as authorization.

## 24. Weekly

Render `/v1/weekly` facts/recommendations.

The client may use richer layout than Telegram, but must not invent extra metrics or performance judgements.

## 25. Settings

Initial settings UI may expose:

- timezone;
- daily digest toggle/time;
- quiet hours;
- Attention enabled;
- Attention intensity 1–5;
- generic motivation enabled.

Operator/provider routing settings remain server configuration and are not exposed.

## 26. Telegram coexistence

Android does not disable Telegram automatically.

Telegram remains:

- supported capture source;
- existing proactive reminder channel;
- source of historical Items.

The same Items/actions appear through API because storage is shared.

## 27. Notifications

No Android push in PM-19 v1.

Do not build FCM only to duplicate Telegram notifications.

A future phase may add a transport preference once native-client value is proven.

## 28. Widget

Implement one high-value widget rather than many.

Recommended initial widget:

~~~text
Today / top Attention Items
~~~

It should:

- fetch bounded server data through WorkManager;
- show a small number of Items;
- open the app to the relevant Item/list;
- avoid displaying highly sensitive long source content on the lock/home screen.

## 29. Widget ranking

Widget displays server-provided order.

No client-side priority formula.

## 30. Refresh

Use pull-to-refresh/manual refresh plus bounded background refresh for widget/capture retry.

Do not continuously poll the server in foreground/background.

## 31. Deep links

Internal app navigation may support:

~~~text
aiinbox://item/<id>
aiinbox://ask/<id>
~~~

These are local app links only; server authorization remains required before showing private data.

## 32. Error handling

Distinguish at minimum:

- unauthorized/token invalid;
- server unavailable;
- request validation;
- pending Ask;
- failed Ask;
- Item no longer exists;
- transient network failure.

Do not show raw HTTP stack traces.

## 33. Connection screen

Provide clear test-connection behavior:

- base URL valid;
- TLS/auth success;
- API version supported.

Do not ask for Telegram bot token.

## 34. API version compatibility

Client targets `/v1` explicitly.

On unsupported API response/version, show a controlled upgrade/compatibility error rather than continuing with guessed schemas.

## 35. No analytics requirement

Do not add third-party behavioral analytics by default.

If crash reporting is later enabled, scrub:

- token;
- source content;
- question/answer text;
- Item summaries/titles where possible.

## 36. Accessibility

Compose UI should provide:

- content descriptions for action buttons;
- scalable text;
- usable contrast;
- non-color-only state indicators.

## 37. Tests — networking

Use a fake/mock server contract.

Cover:

- auth header;
- base URL;
- common error envelope;
- pagination;
- API version path.

## 38. Tests — Share Sheet

Cover:

- shared text;
- shared URL;
- optional note;
- offline enqueue;
- WorkManager retry;
- stable Idempotency-Key across retries;
- successful queue cleanup.

## 39. Tests — actions

Cover:

- Done;
- Snooze;
- Archive;
- Interest 1/2/3;
- failure rollback/reload.

## 40. Tests — Ask

Cover:

- enqueue;
- pending polling;
- DONE response;
- failed response;
- source opening;
- bounded polling/backoff.

## 41. Tests — widget

Cover:

- bounded item count;
- server order preserved;
- stale cached state handling;
- unauthorized/server failure safe state.

## 42. Server contract integration

CI or a dedicated integration suite should validate generated/current PM-18 OpenAPI or a running test server against Android DTOs.

Avoid hand-maintained duplicate schemas drifting silently.

## 43. Documentation

Provide:

- connection setup;
- token setup;
- HTTPS recommendation;
- Share Sheet behavior;
- offline queue behavior;
- supported feature matrix vs Telegram;
- troubleshooting.

## 44. Acceptance criteria

1. Android connects to authenticated PM-18 `/v1` API.
2. API token is Keystore-protected.
3. Share Sheet captures text/URL.
4. Offline Share capture is durable locally.
5. Retry uses stable idempotency key.
6. Server remains canonical.
7. Inbox/detail work.
8. Today uses server ranking.
9. Attention uses server ranking.
10. Search uses server search semantics.
11. Ask uses server durable Ask jobs.
12. Weekly displays server facts.
13. Done/Snooze/Archive/Interest use API actions.
14. Basic attention/settings controls work.
15. Telegram remains supported and is not automatically disabled.
16. No direct LLM/cloud-provider credential exists in Android.
17. No Android push infrastructure is introduced in v1.
18. At least one useful widget works.
19. Tests cover offline capture and API contract.

## 45. Definition of Done

~~~text
Android Share Sheet / UI
        ↓
local thin client + secure token
        ↓
PM-18 /v1 API
        ↓
existing AIInbox services/workers/storage
        ↓
Telegram continues in parallel
~~~

PM-19 is complete when Android removes the main Telegram UX constraints without creating a second copy of AIInbox business logic.