# PM-03 — Telegram Video & Documents

Type: Post-MVP Epic + Detailed Technical Specification  
Status: IN_REVIEW
Prerequisites: stabilized MVP; PM-02 recommended for forwarded variants

## 1. Epic

### Problem

The bot captures direct Telegram video through the shared composite Item pipeline.
PM-03 adds common documents to the same source/checkpoint/analysis flow.

Users should not need to upload a file elsewhere and send a URL just to make AIInbox understand it.

### Goal

Support native Telegram capture for:

- video;
- PDF;
- TXT;
- Markdown;
- DOCX.

All new sources converge into the existing message aggregate:

~~~text
ingestion
→ one durable Item
→ one or more durable ItemSource rows
→ independent extraction/checkpoints
→ combined NormalizedContent
→ Analyzer
→ PriorityEngine
→ persisted result
~~~

No second media-analysis subsystem.

## 2. In scope

### Video

- Telegram `video` — implemented;
- optionally compatible video sent as Telegram document when MIME/extension is recognized safely;
- audio extraction;
- transcription;
- representative frames;
- optional vision enrichment;
- persisted transcript/visual notes;
- resumable processing;
- size/duration bounds.

### Documents

- PDF;
- text/plain;
- Markdown;
- DOCX;
- document extraction into normalized text;
- existing chunking/long-content analysis;
- persisted extracted text;
- file metadata;
- retry from durable extracted content.

### URL PDF

If generic web ingestion receives a response that is clearly a supported document (initially PDF), route it to the document extraction path rather than trying HTML extraction.

URL sources remain `WEB` after fetch. Confirmed PDF bytes produce a document
`NormalizedContent` and a durable `DOCUMENT_TEXT` checkpoint with response
metadata. Recovery identifies this path from the persisted content kind, so source
identity does not change during processing.

## 3. Out of scope

- OCR for arbitrary scanned PDFs in the first implementation unless there is a clear low-cost library already approved;
- spreadsheets;
- presentations;
- archives;
- password-protected files;
- DRM/protection bypass;
- arbitrary executable/binary formats;
- cloud-drive authentication;
- new object storage;
- keeping large source media permanently after successful derivation.

## 4. Source types

Add explicit domain source types only for true content-source distinctions.

Recommended:

~~~text
VIDEO
DOCUMENT
~~~

Document subtype belongs in metadata, e.g.:

~~~json
{
  "document_format": "pdf",
  "mime_type": "application/pdf",
  "file_name": "paper.pdf"
}
~~~

Do not create source enums for PDF/TXT/MD/DOCX unless behavior truly requires it.

## 5. Ingestion

Telegram handler responsibilities remain thin:

1. allowlist check;
2. read Telegram metadata;
3. perform cheap pre-queue size validation when Telegram provides size;
4. create one durable Item plus its child ItemSources;
5. ACK quickly;
6. worker handles download/extraction.

For an oversized known file:

- mark the video source `FAILED/TOO_LARGE`;
- fail the whole Item only when no useful caption/URL sibling remains;
- otherwise continue to a `PARTIAL` combined analysis;
- do not enqueue a download that cannot succeed;
- show configured limit, not a magic hardcoded number.

## 6. Telegram video pipeline

~~~text
Telegram video/file_id
      ↓
bounded download to temp
      ↓
ffprobe metadata
      ↓
duration/size validation
      ↓
┌──────────────┬─────────────────┐
│              │                 │
audio extract  representative frames
│              │
STT            vision if capability=true
│              │
transcript     visual_notes
└──────────────┴─────────────────┘
      ↓
NormalizedContent
      ↓
existing Analyzer
~~~

### Reuse

Reuse current capabilities where practical:

- Telegram download boundary;
- TranscriptionProvider;
- ffmpeg/ffprobe subprocess safety;
- frame extraction/dedup;
- vision capability detection;
- temp cleanup;
- transcript/visual notes persistence;
- analysis completeness.

Do not copy the YouTube pipeline wholesale if shared small helpers can be extracted cleanly.

Do not perform a broad media-framework rewrite merely to remove a few lines of duplication.

## 7. Video analysis completeness

Use explicit completeness:

- TRANSCRIPT_ONLY when vision is unavailable/fails;
- TRANSCRIPT_AND_VISUAL when both succeeded.
- VISUAL_ONLY when the video has no transcript but vision succeeds.

A vision failure with a valid transcript should normally remain a successful Item with honest completeness.
A video with no transcript and failed vision extraction follows the existing failure/partial-content policy.

A transcription and vision failure with no other meaningful content is a failed Item.

If transcription fails outside the visual-only path, meaningful caption text or
another successful ItemSource keeps the Item `PARTIAL`; the source-local failure remains durable.

## 8. Video temp/resource policy

- source media is temporary by default;
- derived transcript and visual notes are durable;
- frames are temporary;
- clean on success/failure/timeout/cancellation where practical;
- no `shell=True`;
- subprocesses have bounded timeout;
- cancellation propagates correctly.

## 9. Document pipeline

~~~text
Telegram document
      ↓
bounded download
      ↓
format identification
      ↓
DocumentExtractor
      ↓
extracted text
      ↓
persist DOCUMENT_TEXT
      ↓
existing long-content path
      ↓
Analyzer
~~~

## 10. Document format detection

Use a conservative combination of:

