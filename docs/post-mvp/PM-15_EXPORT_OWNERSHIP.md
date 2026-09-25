# PM-15 — Export / Ownership

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: stable post-MVP schema; existing verified backup/restore remains independent  
Status: PLANNED

## 1. Problem

AIInbox already has disaster-recovery backup, but backup is not a user-owned portable export.

A backup exists to restore the application exactly. A user export exists to let the user inspect, archive or move their data outside AIInbox without understanding SQLite internals.

## 2. Goal

Add a user-facing export that produces portable JSON and Markdown from the user's canonical AIInbox data.

The export must be explicit, bounded, privacy-aware and completely independent from the backup/restore path.

## 3. Backup vs export

Keep the distinction strict:

~~~text
Backup
→ operational disaster recovery
→ SQLite-level fidelity
→ may contain internal implementation tables

Export
→ user ownership / portability
→ documented stable schema
→ readable outside AIInbox
→ excludes secrets and technical caches
~~~

Do not reuse backup files as exports.

## 4. In scope

- `/export` user command;
- compact and full export modes;
- background durable export job;
- deterministic JSON/JSONL representation;
- Markdown representation;
- optional inclusion of original persisted text/transcripts in full mode;
- ZIP packaging;
- durable Telegram file delivery;
- restrictive temporary/persistent export-file handling;
- automatic cleanup after successful delivery and bounded expiration;
- schema version/manifest;
- tests and documentation.

## 5. Out of scope

- database restore from export;
- cross-instance import;
- synchronization with Notion/Drive/Obsidian;
- cloud object storage;
- scheduled exports;
- encryption format/key management beyond normal transport/storage permissions;
- exporting API keys/cookies/provider credentials;
- exporting derived embedding vectors;
- exporting transient LLM Ask answers.

Import can be a later phase only after export format has real usage.

## 6. User surface

Recommended v1:

~~~text
/export
/export compact
/export full
~~~

`/export` defaults to `compact`.

Compact:

- canonical Item metadata;
- source metadata/provenance;
- profile/settings;
- lifecycle/feedback history;
- generated summaries/analysis fields already canonical on Item;
- no long extracted text/transcripts.

Full adds original persisted evidence Content.

## 7. Handler behavior

Telegram handler must remain thin:

~~~text
validate mode
→ create durable ExportJob
→ commit
→ quick ACK
~~~

Do not serialize the full database or create ZIP files inside the handler.

## 8. Durable ExportJob

Add a small job model, conceptually:

~~~text
ExportJob
- id
- user_id
- telegram_message_id nullable
- mode: COMPACT | FULL
- status: PENDING | RUNNING | DONE | FAILED
- error_code
- error_message
- created_at
- updated_at
~~~

Use `(user_id, telegram_message_id)` idempotency when a Telegram message id exists so transport retries do not create duplicate exports.

## 9. Worker

Add an `ExportWorker` using the existing single-process worker pattern:

~~~text
oldest PENDING
→ atomic conditional claim RUNNING
→ read immutable export snapshot/projection
→ write archive outside SQLite transaction
→ short transaction: DONE + durable Delivery intent
~~~

Startup recovery:

~~~text
RUNNING → PENDING
~~~

DB infrastructure failures must escape to the process supervisor.

## 10. Export artifact storage

Generated archives can be much larger than normal Telegram message payloads.

Do not store archive bytes in SQLite.

Use a configured persistent export directory separate from:

- canonical DB path;
- temporary media tmpfs;
- backup directory.

Example:

~~~text
/data/exports/
~~~

Files must use non-user-controlled generated names and restrictive permissions.

## 11. Crash safety

Write to a unique temporary path in the export directory, then atomically rename to the final generated filename only after the archive is complete.

On startup/periodic cleanup:

- remove abandoned temporary export files older than a bounded threshold;
- keep files referenced by pending/sending Deliveries;
- remove expired terminal artifacts.

## 12. Delivery integration

Extend the existing durable `Delivery` source model with `export_job_id` rather than inventing a second Telegram outbox.

Update the single-source CHECK so exactly one of the supported source FKs is non-null.

