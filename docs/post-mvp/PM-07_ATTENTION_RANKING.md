# PM-07 — Attention Ranking Engine

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-01 Interest Level; PM-06 Behaviour-Aware Ranking
Status: IN_REVIEW

## 1. Epic

### Problem

priority_score answers:

> How important is this Item?

It does not answer:

> Which Item deserves attention right now?

A new priority-90 Item may not need another reminder today, while a priority-80 Item saved 50 days ago and never revisited may be the better candidate.

### Goal

Create a deterministic dynamic Attention Ranking Engine combining:
- semantic priority;
- PM-06 personal rank;
- manual interest;
- age;
- neglect;
- due-date pressure;
- recent-show suppression.

PM-07 ranks candidates only.
Automatic sending belongs to PM-08.

## 2. Core distinction

~~~text
priority_score  = semantic importance
personal_rank   = semantic priority adjusted by behaviour
attention_score = what deserves attention now
~~~

attention_score is time-dependent and must not overwrite priority_score.

## 3. In scope

- AttentionRankingService;
- candidate eligibility;
- deterministic formula;
- age bonus;
- neglect bonus;
- interest adjustment;
- due-date bonus;
- stale-important bonus;
- recent-show penalty;
- explainability;
- on-demand /attention preview;
- tests.

## 4. Out of scope

- automatic notifications;
- intensity;
- generic nudges;
- LLM hooks;
- reminder feedback;
- calendar;
- ML.

## 5. Eligibility

Initial candidates:

~~~text
processing_status = READY
state = ACTIVE
item_type in ACTION, LEARN, READ, WATCH
~~~

Exclude:
- SNOOZED;
- DONE;
- ARCHIVED;
- FAILED/PROCESSING;
- REFERENCE;
- IDEA;
- SOMEDAY.

This deliberately begins with TodayService actionable semantics.

## 6. Base

~~~text
base = personal_rank
~~~

No behaviour history means personal_rank equals priority_score.

## 7. Manual interest adjustment

| interest_level | Adjustment |
|---|---:|
| 1 | -8 |
| 2 | 0 |
| 3 | +8 |

## 8. Age bonus

| Age | Bonus |
|---|---:|
| <7 days | 0 |
| 7–30 days | linear 0..4 |
| 30–90 days | linear 4..10 |
| >90 days | 12 max |

Implement interpolation as a pure tested function.

## 9. Attention history

Derive last shown from the latest user-scoped `TODAY_SHOWN` or
`ATTENTION_SHOWN` Event for the Item. `CREATED` and the initial READY result
delivery are not exposure. SQLite naive timestamps are interpreted as UTC; all
intervals use elapsed UTC time.

Both exposure Events represent a successfully delivered Telegram response.
`/today` records its selected Items after the combined list is sent; `/attention`
records each Item after its card is sent.

Derived value:

~~~text
last_shown_at
~~~

Never shown remains NULL/unknown.

## 10. Neglect bonus

| Time since last shown | Bonus |
|---|---:|
| shown <7 days ago | 0 |
| 7 days <= elapsed <14 days | +3 |
| 14 days <= elapsed <=30 days | +6 |
| >30 days | +10 |
| never shown and Item age >=14 days | +8 |

Never shown + age <14 days -> 0.

## 11. Recent-show penalty

| Last shown | Penalty |
|---|---:|
| <24h | -25 |
| 1 day <= elapsed <3 days | -15 |
| 3 days <= elapsed <7 days | -8 |
| 7 days <= elapsed <14 days | -3 |
| >=14 days / never | 0 |

Recent suppression should normally dominate age bonus.

## 12. Due-date bonus

Use persisted suggested_due_at only.

| Due relation | Bonus |
|---|---:|
| overdue | +10 |
| now <= due_at <= now +3 days | +7 |
| now +3 days < due_at <= now +7 days | +4 |
| later / none | 0 |

Do not infer new dates here.

## 13. Stale-important bonus

Condition:

~~~text
priority_score >= 75
AND age >= 30 days
AND not shown in last 14 days
~~~

Bonus:

~~~text
+8
~~~

## 14. Formula

~~~text
attention_score =
    personal_rank
  + interest_adjustment
  + age_bonus
  + neglect_bonus
  + due_bonus
  + stale_important_bonus
  + recent_show_penalty
~~~

Final:

~~~text
clamp(round-half-away-from-zero(attention_score), 0, 100)
~~~

Age and exposure elapsed time cannot be negative. The public ranking call accepts
an injectable UTC `now`; production captures it once per ranking operation.

## 15. Ordering

1. attention_score DESC;
2. priority_score DESC;
3. created_at ASC;
4. id ASC.

Old Items win exact ties.

## 16. Explainability

Return:

~~~text
AttentionRank:
  score
  personal_rank
  interest_adjustment
  age_bonus
  neglect_bonus
  due_bonus
  stale_important_bonus
  recent_show_penalty
  last_shown_at
  age_days
~~~

Generate deterministic reason text, e.g.:

> High priority, strong interest, waiting 47 days, not shown for 19 days.

No LLM.

## 17. Storage

Do not add canonical attention_score column.

It depends on current time/history/policy.
Persist source facts/events only.

## 18. /attention preview

Add:

~~~text
/attention
~~~

Default:
- top 3;
- max 5.

Show:
- title;
- attention score;
- priority;
- interest;
- age;
- short reason;
- existing actions/open buttons.

Recommended new exposure event:

~~~text
ATTENTION_SHOWN
~~~

This distinguishes attention preview from TODAY_SHOWN.

PM-06 ignores it as a preference signal. The command sends one card per Item
with the existing `item_keyboard` and ItemSource-aware actions. It records one
`ATTENTION_SHOWN` only after that card was accepted by Telegram. Each Event
commits independently, and the SQLite read transaction is closed before any
Telegram send. `/today` likewise records `TODAY_SHOWN` only after its combined
message was accepted, without changing TodayService selection or ordering.

## 19. TodayService

Do not replace /today in PM-07.

Keep:
- /today = current simple semantic list;
- /attention = dynamic attention preview.

Later evidence can justify convergence.

## 20. Tests

### Scoring boundaries

Exact tests for:
- age;
- neglect;
- recent-show;
- due-date;
- interest;
- stale-important;
- clamp.

### Filtering

- READY ACTIVE actionable included;
- REFERENCE/IDEA/SOMEDAY excluded;
- SNOOZED/DONE/ARCHIVED excluded.

### Ordering

- old neglected Item can outrank newer higher-priority Item;
- recently shown Item is suppressed;
- high interest is bounded;
- deterministic tie-break.

### Integration

- user-scoped history;
- /attention result;
- exposure event;
- priority_score unchanged.

## 21. Acceptance criteria

1. Attention score is dynamic and explainable.
2. priority_score stays untouched.
3. old important neglected Items can rise.
4. recently shown Items are suppressed.
5. lifecycle exclusions are respected.
6. /attention previews ranking without unsolicited messages.
7. no LLM/ML required.
8. full quality gate passes.

## 22. Definition of Done

- scoring service;
- preview retrieval;
- Telegram /attention;
- reason formatter;
- tests;
- no PM-08 scheduler work;
- BOT_USAGE updated;
- repository review workflow completed.
