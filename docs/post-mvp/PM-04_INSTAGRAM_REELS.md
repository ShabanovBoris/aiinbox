# PM-04 — Instagram Reels

Type: Post-MVP Epic + Detailed Technical Specification  
Status: DONE
Prerequisites: existing yt-dlp/media pipeline; PM-03 media reuse preferred

## 1. Epic

### Problem

Instagram Reels are a common “save for later” information source. A Reel may contain useful spoken content, visual demonstrations, captions and creator context, but a generic web extractor cannot reliably understand it.

### Goal

A user can send a supported Instagram Reel URL and AIInbox will, where platform access permits:

- recognize it before generic web routing;
- extract useful metadata;
- obtain playable media/audio;
- transcribe spoken content;
- analyze representative frames when vision is enabled;
- persist derived content;
- run the existing Analyzer/PriorityEngine;
- fail transparently when Instagram access requires auth or is rate-limited.

## 2. Scope

Initial supported URL family:

~~~text
https://www.instagram.com/reel/...
~~~

Optionally accept canonical variants that yt-dlp can confidently identify.

One URL = one Reel.

## 3. Out of scope

- profile crawling;
- stories;
- feed synchronization;
- comments;
- follower data;
- private-account bypass;
- account automation;
- bulk download;
- circumventing platform protections;
- CAPTCHA bypass;
- harvesting browser cookies automatically;
- “make Instagram always work” hacks outside the supported extractor/auth boundary.

## 4. Source type

Add:

~~~text
SourceType.INSTAGRAM
~~~

Do not create a generic SOCIAL_VIDEO framework in this phase.

A generic layer may be justified only after a second real provider needs the same behavior and the commonality is proven.

## 5. URL routing

Instagram Reel detection must occur before generic WEB routing.

Conceptually:

~~~text
URL
├─ YouTube? → YOUTUBE
├─ Instagram Reel? → INSTAGRAM
└─ otherwise → WEB
~~~

Reuse canonical URL normalization and message-local ItemSource dedup logic.

Preserve meaningful path/query components needed by Instagram/yt-dlp.

## 6. Extraction pipeline

~~~text
Instagram Reel URL
      ↓
yt-dlp metadata/extraction
      ↓
title/caption/creator/duration/canonical URL
      ↓
media availability?
      ↓
download media/audio
      ↓
┌──────────────┬─────────────────┐
│              │                 │
STT            representative frames
│              │
transcript     vision notes
└──────────────┴─────────────────┘
      ↓
NormalizedContent
      ↓
existing Analyzer
      ↓
PriorityEngine
~~~

## 7. Metadata

Persist useful metadata supplied by the extractor where available:

- creator/display name;
- uploader/username;
- Reel title/caption excerpt;
- duration;
- canonical URL;
- extraction method/version if useful for diagnosis.

Do not rely on all fields always being present.

## 8. Text composition

Normalized content should prefer actual spoken transcript as the main text when available.

Caption/description should be preserved separately or included as contextual metadata.

Do not pretend caption text is a transcript.

## 9. STT

If yt-dlp yields no trustworthy transcript/subtitle path, download audio/media and use the existing TranscriptionProvider.

Provider-specific long-audio behavior remains inside the provider boundary.

## 10. Vision

If:

~~~text
provider.capabilities.vision == true
~~~

use the existing representative-frame approach.

Vision is enrichment, not mandatory success.

If frame extraction or vision fails but transcript is valid:

~~~text
analysis_completeness = TRANSCRIPT_ONLY
~~~

Item may still become READY.

## 11. Authentication

### Default

Try public extraction without auth.

### Optional configured cookies

Support a manually provisioned cookie file only if required by actual usage.

Suggested config:

~~~text
INSTAGRAM_COOKIES_FILE=
~~~

Rules:

- optional;
- file outside repository;
- never committed;
- never logged;
- never included in backup/export;
- never displayed in /status;
- passed to yt-dlp through structured API/options, not a shell command;
- no automatic “read my browser profile” production behavior.

## 12. Errors

Introduce only useful stable application errors.