Add a delivery type such as:

~~~text
EXPORT_FILE
~~~

`DeliveryWorker` sends the generated ZIP/document and then removes the artifact only after Telegram delivery is durably marked SENT.

## 13. At-least-once boundary

Keep current Delivery semantics.

A crash after Telegram accepts the file but before Delivery is finalized can cause a duplicate after restart.

Do not claim exactly-once delivery.

## 14. Export format version

Every archive contains a manifest with a stable explicit version:

~~~json
{
  "format": "aiinbox-export",
  "version": 1,
  "generated_at": "...",
  "mode": "compact",
  "user": {...}
}
~~~

Never infer export schema version from application version alone.

## 15. Archive layout

Recommended:

~~~text
aiinbox-export-<timestamp>.zip
  manifest.json
  profile.json
  settings.json
  items.jsonl
  sources.jsonl
  events.jsonl
  reminders.jsonl
  items.md
  contents.jsonl        # full mode only
  README.md
~~~

Exact filenames should remain stable once shipped.

## 16. Item export

Export user-owned/canonical Item fields useful outside AIInbox, including:

- stable Item id within this export;
- lifecycle/processing status;
- title/summary/category/type/tags;
- user_note;
- source type and safe source URL;
- analysis factors and priority score where useful;
- interest level;
- next action / suggested due date;
- language/completeness;
- created/completed/archived/snoozed timestamps.

Do not export transient error stack traces or internal worker state unless explicitly documented as portable data.

## 17. Source export

Export source provenance needed to understand composite Items:

- item_id;
- source_index;
- source_type;
- source_url;
- duration;
- extraction outcome;
- bounded/safe metadata.

Do not export Telegram `file_id` by default: it is transport-specific, not portable user content.

## 18. Forward provenance

User-visible forward provenance may be exported:

- forwarded flag;
- source display name/username where persisted;
- public original message link ingredients where available;
- original sent timestamp.

Do not invent unavailable identities.

## 19. Profile/settings

Export:

- UserProfile JSON;
- timezone;
- user-facing notification/attention settings.

Do not export:

- Telegram bot token;
- LLM provider credentials;
- API bearer tokens;
- cookie file contents/path secrets;
- host/deployment secrets.

## 20. Events

Events are user history and should be portable in a documented representation.

Export:

- event type;
- item/reminder relationship where meaningful;
- created_at;
- sanitized bounded payload fields.

Do not expose internal callback receipt rows as user history.

## 21. Reminder history

Export a simplified reminder representation sufficient to understand reminder-linked Events:

- reminder id;
- item id nullable;
- type;
- status;
- scheduled_at;
- sent_at.

Do not export:

- claim_generation;
- claimed_at leases;
- internal scheduling payload fields that contain no user-portable meaning.

## 22. Content export — compact

Compact mode does not include long original evidence text.

It still includes canonical Item summary/title/metadata, so the export remains useful and small.

## 23. Content export — full

Full mode includes primary persisted content kinds:

- USER_TEXT;
- WEB_TEXT;
- DOCUMENT_TEXT;
- TRANSCRIPT;
- VISUAL_NOTES;
- DESCRIPTION.

Optionally include CHUNK_SUMMARY only if explicitly documented as generated processing output.

Do not include by default:

- ATTENTION_HOOK;
- TRANSCRIPT_CHUNK;
- Ask generated answers;
- embedding vectors.

## 24. Content provenance

Each exported Content row must retain:

- content id;
- item id;
- source id nullable;
- kind;
- created_at;
- text;
- safe metadata where meaningful.

Validate source ownership exactly as PM-13 does; do not export a mismatched cross-Item `source_id` association as if it were valid provenance.

## 25. Markdown

`items.md` is a readable view, not the canonical machine export.

Recommended structure per Item:

~~~text
## Title
- ID
- State / Type / Category
- Interest / Priority
- Created
- Sources

Summary...

User note...

Full mode only:
### Source content
...
~~~

Bound headings and escape/normalize pathological text so one Item cannot corrupt the Markdown structure.

## 26. JSON determinism

