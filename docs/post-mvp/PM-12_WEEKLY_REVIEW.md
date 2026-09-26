# PM-12 — Weekly Review

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-06 Behaviour Ranking; PM-11 Reminder Feedback Loop

Status: DONE (PR #41 merged)

## 1. Epic

### Problem

`WeeklyReview` needs deterministic facts about the previous week and current
backlog. Ordinary Telegram, however, should return the user to useful saved
content rather than report the system's aggregate analytics.

The user-facing `/weekly` is a compact list of concrete materials worth
revisiting, not another long backlog dump or a productivity report.

### Goal

Build an internal deterministic seven-day read model of:
- capture;
- completion;
- archive/cleanup;
- active themes;
- stale backlog;
- repeated postponement;
- reminder outcomes;
- up to three concrete next recommendations.

The Telegram projection exposes only up to three existing concrete
recommendations. Aggregate facts remain internal and are never a fallback when
there are no recommendations. The first version does not require an LLM.

## 2. Time window

Use the user's timezone.

Primary reporting window:

~~~text
last 7 local calendar days including today
~~~

This avoids ambiguous partial calendar-week semantics.

Also compute backlog age/staleness from current state.

## 3. In scope

- WeeklyReviewService;
- /weekly command;
- deterministic aggregate metrics kept in the internal read model;
- category and stale/neglected analysis kept internal;
- reminder outcome metrics kept internal;
- at most three existing deterministic recommendations in Telegram;
- concise Russian, object-centric Telegram formatting;
- tests.

## 4. Out of scope

- scheduled weekly delivery in v1;
- charts/images;
- external productivity data;
- LLM-written performance judgement;
- goals coaching beyond stored AIInbox facts;
- comparing the user to other people.

A scheduled weekly notification can be added later if on-demand review proves useful.

## 5. Core metrics

### Flow

For last 7 local days:
- Items created;
- Items completed;
- Items archived.

### Current backlog

- READY + ACTIVE actionable count;
- active high-priority count;
- active interest_level=3 count.

### Stale backlog

Initial:

~~~text
READY + ACTIVE actionable
age >= 30 days
~~~

Compute the count in `WeeklyReview`; do not show it in ordinary Telegram copy.

### Old important never revisited

Initial:

~~~text
priority_score >= 75
age >= 30 days
no TODAY_SHOWN / ATTENTION_SHOWN / successful PROACTIVE_ATTENTION in last 14 days
~~~

Compute the count internally. A selected saved material may appear as one of the
concrete recommendations; never show the aggregate count or a numbered top list.

## 6. Category metrics

Compute at most the top three categories in the internal read model for:
- Items created in the period;
- Items completed in the period.

Do not render category counts in the Telegram projection.

### Most postponed category

Use SNOOZED events in the seven-day window.

Require a minimum of 2 snooze events before calling a category “most postponed”.

If threshold is not met:
- omit this insight.

### Strongest progress category

Use DONE count in the period.

Require at least 2 completions.

Do not claim “best progress” from one completed Item.

## 7. Reminder metrics

When PM-11 data exists, compute compact raw counts internally:

~~~text
Attention reminders:
sent N
opened O
done D
dismissed X
disliked Y
~~~

Do not show these counts or percentages in the ordinary Telegram projection.
Avoid percentages for very small sample sizes if an explicit diagnostics surface
is added later.

If sent < 5:
- retain factual counts only in the internal read model;
- do not infer effectiveness.

## 8. Backlog trend

Compute:

~~~text
net_change = created - completed - archived
~~~

Internal interpretation:
- positive: backlog grew by N;
- negative: backlog shrank by N;
- zero: roughly balanced.

The current Telegram projection never displays this trend. If a future explicit
diagnostics surface exposes it, do not label growth as failure.

## 9. Recommendations

Maximum three and all deterministic.

Candidate classes:

### Return to one old important Item

Use top PM-07 attention candidate among stale/high-priority Items.

### Quick win

READY + ACTIVE actionable:
- estimated_action_minutes <= 20;
- high current attention/priority.

### Cleanup review

Candidate:
- ACTIVE;
- age >= 90 days;
- interest_level = 1 or explicit NOT_INTERESTING history;
- low/medium priority.

Russian Telegram wording is:

> Проверить, ещё актуален ли он.

Do not auto-recommend “archive” as a certainty.

Ensure recommendations use distinct Items.

## 10. Example output

~~~text
📌 Вернуться на этой неделе

• Kotlin compiler changes
Вернуться к материалу.

• Процесс деплоя на серьёзном проекте
Короткий следующий шаг. ≈ 15 минут

• Старый материал по архитектуре
Проверить, ещё актуален ли он.
~~~

The empty state is:

~~~text
📌 На этой неделе пока нечего отдельно возвращать в фокус.
~~~

Do not fall back to aggregate metrics when no recommendation exists. No weekly
counts, category totals, backlog trend, Reminder outcomes, Item jargon, scores,
or ordinal positions appear in ordinary Telegram messages.

## 11. Data access

Avoid N+1 queries.

WeeklyReviewService should use bounded aggregate queries and reuse:
- PM-07 ranking service;
- Event;
- Reminder;
- Item.

Do not build a data warehouse.

## 12. User language

Ordinary Telegram copy is Russian. Saved titles keep their canonical source
language; generated recommendation descriptions use the existing Russian
product wording.

All computed facts remain language-neutral; only presentation text depends on
the product language.

No LLM translation required.

## 13. /weekly Telegram command

- allowlisted user only;
- command performs reads/aggregation;
- no heavy external calls;
- result should fit Telegram message limits.

The command sends one message containing only the recommendation rows. Keep the
existing Telegram length bound; do not add aggregate sections or split the
report into multiple messages.

## 14. Exposure side effects

`/weekly` is an on-demand read-only recommendation projection, not an analytical
Telegram report or an Attention exposure. It does not write any Event for Items
considered by its internal metrics or recommendations, including
`TODAY_SHOWN`, `ATTENTION_SHOWN`, or `WEEKLY_SHOWN`.

The next `/attention` preview therefore continues to use only actual PM-07/11
exposure history. No schema change is required for PM-12.

## 15. Tests

### Date/time

- seven local calendar days;
- timezone boundary;
- DST-safe local date handling where practical.

### Metrics

- created/done/archive;
- net change;
- stale count;
- old-important-never-revisited;
- top categories;
- postpone threshold;
- progress threshold;
- reminder counts.

### Recommendations

- max 3;
- distinct Items;
- stale important;
- quick win;
- cleanup wording/criteria;
- no recommendation when no eligible Item.

### Formatting

- only concrete existing recommendation titles and short next-step descriptions;
- at most three rows, with no numbering or aggregate fallback;
- neutral empty state when no recommendation exists;
- no Item/Backlog/lifecycle jargon, scores, or aggregate counts;
- bounded Telegram length;
- no fabricated content or unsupported percentages.

## 16. Acceptance criteria

1. `WeeklyReview` computes the documented seven-day and current-backlog facts.
2. Internal category and reminder facts use the documented evidence thresholds.
3. Telegram shows at most three existing `WeeklyRecommendation` rows, each
   linked to its concrete saved material; it exposes no aggregate metrics.
4. When there are no recommendations, Telegram shows the neutral empty state
   without falling back to activity counts.
5. The Russian output is compact and contains no system jargon, scores,
   numbering, or productivity counters.
6. No LLM is required and `/weekly` does not mutate semantic ranking or create
   exposure Events.
7. The quality gate passes.

## 17. Definition of Done

- `WeeklyReviewService` with aggregate facts retained in the internal model;
- `/weekly` with only concrete recommendation rows in its Telegram projection;
- an object-centric formatter with a neutral no-recommendation state;
- tests for metrics, recommendations, and aggregate-free Telegram formatting;
- BOT_USAGE update;
- no scheduled weekly push unless separately approved;
- repository review completed.
