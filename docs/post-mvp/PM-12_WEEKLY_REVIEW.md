# PM-12 — Weekly Review

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-06 Behaviour Ranking; PM-11 Reminder Feedback Loop

## 1. Epic

### Problem

Daily attention answers:

> What should I do now?

It does not answer:

> What is happening to my attention and backlog over time?

The user needs a compact weekly reflection, not another long backlog dump.

### Goal

Add /weekly: a deterministic seven-day review of:
- capture;
- completion;
- archive/cleanup;
- active themes;
- stale backlog;
- repeated postponement;
- reminder outcomes;
- up to three concrete next recommendations.

The first version should not require an LLM.

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
- deterministic metrics;
- category summaries;
- stale/neglected analysis;
- reminder outcome metrics;
- max three recommendations;
- concise Telegram formatting;
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

Report count.

### Old important never revisited

Initial:

~~~text
priority_score >= 75
age >= 30 days
no TODAY_SHOWN / ATTENTION_SHOWN / successful PROACTIVE_ATTENTION in last 14 days
~~~

Report count and optionally top 1–3 titles.

## 6. Category metrics

Show at most top three categories for:
- Items created in the period;
- Items completed in the period.

Do not show dozens of categories.

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

When PM-11 data exists, report compactly:

~~~text
Attention reminders:
sent N
opened O
done D
dismissed X
disliked Y
~~~

Avoid percentages for very small sample sizes.

If sent < 5:
- show raw counts only;
- do not infer effectiveness.

## 8. Backlog trend

Compute:

~~~text
net_change = created - completed - archived
~~~

Wording:
- positive: backlog grew by N;
- negative: backlog shrank by N;
- zero: roughly balanced.

Do not label growth as failure.

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

Wording must be:

> Check whether this is still relevant.

Do not auto-recommend “archive” as a certainty.

Ensure recommendations use distinct Items.

## 10. Example output

~~~text
Неделя

Добавлено: 38
Готово: 17
Архивировано: 6
Backlog: +15

Активные темы:
AI — 11
Android — 7
Finance — 5

Старый backlog:
21 Item старше 30 дней
4 важных давно не возвращались в фокус

Чаще откладывал:
Piano

Attention:
8 reminders · 2 done · 1 disliked

На следующую неделю:
1. Вернуться к <Item>
2. Закрыть быстрый <Item> (~15 мин)
3. Проверить актуальность <Item>
~~~

Omit empty sections rather than printing zeros everywhere.

## 11. Data access

Avoid N+1 queries.

WeeklyReviewService should use bounded aggregate queries and reuse:
- PM-07 ranking service;
- Event;
- Reminder;
- Item.

Do not build a data warehouse.

## 12. User language

Use existing bot/product language in v1.

If later localized, all computed facts remain language-neutral and only formatting changes.

No LLM translation required.

## 13. /weekly Telegram command

- allowlisted user only;
- command performs reads/aggregation;
- no heavy external calls;
- result should fit Telegram message limits.

If content is too large:
- trim low-value sections;
- do not split into ten messages.

## 14. Exposure side effects

/weekly should not mark every mentioned Item as TODAY_SHOWN.

If recommendations are considered attention exposures, use a dedicated event only for recommended Item ids, e.g.:

~~~text
WEEKLY_SHOWN
~~~

PM-06 ignores it initially.

This preserves semantics.

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

- empty sections omitted;
- bounded Telegram length;
- no fabricated percentages.

## 16. Acceptance criteria

1. /weekly summarizes real seven-day activity.
2. stale/neglected backlog is visible.
3. category insights require sufficient evidence.
4. reminder outcomes are factual.
5. max three deterministic recommendations.
6. no LLM is required.
7. command does not mutate semantic ranking.
8. output remains compact.
9. quality gate passes.

## 17. Definition of Done

- WeeklyReviewService;
- /weekly;
- aggregate queries;
- formatter;
- tests;
- BOT_USAGE update;
- no scheduled weekly push unless separately approved;
- repository review completed.
