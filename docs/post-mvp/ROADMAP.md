# Post-MVP Roadmap — Personal Attention Manager

Status: planning document  
Baseline: stabilized MVP + PM-00 reliability work on `main`  
Scope of this document: PM-01 and later. PM-00 is already implemented and is not duplicated here.

## 1. Product direction

The MVP already solves:

~~~text
capture
→ extract
→ understand
→ classify
→ prioritize
→ store
→ search
→ remind
~~~

Post-MVP development moves the product toward:

~~~text
capture almost anything
→ understand user intent
→ learn from explicit and implicit feedback
→ decide what deserves attention now
→ return it in an engaging, contextual form
→ observe the reaction
→ adapt future ranking and reminders
~~~

The product target is not a bookmark manager and not a generic TODO list. It is a **Personal Attention Manager**.

## 2. Architectural invariants

All post-MVP work must preserve the current architecture:

- single-process modular monolith while the personal workload permits it;
- SQLite remains canonical storage unless a real scaling constraint appears;
- existing resumable processing/checkpoint model is reused;
- Telegram handlers remain thin;
- source-specific logic belongs at the edges/extractors;
- all successfully extracted/transcribed content is persisted before expensive later stages;
- model output is validated and treated as untrusted;
- semantic priority remains explainable and deterministic;
- new ranking layers must not overwrite or destroy existing semantic signals;
- no Redis/Celery/Kafka/vector DB merely because a new feature could use one;
- no new parallel media-analysis pipeline when the existing NormalizedContent → Analyzer → PriorityEngine path can be reused.

## 3. Roadmap overview

| PM | Epic | Primary outcome | Depends on |
|---|---|---|---|
| 01 | User Interest Level | Explicit 1–3 interest signal on every Item | MVP |
| 02 | Forwarded Telegram Messages | Forwarding metadata + correct ingestion of forwarded text/media | PM-01 optional, MVP required |
| 03 | Telegram Video & Documents | Native video/PDF/TXT/MD/DOCX capture | PM-02 for forwarded variants |
| 04 | Instagram Reels | Reel → metadata/transcript/vision → existing analysis pipeline | PM-03 media reuse preferred |
| 05 | Explicit Feedback | Useful/not-interesting/corrections as durable feedback | PM-01 |
| 06 | Behaviour-Aware Ranking | Explainable personal affinity from events | PM-05 |
| 07 | Attention Ranking Engine | Dynamic “what should be shown now?” score | PM-01, PM-06 |
| 08 | Attention Intensity | Calm → aggressive notification policies | PM-07 |
| 09 | Contextual Attention Hooks | Grounded facts/hooks for old important Items | PM-07 |
| 10 | Motivational Nudges | Duolingo-like contextual motivation without a specific Item | PM-08 |
| 11 | Reminder Feedback Loop | Reminder outcomes feed future attention ranking | PM-08..10 |
| 12 | Weekly Review | Reflection over backlog, progress and stale Items | PM-06, PM-11 |
| 13 | Ask My Inbox | Question answering over saved Items with citations to Items | Search |
| 14 | Hybrid Semantic Search | FTS + embeddings only if lexical search is insufficient | PM-13 evidence |
| 15 | Export / Ownership | User-readable export independent from disaster-recovery backup | stable schema |
| 16 | Per-operation LLM Routing | Different provider/model per operation | provider boundary |
| 17 | Ollama / Local Models | Local models for selected cheap/private operations | PM-16 |
| 18 | HTTP API | API over existing application services | stable post-MVP core |
| 19 | Android Client | Share Sheet, rich browsing, widgets | PM-18 |
| 20 | Calendar-aware Attention | Recommend Items using real free-time windows | PM-07, external calendar |

## 3.1 Detailed specifications PM-06…PM-13

- [PM-06 — Behaviour-Aware Ranking](PM-06_BEHAVIOUR_AWARE_RANKING.md)
- [PM-07 — Attention Ranking Engine](PM-07_ATTENTION_RANKING.md)
- [PM-08 — Attention Intensity and Proactive Scheduling](PM-08_ATTENTION_INTENSITY.md)
- [PM-09 — Contextual Attention Hooks](PM-09_CONTEXTUAL_HOOKS.md)
- [PM-10 — Motivational Nudges](PM-10_MOTIVATIONAL_NUDGES.md)
- [PM-11 — Reminder Feedback Loop](PM-11_REMINDER_FEEDBACK_LOOP.md)
- [PM-12 — Weekly Review](PM-12_WEEKLY_REVIEW.md)
- [PM-13 — Ask My Inbox](PM-13_ASK_MY_INBOX.md)

