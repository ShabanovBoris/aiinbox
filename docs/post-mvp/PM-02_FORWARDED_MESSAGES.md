# PM-02 — Forwarded Telegram Messages

Type: Post-MVP Epic + Detailed Technical Specification  
Status: DONE
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
- forwarded video → VIDEO;
- forwarded document → DOCUMENT (implemented by PM-03).

## 3. In scope

- detect Telegram forward origin;
- persist normalized provenance metadata;
- forwarded text;
- forwarded text/caption containing URLs;
- forwarded photo-post captions containing visible or hidden `text_link` URLs;
- forwarded voice/audio/video using existing media paths;
- origin-aware formatting where useful;
- tests for origin variants and idempotency.

PM-03 extends the same behavior to native media; video is now wired through the
shared ItemSource contract, while document extraction remains pending.

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

Forwarded messages use the same canonical URL parser, but the Telegram message
boundary remains authoritative: one forwarded post creates one Item.

Examples:

~~~text
forwarded YouTube URL
→ YOUTUBE

forwarded Instagram Reel URL
→ INSTAGRAM after PM-04

forwarded article URL
→ WEB

forwarded post with multiple URLs
→ one TEXT Item containing the complete post text and all URLs
~~~

Do not duplicate URL-normalization logic. URL identity is local to the Telegram
message: the same URL in two different posts may carry different source context
and therefore belongs to two different Items.

After extraction, one final analysis synthesizes the whole Item. For multiple
successful sources, title/summary must represent every substantive source and the
original post text/caption; later sources must not disappear merely because an
earlier source is longer or more prominent.

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

Forwarded video uses the same `VIDEO` ItemSource/extractor as direct video, including
video that Telegram transports as a `Document` with video MIME/extension. Caption
and URLs remain siblings in the same Item, and a failed video source may degrade
to a partial analysis when useful caption/URL content remains.

Non-video document extraction is still pending; unsupported document messages fail gracefully.

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
- repeated URL inside one message is represented once as an ItemSource;
- the same URL in different messages does not merge Items;
- forward metadata does not alter message identity.

The Item boundary is the Telegram message/post, because surrounding text and the
set of attached sources are part of its meaning.

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
- multi-URL forwarded post creates exactly one TEXT Item;
- complete forwarded text, including URLs, survives persistence and analysis;
- replay of the same Telegram update creates no second Item;
- origin metadata persists.

### Forward media

- voice uses VOICE path;
- audio uses AUDIO path;
- video uses VIDEO transcript + optional vision path;
- media failure with usable caption/source siblings yields partial analysis;
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
7. Direct and forwarded messages share the same one-message-one-Item aggregation semantics.
8. Full quality gate passes.

## 16. Definition of Done

- migration for `source_metadata_json` if not already present;
- transport normalization implemented;
- text/URL/voice/audio/video paths covered;
- user-visible provenance formatting kept compact;
- tests added;
- document extension remains in PM-03 without a parallel processing pipeline;
- implementation/review docs updated according to repository workflow.
