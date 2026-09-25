# AIInbox Quality & UX Polish Milestone

Baseline when this milestone was defined:

~~~text
main: c971f9920ccbcbf646a383e14522026cb209d485
PM-15: merged in PR #44
PM-14+: planned
~~~

## Why this milestone exists

AIInbox already has rich capture, explicit feedback, Attention ranking, proactive reminders, Ask My Inbox and portable export.

Current user testing shows that the next constraint is not missing capability but the quality and density of the existing experience:

- analysis is often generic instead of outcome-first;
- profile context can distort category classification;
- READY cards expose too much internal scoring metadata;
- Item keyboards are oversized;
- resurfaced Items are inconsistently linked back to source/original capture;
- hooks and motivational copy are technically correct but weak;
- slash-command discovery is poor;
- Ask failures are too generic to diagnose from the user surface.

The milestone therefore pauses major feature expansion and polishes the existing core first.

## Five implementation PRs

1. [POLISH-01 — AI Analysis v2](POLISH-01_AI_ANALYSIS_V2.md)
2. [POLISH-02 — Compact Telegram Item UI](POLISH-02_COMPACT_TELEGRAM_UI.md)
3. [POLISH-03 — Unified Provenance & Original Access](POLISH-03_UNIFIED_PROVENANCE_ORIGINAL_ACCESS.md)
4. [POLISH-04 — Hooks & Notifications v2](POLISH-04_HOOKS_NOTIFICATIONS_V2.md)
5. [POLISH-05 — Telegram Navigation & AI Reliability](POLISH-05_NAVIGATION_AND_AI_RELIABILITY.md)

## Recommended implementation order

~~~text
POLISH-01
AI analysis / category quality
        ↓
POLISH-02
compact default UI
        ↓
POLISH-03
original/source access everywhere
        ↓
POLISH-04
hooks and notifications v2
        ↓
POLISH-05
navigation and AI reliability
        ↓
explicit quality review
        ↓
resume PM-14+
~~~

Some implementation work may overlap, but each PR should remain independently reviewable and should not silently absorb the next PR's scope.

## Current status and next gate

- POLISH-01 — DONE (PR #46 merged)
- POLISH-02 — DONE (PR #48 merged)
- POLISH-03 — DONE (PR #49 merged)
- POLISH-04 — DONE (PR #50 merged)
- POLISH-05 — IN_REVIEW

After POLISH-05 review, run a separate Quality & UX review across analysis and
category quality, default Item UI density, source/original access, notification
quality, and navigation/AI reliability. Do not resume PM-14 or PM-16+ until that
review explicitly passes.

## Product invariant after the milestone

AIInbox should expose the user's content, not its internal model.

Default user-facing surfaces prioritize:

~~~text
what happened / conclusion
→ why this is worth opening
→ where the original is
~~~

Internal fields such as:

- ItemType;
- priority score;
- interest;
- attention score;
- ranking reason;
- analysis completeness details

remain available to the system and, where useful, behind Details. They are not the default presentation.

## Freeze policy

Until POLISH-01…05 are accepted and reviewed together:

- PM-14 Hybrid Semantic Search: ON HOLD;
- PM-16+ feature expansion: ON HOLD;
- bug/security/reliability fixes remain allowed;
- PM-15 remains supported and DONE.

This is a product sequencing decision, not a hard technical dependency.

After POLISH-05, review real usage and explicitly decide whether to resume PM-14.

## Cross-PR invariants

All five PRs must preserve:

- SQLite as canonical storage;
- thin Telegram handlers;
- durable jobs/outbox boundaries;
- no network calls while holding SQLite transactions;
- existing prompt-injection protections;
- source provenance;
- deterministic priority/attention formulas unless an explicit PR changes them;
- PM-11 reminder attribution semantics;
- PM-13 citation validation;
- PM-15 export/backup separation;
- no Redis/Celery/vector DB introduced as part of polish.

## Success criteria for the milestone

The milestone is complete when:

1. a long article/video immediately communicates its conclusion/result;
2. categories describe content rather than the user's profession;
3. the default READY card is compact;
4. system scores/details are hidden by default;
5. primary actions return the user to content;
6. every important resurfacing surface exposes source/original access;
7. reminder hooks are concrete and grounded rather than generic;
8. generic nudges feel human without inventing facts;
9. main bot actions are discoverable without memorizing slash commands;
10. repeated Ask/provider failures can be safely diagnosed;
11. real user review confirms the current core is worth extending before PM-14 resumes.