Use UTF-8 and stable field names.

JSONL rows should be emitted in deterministic order, for example by numeric id.

Do not rely on ORM `__dict__` serialization.

## 27. Archive safety

All ZIP entry names are application-owned constants.

Never use Item title or source filename directly as an archive path.

This avoids path traversal and pathological filename issues.

## 28. Size bounds

Full export may be large.

Before Telegram delivery, enforce a configured maximum archive size consistent with Telegram transport limits.

If a full export is too large:

- fail with a controlled user message;
- do not silently omit random Items;
- optionally recommend compact mode.

Chunked multi-file export is out of scope for v1 unless required by real data size.

## 29. Privacy

An export is highly sensitive.

Requirements:

- only the owning allowlisted user can request it;
- generated paths are random/non-guessable;
- logs never contain exported content;
- artifact is removed after successful delivery;
- failed/abandoned artifacts expire after bounded retention;
- file permissions are restrictive.

## 30. No hidden canonical mutation

Export must not change:

- Item lifecycle;
- ranking;
- Events;
- Reminders;
- profile/settings;
- FTS;
- embeddings.

Only ExportJob/Delivery/artifact housekeeping changes.

## 31. Tests — schema/content

Cover:

- compact contains required machine-readable files;
- full includes primary persisted Content;
- compact excludes long Content;
- source provenance preserved;
- DONE/ARCHIVED/SNOOZED included;
- profile/settings represented;
- events/reminder relationships represented;
- generated hooks/checkpoints/vectors excluded by default.

## 32. Tests — secrets

Seed values resembling:

- API keys;
- Telegram bot token;
- provider base credentials;
- Instagram cookie path/content.

Verify no configured secret is present in archive bytes.

## 33. Tests — worker/recovery

Cover:

- atomic job claim;
- duplicate Telegram update idempotency;
- RUNNING startup recovery;
- interrupted artifact creation does not appear as complete archive;
- DONE + Delivery creation consistency;
- Telegram retry does not regenerate export;
- successful Delivery removes artifact;
- pending Delivery preserves artifact.

## 34. Tests — ownership

Two users with data:

User A export contains zero User B Items/Events/Reminders/Profile data.

## 35. Backup regression

Existing online SQLite backup, checksum, restore drill and off-host flow must remain unchanged.

Export code must not call backup scripts or reuse backup rotation directories.

## 36. Documentation

Update:

- PRODUCT_SPEC;
- BOT_USAGE;
- RUNBOOK;
- privacy/retention notes;
- post-MVP roadmap.

Document clearly:

~~~text
backup != export
export != import
~~~

## 37. Acceptance criteria

1. `/export` supports compact and full modes.
2. Handler only enqueues and ACKs.
3. ExportJob is durable/recoverable.
4. Existing DeliveryWorker sends the archive.
5. Archive is generated outside SQLite transactions.
6. Export has explicit format/version manifest.
7. JSON/JSONL is deterministic.
8. Markdown is readable and bounded.
9. Composite source provenance is preserved.
10. Compact excludes long primary Content.
11. Full includes documented primary Content.
12. Secrets/cookies/API keys are excluded.
13. Technical caches/jobs/callback receipts/embeddings are excluded by default.
14. Artifact names cannot use user-controlled paths.
15. User isolation is strict.
16. Successful delivery cleans the artifact.
17. Failed/pending delivery preserves required artifact until retry/expiry.
18. Backup/restore behavior is unchanged.
19. No canonical Item/ranking mutation occurs.
20. Full quality gate passes.

## 38. Definition of Done

~~~text
/export full
    ↓
ExportJob PENDING
    ↓
ExportWorker
    ↓
canonical user data projection
    ↓
manifest + JSONL + Markdown
    ↓
atomic ZIP artifact
    ↓
ExportJob DONE + Delivery PENDING
    ↓
DeliveryWorker
    ↓
Telegram file
    ↓
Delivery SENT
    ↓
secure artifact cleanup
~~~

PM-15 is complete when the user can take their AIInbox data away in a documented, readable format without receiving an operational database backup or leaking deployment secrets.