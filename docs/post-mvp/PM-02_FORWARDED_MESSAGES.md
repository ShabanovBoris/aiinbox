# PM-02 — Forwarded Telegram Messages

Type: Post-MVP Epic + Detailed Technical Specification  
Status: NOT_STARTED  
Prerequisite: stabilized Telegram ingestion

## 1. Epic

### Problem

A major real-world Telegram capture flow is forwarding an existing message to the bot.

Current ingestion should not require the user to copy text or manually re-share URLs. Forwarded messages also carry useful provenance that should not be lost.

### Goal

AIInbox accepts forwarded Telegram messages as naturally as original messages while preserving available origin metadata.

Forwarding is **provenance**, not a content type.

## 2. Core invariant

Do not introduce:

~~~text
SourceType.FORWARDED
~~~

A forwarded message still resolves to its real content source:

- forwarded text → TEXT;
- forwarded URL → WEB/YOUTUBE/INSTAGRAM;
- forwarded voice → VOICE;
- forwarded audio → AUDIO;
- forwarded video → VIDEO after PM-03;
- forwarded document → DOCUMENT after PM-03.

## 3. In scope

- detect Telegram forward origin;
- persist normalized provenance metadata;
- forwarded text;
- forwarded text/caption containing URLs;
- forwarded voice/audio using existing media paths;
- origin-aware formatting where useful;
- tests for origin variants and idempotency.

PM-03 will extend the same behavior to video/documents.

## 4. Out of scope

- scraping inaccessible original Telegram messages;
- resolving hidden identities;
- joining private channels;
- reconstructing an original message URL when Telegram does not provide enough data;
- bulk-importing channel history;
- separate forwarded-message processing pipeline.

## 5. Data model

Preferred near-term addition:

~~~text
items.source_metadata_json JSON NULL
~~~

Avoid many forwarding-only nullable columns.

Example:

~~~json
{
  "forwarded": true,
  "forward_origin_type": "channel",
  "forward_source_name": "Android Developers",
  "forward_source_username": "androiddev",
  "forward_message_id": 8712,
  "original_sent_at": "2026-09-01T12:14:00Z"
}
~~~

Store only data actually supplied by Telegram.

Do not infer missing usernames/ids.

## 6. Forward origin normalization

Map Telegram forward origin into a small stable internal structure.

Expected origin classes may include:

- user;
- hidden_user;
- chat;
- channel.

Internal metadata should remain transport-neutral enough that domain code does not import aiogram types.

Telegram → plain dict/value object conversion belongs in the Telegram edge.

## 7. Text semantics

A critical distinction:

### Original forwarded content

The original forwarded message text/caption is source content.

### User note

Text intentionally added by the user around a separately captured source remains `user_note`.

Do not silently convert the forwarded author's text into “the user's note”.

For simple forwarded text, `user_note` may be empty and the Item text content is the forwarded message itself.

## 8. URL routing

Forwarded messages containing URLs must use the same canonical URL parsing/routing as ordinary messages.

Examples:

~~~text
forwarded YouTube URL
→ YOUTUBE

forwarded Instagram Reel URL
→ INSTAGRAM after PM-04

forwarded article URL
→ WEB
~~~

Do not duplicate URL-normalization or dedup logic.

## 9. Media routing

### Voice/audio

Reuse current:

~~~text
Telegram file_id
→ existing downloader
→ existing transcription
→ existing pipeline
~~~

Forward metadata accompanies the Item.

### Video/document

Until PM-03 is implemented:

- unsupported forwarded media must fail gracefully;
- do not create half-implemented source types.

After PM-03, these should work through the same handlers/extractors as non-forwarded media.

## 10. Source link

If a public channel origin includes enough information to construct a valid original Telegram link, the UI may offer:

~~~text
[Открыть оригинал]
~~~

Rules:

- never fabricate a link;
- do not assume private chat/message accessibility;
- no network lookup solely to enrich provenance in this phase.

## 11. Display

Where useful, show a compact source line:

~~~text
Источник: Android Developers
~~~

Avoid showing internal Telegram ids to the user.

## 12. Deduplication

Existing dedup semantics remain canonical:

- Telegram source identity prevents duplicate update ingestion;
- URL dedup remains per-user;
- forward metadata must not defeat URL dedup.

If the same URL is forwarded twice from different channels, current product policy for URL dedup remains in force unless deliberately changed in a separate product decision.

## 13. Privacy

Forward metadata may contain names/usernames from Telegram.

Requirements:

- store only necessary provenance;
- do not log full private forwarded text unnecessarily;
- do not expose origin metadata to other users;
- export policy later must treat it as user data.

## 14. Tests

### Forward text

- known user origin;
- hidden-user origin;
- chat origin;
- channel origin.

### Forward URL

- forwarded article uses WEB;
- forwarded YouTube uses YOUTUBE;
- duplicate URL behavior unchanged;
- origin metadata persists.

### Forward media

- voice uses VOICE path;
- audio uses AUDIO path;
- processing result does not differ merely because message was forwarded.

### Idempotency

- same Telegram update replay creates no second Item;
- metadata survives restart/reload.

### Security

- unavailable origin data is not guessed;
- public-link rendering only when fields are sufficient.

## 15. Acceptance criteria

1. User can forward ordinary Telegram text directly to AIInbox.
2. Existing URL/media routing is reused.
3. Available origin metadata survives persistence.
4. Original forwarded text is not mislabeled as `user_note`.
5. No `FORWARDED` source type exists.
6. Replay/idempotency guarantees remain intact.
7. Existing non-forwarded ingestion behaves unchanged.
8. Full quality gate passes.

## 16. Definition of Done

- migration for `source_metadata_json` if not already present;
- transport normalization implemented;
- text/URL/voice/audio paths covered;
- user-visible provenance formatting kept compact;
- tests added;
- PM-03 extension points documented but not preimplemented;
- implementation/review docs updated according to repository workflow.