- Telegram MIME type;
- filename extension;
- fetched HTTP content type for URL documents.

Do not trust extension alone when content can be safely identified.

Reject unsupported format explicitly.

Suggested error:

~~~text
UNSUPPORTED_SOURCE
~~~

or a narrower document-specific code only if it improves user diagnostics.

## 11. PDF

Requirements:

- extract selectable text;
- preserve page-order text reasonably;
- optional page metadata may be stored;
- enforce page/file/text-size bounds if required;
- if PDF has essentially no extractable text, report a controlled extraction limitation.

Do not silently claim a scanned PDF was analyzed if only empty/selectable text was available.

Future OCR should be a separate scoped feature.

## 12. TXT / Markdown

- decode safely;
- prefer UTF-8;
- bounded fallback encoding handling only if needed;
- do not execute embedded content;
- Markdown is data, not instructions.

## 13. DOCX

Extract readable paragraph/table text without executing macros or embedded objects.

Only textual extraction is required initially.

## 14. Content kinds

Add a durable content kind such as:

~~~text
DOCUMENT_TEXT
~~~

or reuse an existing generic text kind if semantics remain unambiguous.

Metadata should include useful derivation facts, e.g.:

~~~json
{
  "file_name": "paper.pdf",
  "mime_type": "application/pdf",
  "document_format": "pdf",
  "pages": 18
}
~~~

Avoid persisting giant redundant metadata.

## 15. Resume semantics

After successful extraction/transcription persistence:

- retry/restart must not download the same Telegram file again unnecessarily;
- retry after final LLM failure reuses durable text/transcript/visual notes;
- processing_stage remains meaningful.

This follows existing D-001 resumable architecture.

## 16. Forwarded messages

When PM-02 metadata is present:

- forwarded video uses the implemented VIDEO path;
- forwarded document uses DOCUMENT path;
- forward origin remains source metadata;
- it must not alter extraction semantics.

## 17. URL documents

Web ingestion should inspect the actual response boundary before HTML parsing.

For supported PDF response:

~~~text
WEB URL
→ secure/pinned download boundary
→ document detection
→ PDF extractor
→ DOCUMENT_TEXT
→ Analyzer
~~~

Preserve existing SSRF/DNS-pinning/redirect/byte-cap guarantees.

Do not hand a document URL to an unrestricted separate downloader.

## 18. Security

### Documents are untrusted

- no macro execution;
- no embedded shell/tool execution;
- no following arbitrary embedded external references during extraction;
- no HTML/browser execution for document extraction;
- extracted content remains prompt-injection-untrusted.

### Zip-bomb style risks

DOCX is a ZIP container. Extraction must enforce reasonable compressed/uncompressed limits or use a parser that does not permit unbounded expansion.

### Filenames

Never interpolate filenames into shell commands.

Temp paths should be generated by the application.

## 19. Configuration

Add only necessary config, for example:

~~~text
MAX_VIDEO_BYTES
MAX_VIDEO_DURATION_SECONDS
MAX_DOCUMENT_BYTES
MAX_DOCUMENT_TEXT_CHARS
~~~

Defaults are 20,000,000 source bytes and 500,000 extracted characters.

Reuse existing settings where semantics are identical.

Do not expose dozens of per-format knobs.

## 20. Telegram UX

Video:

~~~text
Принял видео. Разбираю…
~~~

Document:

~~~text
Принял документ paper.pdf. Разбираю…
~~~

On unsupported/scanned/unextractable content, return a concise reason and Retry only when retry can plausibly help.

## 21. Tests — video

- ingestion creates VIDEO;
- pre-known oversized file persists controlled failure;
- download byte cap;
- ffmpeg args are structured and safe;
- transcript path;
- vision=false path;
- vision success path;
- vision failure degrades to transcript-only;
- resume reuses transcript/visual notes;
- temp cleanup;
- forwarded video preserves PM-02 metadata.

## 22. Tests — documents

- PDF fixture extraction;
- TXT fixture;
- Markdown fixture;
- DOCX fixture;
- unsupported format;
- oversized document;
- empty/scanned-like PDF controlled failure;
- durable DOCUMENT_TEXT;
- retry reuses extraction;
- forwarded document metadata;
- URL PDF uses secure web boundary;
- prompt content does not influence tools.

Default suite must not require Telegram network or external sites.

## 23. Migration

Migration must include:

- new source enum values in the SQLite-compatible schema as required;
- new content kind if introduced;
- any source metadata dependencies not already added in PM-02.

Verify fresh and upgrade paths.

## 24. Acceptance criteria

PM-03 is accepted when:

1. Direct Telegram video becomes a normal durable Item.
2. Video uses STT and optional vision through existing boundaries.
3. PDF/TXT/MD/DOCX become analyzable Items.
4. Extracted content is persisted.
5. Retry does not repeat successful expensive extraction.
6. Forwarded variants work after PM-02.
7. URL PDF preserves existing SSRF security.
8. Unsupported/scanned content fails honestly.
9. No source file is unnecessarily retained after successful processing.
10. Full quality gate passes.

## 25. Definition of Done

- source/model/migrations complete;
- extractor boundaries implemented;
- user-facing handlers wired;
- resume behavior verified;
- temp/security tests added;
- BOT_USAGE/RUNBOOK updated;
- no OCR/spreadsheet/presentation creep;
- repository review protocol completed.