Candidate mapping:

~~~text
AUTH_REQUIRED
RATE_LIMITED
DOWNLOAD_FAILED
EXTRACTION_FAILED
TOO_LARGE
TIMEOUT
UNSUPPORTED_SOURCE
~~~

If existing generic codes are enough, reuse them.

### AUTH_REQUIRED

User-facing example:

> Не удалось получить Reel без авторизации Instagram. Ссылка сохранена; можно настроить cookies и повторить.

### RATE_LIMITED

Should be considered potentially transient with bounded retry/backoff.

Do not retry indefinitely.

## 13. Durable Item on failure

When extraction fails after ingestion:

- Item remains in DB;
- source URL remains available;
- error_code/error_message are controlled;
- Retry is possible;
- no partial temp data leaks.

## 14. Resumability

Persist successful expensive stages:

- transcript;
- visual notes;
- description/caption if useful.

If LLM analysis fails after transcript persistence, retry must not redownload/retranscribe the Reel.

## 15. Size and duration bounds

Reuse current video/media limits where semantics match.

If Instagram-specific bounds are necessary, keep them minimal.

yt-dlp options must prevent accidental multi-item expansion.

No collection/profile processing.

## 16. Subprocess/extractor safety

The yt-dlp Python API runs in an isolated child process. The parent uses a
structured argument array, never a shell command, and enforces a wall-clock
timeout. On cancellation or shutdown it terminates the child process group and
waits for exit before removing the private download directory. Unknown-duration
ffprobe uses the same cancellable process boundary. Worker metadata returned to
the parent is bounded.

## 17. Prompt injection

Reel captions/transcripts/visual notes are untrusted content.

They never gain tool authority.

Existing Analyzer system isolation remains mandatory.

## 18. Telegram UX

ACK:

~~~text
Принял Reel. Разбираю…
~~~

Successful result uses the same Item result UI, including PM-01 interest control when available.

Failure should distinguish likely auth/rate-limit/permanent unsupported errors.

## 19. Forwarded messages

A forwarded Telegram message containing an Instagram Reel URL:

- routes to INSTAGRAM;
- preserves PM-02 forward metadata;
- uses the same message-local URL source identity;
- original forwarded text/caption remains source/user-context according to PM-02 semantics.

## 20. Tests

### URL recognition

- valid reel URL;
- unrelated Instagram profile URL does not silently enter Reel pipeline unless explicitly supported;
- generic website unaffected.

### Extraction

Use mocked/fixture yt-dlp metadata.

Cover:

- public happy path;
- missing optional metadata;
- STT fallback;
- vision=false;
- vision success;
- vision failure graceful;
- oversized/duration rejection.

### Auth/rate limiting

- auth-required maps to controlled error;
- optional cookie path is passed without leaking credentials;
- rate-limit uses bounded retry;
- permanent unsupported error does not spin.

### Resume

- transcript persisted;
- LLM retry does not invoke yt-dlp/STT again;
- visual notes reused.

### Security

- no shell interpolation;
- temp cleanup;
- private/protected content is not bypassed.

## 21. Acceptance criteria

1. A public supported Reel can enter the normal AIInbox pipeline.
2. Instagram is recognized before generic WEB extraction.
3. Spoken content is transcribed when accessible.
4. Vision is optional and honestly reflected in completeness.
5. Metadata/caption are preserved without being mislabeled.
6. Auth-required and rate-limited cases fail transparently.
7. Optional cookies are handled as secrets.
8. Retry reuses durable intermediate results.
9. No crawling/bulk/private-account bypass exists.
10. Full quality gate passes.

## 22. Rollout

Initial rollout should be conservative.

Observe real failure distribution:

- public success;
- auth-required;
- rate-limit;
- extractor changes.

Do not expand scope until real usage shows which Instagram failures matter.

## 23. Definition of Done

- routing;
- extractor;
- persistence;
- controlled auth/error mapping;
- STT/vision reuse;
- tests;
- BOT_USAGE/RUNBOOK config docs;
- no unrelated social platform framework;
- repository review workflow completed.
