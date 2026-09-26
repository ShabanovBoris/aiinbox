# Future Roadmap Registry — PM-21…PM-50

Status: candidate opportunity registry  
Purpose: preserve product directions discussed after PM-20 without implying implementation order.

## 1. Numbering rule

PM numbers in this document are stable idea identifiers, not execution priority.

Current execution priority is documented separately in
[CURRENT_FOCUS_API_BROWSER_INTEGRATIONS.md](CURRENT_FOCUS_API_BROWSER_INTEGRATIONS.md).

A lower PM number does not automatically run before a higher one.

## 2. Knowledge graph and retrieval

### PM-21 — Duplicate & Near-Duplicate Detection

Detect repeated URLs, reposts, duplicated notes and semantically near-identical
Items. Never delete automatically. Represent duplicate/related status as a
derived relationship so the user can inspect or consolidate safely.

### PM-22 — Related Items / Knowledge Connections

For an Item, surface a small set of genuinely related older saves. Relationships
remain derived and replaceable; they do not become a second canonical taxonomy.

### PM-23 — Topic Dossiers

Create virtual topic collections such as Local AI, Android, Piano or Finance from
existing Items. One Item may belong to multiple dossiers. Dossiers can later
support timeline, Ask and weekly views without introducing rigid folders.

### PM-24 — Temporal Ask / Inbox History

Extend Ask with time-aware questions such as:
- what did I save about X in the last two months?
- what changed between January and September material?
- what important material about X has not been revisited recently?

Time becomes an explicit retrieval dimension rather than an LLM guess.

### PM-25 — Contradictions & Source Comparison

Compare several saved sources and identify materially different claims with
citations. Do not automatically declare which source is correct unless the saved
evidence supports that conclusion.

## 3. From attention to execution

### PM-26 — Action Extraction

Derive candidate actions from saved material, such as trying a library, reviewing
a benchmark or changing a configuration. LLM output creates ActionCandidates only;
it must not silently convert an Item into canonical ACTION state.

### PM-27 — Projects / Outcomes

Introduce a lightweight Project concept connecting a goal/outcome to saved Items
and explicitly accepted actions. Keep projects below the application layer; do
not fork the Item processing pipeline.

### PM-28 — Next Best Action

Combine accepted actions, Attention score, estimated action time, dependencies and
current context to suggest one next concrete step.

### PM-29 — Focus Sessions

A bounded focus mode such as `/focus 30`: select one to three suitable actions or
Items for the requested window and temporarily suppress unrelated discretionary
nudges. Do not turn AIInbox into a full Pomodoro product.

### PM-30 — Waiting / Follow-up Items

Model "waiting for", "check after release" and "follow up later" separately from
ordinary snooze. Waiting means progress depends on an external event, not just a
future clock time.

### PM-31 — Conditional Watchlists

Let the user define conditions over newly ingested AIInbox material, e.g. alert
when a new saved Item mentions a specific device/model combination. This watches
new Inbox content, not the public internet.

## 4. Capture surfaces

### PM-32 — RSS / Newsletter / Email Capture

Add controlled inbound sources such as RSS feeds and forwarded email. Reuse the
canonical ingestion/extraction/analyzer path; do not build a separate email client.

### PM-33 — Browser Extension

Browser extension for fast Save to AIInbox from:
- current page;
- selected text;
- page URL;
- optional user note.

This item is elevated to the current execution focus after the minimal PM-18 HTTP
capture/auth contract is available.

### PM-34 — Screenshot / Image Capture

Capture screenshots/photos/slides into persisted OCR/visual notes and reuse the
existing Analyzer. Avoid a parallel image-only knowledge model.

### PM-35 — Source Freshness / Revisit

On explicit request, refetch a saved URL and compare the new source revision to the
saved one. Do not continuously refetch the whole archive.

## 5. Reflection and goals

### PM-36 — Personal Knowledge Timeline

Show how topics, saves and revisit/completion behavior evolved over time using
actual Item/Event history. Avoid psychological profiling.

### PM-37 — Goals Integration

Make goals more structured and user-correctable. Item-to-goal relationships must
remain explainable; inferred goal fit must not silently become canonical truth.

## 6. Cost, quality and privacy

### PM-38 — Adaptive Capture Analysis

Use cheaper analysis paths for low-value/simple captures and deeper analysis for
high-interest or complex material while preserving one canonical processing path.

### PM-39 — LLM Cost / Quality Observatory

Record bounded technical metrics per LLM operation: provider/model, latency,
tokens/cost estimate, invalid-output/failure rate. Do not persist full private
prompts merely for observability.

### PM-40 — Evaluation Harness

Create versioned fixtures for analysis, classification, summaries, Ask citations,
hooks and retrieval so provider/model changes can be measured before rollout.

### PM-41 — Local-first Privacy Mode

Allow policy such as "documents local only" or "this operation may use cloud".
A local-route failure must not silently exfiltrate the same content to cloud.

### PM-42 — Encrypted Sensitive Items

Optional at-rest encryption and cloud-processing restrictions for explicitly
sensitive Items. This is later-stage work after core product and key-management
requirements are mature.

## 7. Multi-client product

### PM-43 — Multi-device Sync Contract

Formalize incremental client sync, cursors/versioning and conflict semantics once
mobile/browser clients require more than bounded list APIs. Server remains
canonical.

### PM-44 — Web Client

Responsive first-party UI over the HTTP API for browsing, long-form reading,
search, Ask, Weekly and settings.

### PM-45 — Personal Dashboard

Operational dashboard for active/stale backlog, forgotten-important Items,
reminder outcomes, project progress and LLM operation health. Avoid vanity metrics.

### PM-46 — Importers

Support import of AIInbox's own PM-15 export first, then selected external sources
such as Pocket/Instapaper/Notion/Obsidian where useful. Provenance must survive.

### PM-47 — Knowledge Maintenance

Detect cleanup candidates such as duplicates, broken sources and very old
low-interest material. Suggest review batches; never auto-delete.

## 8. Research and agent direction

### PM-48 — Research Sessions

A bounded multi-question session over a selected topic and saved AIInbox material.
Context remains grounded in saved sources with citations.

### PM-49 — External Research with Explicit Boundary

Optional mode that clearly separates:
- Saved in AIInbox
- External research

External findings never silently become personal memory or canonical saved source
without an explicit capture action.

### PM-50 — Personal Agent Actions

Late-stage low-risk actions through explicit connectors/APIs, such as creating a
calendar block or preparing a draft. Critical actions require confirmation.
Ingested source content never receives tool authority.

## 9. Priority clusters

### Knowledge-first

PM-21 → PM-22 → PM-23 → PM-24 → PM-25 → PM-40 → PM-48 → PM-49.

### Execution-first

PM-26 → PM-27 → PM-28 → PM-29 → PM-30 → PM-37.

### Platform-first

PM-39 → PM-40 → PM-33 → PM-43 → PM-44 → PM-46.

These clusters are alternatives, not mandatory serial chains.

## 10. Current promotion

As of the current product sequencing decision:

- PM-18 HTTP API is the immediate platform foundation;
- PM-33 Browser Extension is promoted ahead of its numeric position;
- outbound integrations/synchronization are promoted as a dedicated integration
  track, with Miro as the first concrete connector;
- PM-14 semantic retrieval and PM-16/17 model-routing work remain separately
  gated unless explicitly resumed.