## 4. Milestone grouping

### Milestone A — Capture Intent

PM-01 User Interest Level  
PM-02 Forwarded Telegram Messages

Goal: preserve user intent and source provenance with almost zero extra friction.

### Milestone B — Rich Capture

PM-03 Telegram Video & Documents  
PM-04 Instagram Reels

Goal: broaden what can be sent to the inbox while reusing the same analysis core.

### Milestone C — Personal Signals

PM-05 Explicit Feedback  
PM-06 Behaviour-Aware Ranking

Goal: turn user actions into explainable personalization signals.

### Milestone D — Attention Engine Core

- PM-07 Attention Ranking — DONE (PR #36 merged)
- PM-08 Attention Intensity — DONE (PR #37 merged)

Goal: separate semantic priority from “what deserves attention now?” and control reminder pressure.

### Milestone E — Engagement

- PM-09 Contextual Hooks — DONE
- PM-10 Motivational Nudges — DONE
- PM-11 Reminder Feedback Loop — DONE (PR #40 merged)

Goal: make resurfacing useful and engaging rather than repetitive.

### Milestone F — Reflection

- PM-12 Weekly Review — DONE (PR #41 merged)

Goal: expose trends in attention, backlog growth, completion, neglect and category balance.

### Milestone G — Knowledge

PM-13 Ask My Inbox  
PM-14 Hybrid Semantic Search

Goal: make stored material queryable as a personal knowledge base.

### Milestone H — Portability & Cost

PM-15 Export / Ownership  
PM-16 LLM Routing  
PM-17 Ollama

### Milestone I — Additional Clients & Context

PM-18 HTTP API  
PM-19 Android  
PM-20 Calendar-aware Attention

## 5. PM-01 — User Interest Level

Add a manual `interest_level` from 1 to 3 to every Item:

- 1 — low / “just save it”;
- 2 — normal, default;
- 3 — strong personal interest.

It is a user signal and must remain separate from:

- importance;
- urgency;
- goal_fit;
- interest_fit inferred by the LLM;
- priority_score.

The default must always be 2 and the common capture flow must require no tap.

Detailed specification: [PM-01_INTEREST_LEVEL.md](PM-01_INTEREST_LEVEL.md).

## 6. PM-02 — Forwarded Telegram Messages

Forwarding is source provenance, not a new content type.

Examples:

- forwarded text → TEXT;
- forwarded single link → WEB/YOUTUBE according to current URL routing;
- forwarded voice → VOICE;
- forwarded audio → AUDIO;
- forwarded video → VIDEO;
- forwarded document → DOCUMENT (implemented by PM-03).

One forwarded Telegram message remains one Item. URLs/media are child ItemSources,
all successful sources plus original text/caption are synthesized once, and a
failed child source may degrade the Item to PARTIAL instead of discarding usable
siblings. Preserve available forward origin metadata without confusing original
caption/text with the user's own `user_note`.

Detailed specification: [PM-02_FORWARDED_MESSAGES.md](PM-02_FORWARDED_MESSAGES.md).

## 7. PM-03 — Telegram Video & Documents

Current state: Telegram video and PDF/TXT/Markdown/DOCX ingestion use the shared
composite Item pipeline. PM-03 is implemented and merged; its acceptance status
is DONE.

Video uses existing STT + representative frames + optional vision with transcript-only
fallback and source-local retry checkpoints.

Documents should normalize into text and reuse the existing long-content/chunking/analyzer path.

Detailed specification: [PM-03_TELEGRAM_VIDEO_DOCUMENTS.md](PM-03_TELEGRAM_VIDEO_DOCUMENTS.md).

## 8. PM-04 — Instagram Reels

Current state: DONE. PM-04 was implemented and merged to main in PR #31.

Recognize Instagram Reel URLs before generic web routing and process them as media:

~~~text
Reel URL
→ extractor metadata
→ media/audio download when available
→ STT
→ representative frames/vision when available
→ NormalizedContent
→ existing Analyzer
~~~

Integration is best-effort. Auth-required, rate-limit and extraction failures are controlled external failures; the system must not attempt to bypass private-account or platform protections.

Detailed specification: [PM-04_INSTAGRAM_REELS.md](PM-04_INSTAGRAM_REELS.md).

## 9. PM-05 — Explicit Feedback

Current state: DONE. Feedback controls and transactional corrections are part
of the current main baseline.

Add explicit user feedback in addition to implicit lifecycle events.

Initial feedback surface:

- Useful;
- Not interesting;
- Correct category;
- Correct type;
- Priority should be higher;
- Priority should be lower;
- Summary is wrong.

Feedback is durable event data and, where appropriate, a direct correction to canonical Item metadata.

It does not immediately introduce ML.

Detailed specification: [PM-05_EXPLICIT_FEEDBACK.md](PM-05_EXPLICIT_FEEDBACK.md).

## 10. PM-06 — Behaviour-Aware Ranking

Current state: DONE. `BehaviourAffinityService` derives user-scoped
category/type affinity from canonical Item metadata and Event history; its
bounded `personal_rank` is consumed by PM-07. `/today` continues to use
`priority_score`.

Do not alter `priority_score`.

Introduce a separately explainable behaviour/personal-affinity signal based on accumulated events, with strong smoothing for sparse history.

Candidate inputs:

- TODAY_SHOWN;
- DONE;
- SNOOZED;
- ARCHIVED;
- USEFUL;
- NOT_INTERESTING;
- PRIORITY_HIGHER/LOWER;
- category/type history.

Initial implementation should be deterministic and bounded, e.g. maximum ±15 points of adjustment.

No ML until event volume justifies it.

## 11. PM-07 — Attention Ranking Engine

Current state: DONE. PM-07 Attention Ranking was merged in PR #36.
The deterministic manual `/attention` preview uses
PM-06 `personal_rank`, manual interest, age, due date and durable exposure.
`/today` and the digest keep their existing semantic-priority ordering.

Create a dynamic ranking answering:

> What should be shown to the user now?

Inputs may include:

- semantic priority;
- manual interest_level;
- personal affinity;
- age;
- last shown time;
- last action time;
- urgency;
- stale-important bonus;
- recent-show penalty;
- reminder fatigue.

Do not store a permanently canonical attention score if it depends on current time. Prefer calculation from persisted inputs.

Old + important + neglected Items should receive a meaningful boost, while an Item shown yesterday should receive a cooldown penalty.

## 12. PM-08 — Attention Intensity

Add a simple setting with levels 1–5.

Initial policy:

| Level | Label | Approx max/day | Minimum gap | Same-item cooldown |
|---|---|---:|---:|---:|
| 1 | Calm | 1 | ~8h | ~72h |
| 2 | Light | 2 | ~5h | ~48h |
| 3 | Normal | 3 | ~3h | ~24–36h |
| 4 | Active | 4 | ~2h | ~18–24h |
| 5 | Aggressive | 5–6 | ~90m | ~8–18h |

Default: 3.

Always respect:

- quiet hours;
- daily cap;
- minimum interval;
- DONE/ARCHIVED;
- explicit snooze;
- notification fatigue.

The user should configure one intensity level, not 15 ranking coefficients.

## 13. PM-09 — Contextual Attention Hooks

For resurfaced Items, produce a grounded reason to care.

Candidate hook types:

- SURPRISING_FACT;
- PRACTICAL_VALUE;
- QUESTION;
- CHALLENGE;
- CONTRAST.

Hooks must be grounded only in persisted source content. A generated hook should retain evidence/source identity.

Generate lazily only after an Item becomes a real proactive-reminder candidate.
Persist up to three grounded hooks and reuse them with deterministic phrasing
templates rather than calling an LLM for every reminder.

## 14. PM-10 — Motivational Nudges

Add optional Duolingo-like nudges not tied to one Item.

Examples must be based on real computed facts:

- “4 important Items are older than a month”;
- “You have 3 quick wins under 15 minutes”;
- “You added more than you completed today”;
- “You completed at least one Item three days in a row”.

First version should be template-based. An LLM may later rewrite already-computed facts but must not invent metrics.

Provide `generic_motivation_enabled`.

Current state: DONE. Nudges use deterministic Item/Event facts and share
PM-08 budget, quiet hours, minimum gap and durable claims.

## 15. PM-11 — Reminder Feedback Loop

Record reminder-specific outcomes:

- REMINDER_SENT;
- REMINDER_OPENED;
- REMINDER_SNOOZED;
- REMINDER_DONE;
- REMINDER_DISMISSED;
- REMINDER_DISLIKED.

Reminder UI should allow a quick negative signal such as “Fewer like this”.

Current state: IN_REVIEW. Reminder-attributed outcomes feed bounded deterministic
ranking and scheduling adjustments; normal Item lifecycle Events remain canonical.

This closes:

~~~text
attention ranking
→ reminder
→ user reaction
→ durable event
→ future ranking
~~~

## 16. PM-12 — Weekly Review

Current state: DONE. PM-12 was implemented and merged to `main` in PR #41.

Add on-demand `/weekly`; scheduled weekly delivery is out of scope for v1.

Show:

- added/completed/archived counts;
- active categories;
- stale backlog;
- most snoozed themes;
- progress themes;
- old important Items never revisited;
- maximum three concrete recommendations.

This is reflection, not another giant backlog dump.

## 17. PM-13 — Ask My Inbox

Current state: IN_REVIEW.

Initial implementation should use existing FTS5:

~~~text
query
→ FTS retrieval
→ relevant Items + persisted Contents
→ LLM synthesis
→ answer referencing actual Items
~~~

Do not start with embeddings.

Answers must identify source Items so hallucinated “memory” cannot silently become canonical truth.

## 18. PM-14 — Hybrid Semantic Search

Only start this phase after real queries demonstrate systematic FTS failure.

Prefer:

~~~text
FTS score
+ embedding similarity
+ metadata
~~~

For personal scale, first evaluate embeddings stored with SQLite and in-process similarity. Do not introduce a vector database by default.

## 19. PM-15 — Export / Ownership

Backup and export are different:

- backup restores AIInbox;
- export gives the user portable data outside AIInbox.

Provide JSON and Markdown export, optionally including full persisted text/transcripts.

Never include secrets/cookies/API keys.

## 20. PM-16 — Per-operation LLM Routing

Route deterministic operation classes:

- FINAL_ANALYSIS;
- CHUNK_SUMMARY;
- VISION;
- TRANSCRIPTION;
- PROFILE_PATCH;
- ATTENTION_HOOK;
- ASK_INBOX;
- EMBEDDING.

Routing is configuration/code, not an LLM deciding which LLM to call.

## 21. PM-17 — Ollama / Local Models

After per-operation routing, support local models first for low-risk/cheap tasks:

- chunk summary;
- tags/classification where adequate;
- attention hooks;
- embeddings.

Do not require local models for final analysis until quality is demonstrated.

## 22. PM-18 — HTTP API

Expose existing application services via FastAPI rather than recreating business logic.

Minimum future endpoints:

~~~text
POST /items
GET /items
GET /items/{id}
GET /today
GET /attention
GET /search
POST /ask
POST /items/{id}/done
POST /items/{id}/snooze
POST /items/{id}/archive
PATCH /items/{id}/interest
~~~

## 23. PM-19 — Android Client

Android is justified when Telegram becomes limiting for:

- Share Sheet capture;
- rich browsing;
- filters/search;
- statistics/weekly review;
- widgets;
- settings;
- history.

Telegram remains a supported client.

## 24. PM-20 — Calendar-aware Attention

Use actual free-time windows together with:

- attention score;
- estimated_action_minutes;
- age;
- interest level.

Example outcome:

> 24 minutes until the next event. A high-value Item estimated at 18 minutes has been waiting for 31 days.

Calendar integration must remain optional and must not make core ranking unusable without a calendar.

## 25. Target data-model evolution

Near-term additions:

### Item

- `interest_level` — PM-01;
- `source_metadata_json` — PM-02.

Do not persist `attention_score` prematurely if it is time-dependent.

### Events

Extend existing durable events with:

- INTEREST_CHANGED;
- USEFUL;
- NOT_INTERESTING;
- CATEGORY_CORRECTED;
- TYPE_CORRECTED;
- PRIORITY_HIGHER;
- PRIORITY_LOWER;
- SUMMARY_REPORTED_WRONG;
- REMINDER_SENT;
- REMINDER_OPENED;
- REMINDER_DISMISSED;
- REMINDER_DISLIKED.

### Content

Future kind:

- ATTENTION_HOOK.

Hook metadata should identify source/evidence.

### User settings

Future minimal additions:

~~~json
{
  "attention_enabled": true,
  "attention_intensity": 3,
  "generic_motivation_enabled": true
}
~~~

## 26. Success metrics for the Attention Manager

Do not optimize for “notifications sent”.

Prefer:

- reminder → open;
- reminder → done within 24h;
- reminder → snooze;
- reminder → archive;
- reminder → “fewer like this”;
- old high-priority Items returning to DONE/ARCHIVED;
- median age of active backlog;
- percentage of stale important Items eventually resolved.

A notification system that increases dismiss/snooze rates while users lower intensity is regressing even if engagement count rises.

## 27. Target experience

The long-term experience should be:

~~~text
user sends almost anything
→ does almost no manual organization
→ system understands and stores it
→ some Items are naturally forgotten
→ Attention Manager understands importance, interest, age and behaviour
→ resurfaces the right Item
→ gives a concrete grounded reason to care
→ learns from the reaction
~~~

The defining product question is:

> Does AIInbox help the user return to things they genuinely wanted to do or learn, at a useful moment, without turning the inbox into notification noise?
