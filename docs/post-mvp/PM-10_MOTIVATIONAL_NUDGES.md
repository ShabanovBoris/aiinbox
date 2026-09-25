# PM-10 — Motivational Nudges

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-08 Attention Intensity; PM-07 Attention Ranking

Status: DONE

## 1. Epic

### Problem

Not every useful intervention needs to point to one specific Item.

Sometimes the best prompt is a truthful summary of the user's backlog/progress:

> You have three high-value tasks under 20 minutes.

or:

> You added more than you completed today.

The system should support Duolingo-like motivation without fabricating activity or turning the bot into generic quote spam.

### Goal

Add optional generic motivational nudges based only on deterministic facts derived from AIInbox data.

The first version should be template-based and require no LLM.

## 2. In scope

- generic_motivation_enabled setting;
- MotivationService;
- deterministic fact computation;
- nudge eligibility;
- several nudge kinds;
- template variation;
- integration with PM-08 notification budget;
- durable Reminder rows;
- tests.

## 3. Out of scope

- motivational quotes unrelated to user data;
- web content;
- LLM-generated fake metrics;
- psychological profiling;
- streak gamification with rewards/currency;
- push notifications outside Telegram;
- changing PM-07 Item scores.

## 4. Setting

Extend settings_json:

~~~json
{
  "generic_motivation_enabled": true
}
~~~

The user can disable generic nudges while keeping Item-specific proactive attention enabled.

Expose in /settings attention.

Rollout: application defaults enable generic motivation for new users. Migration
adds an explicit `false` for existing users only when the key is absent, and
preserves any existing explicit value.

## 5. Nudge types

Initial deterministic types:

~~~text
STALE_IMPORTANT
HIGH_INTEREST_STALE
QUICK_WINS
INBOX_GROWTH
COMPLETION_STREAK
WEEKLY_PROGRESS
~~~

Do not add a generic RANDOM_MOTIVATION kind.

## 6. Fact definitions

All metrics use the user's timezone where calendar-day semantics matter.

### STALE_IMPORTANT

Condition example:

~~~text
READY + ACTIVE actionable
priority_score >= 75
age >= 30 days
count >= 1
~~~

Fact:
- count.

### HIGH_INTEREST_STALE

~~~text
interest_level = 3
READY + ACTIVE actionable
age >= 14 days
count >= 1
~~~

### QUICK_WINS

~~~text
READY + ACTIVE actionable
estimated_action_minutes <= 20
priority_score >= 60
count >= 3
~~~

### INBOX_GROWTH

For current local day:

~~~text
created_count - resolved_count >= 3
~~~

resolved_count:
- DONE;
- ARCHIVED.

### COMPLETION_STREAK

Consecutive local calendar days with at least one DONE.

Minimum for a nudge:

~~~text
3 days
~~~

### WEEKLY_PROGRESS

Over last 7 local calendar days:
- completed count >= configurable/code threshold, initial 5.

Do not claim percentages unless denominator semantics are defined and tested.

## 7. Motivation candidate

Return a deterministic value object:

~~~text
MotivationCandidate:
  kind
  score
  facts
  template_id
  rendered_text
~~~

Score is only for choosing among nudge types, not Item priority.

## 8. Initial nudge priority

Recommended order when several are eligible:

1. STALE_IMPORTANT;
2. HIGH_INTEREST_STALE;
3. QUICK_WINS;
4. COMPLETION_STREAK;
5. INBOX_GROWTH;
6. WEEKLY_PROGRESS.

Use a small deterministic score/table.

No LLM ranking.

## 9. Templates

Each kind should have several short templates.

Example STALE_IMPORTANT:

~~~text
"У тебя {count} важных Item старше месяца. Сегодня достаточно вернуть в фокус один."

"{count} важных вещей давно лежат без движения. Не нужно закрывать всё — выбери одну."
~~~

Example QUICK_WINS:

~~~text
"В backlog есть {count} задач до 20 минут. Одна небольшая победа заметно разгрузит список."
~~~

Example STREAK:

~~~text
"{days} дня подряд ты закрывал хотя бы один Item. Продолжим серию?"
~~~

Templates may be in the user's preferred language only if explicitly maintained.

For v1, use the bot's supported product language and do not call an LLM merely for translation.

## 10. Truthfulness

Every interpolated fact must come from deterministic DB queries.

No template may imply:
- a streak that was not computed;
- an unread Item count that was not queried;
- a “best week” without historical comparison;
- emotional state;
- productivity judgement.

Avoid shame-oriented language.

The product should create momentum, not fabricate guilt.

## 11. Interaction with intensity

Generic nudges share PM-08 daily budget.

Initial generic limits:

| Intensity | Generic nudges/day |
|---|---:|
| 1 | 0 |
| 2 | max 1 |
| 3 | max 1 |
| 4 | max 1 |
| 5 | max 2 |

Additional rules:
- never send two generic nudges consecutively;
- Item-specific reminder wins at levels 1–3 when a candidate >= PM-08 threshold exists;
- at levels 4–5, a nudge may consume an available slot but still cannot exceed generic cap.

Arbitration is deterministic: at levels 1–3 a sendable proactive candidate
wins; at levels 4–5 proactive and generic sends alternate according to the last
successful Attention-family Reminder, falling back to the other candidate only
when the preferred type is unavailable. After digest and snooze phases, no more
than one Attention-family attempt is made for a user in one worker cycle.

## 12. Reminder type

Add:

~~~text
MOTIVATION_NUDGE
~~~

Reminder.item_id is NULL.

payload_json:

~~~json
{
  "kind": "QUICK_WINS",
  "facts": {"count": 5},
  "template_id": "quick_wins_v2",
  "policy_level": 4
}
~~~

Because item_id is NULL, durable uniqueness/idempotency must explicitly handle user-level reminders.

If necessary, add a SQLite partial unique index for user/type/scheduled slot where item_id IS NULL.

Do not rely on SQL UNIQUE semantics with NULL unless tested.

The implementation uses a local-day + ordinal slot encoded in `scheduled_at`
(local midnight plus slot seconds) and partial unique indexes for the slot and
the user's single open motivation claim.

## 13. Scheduling slot identity

To prevent duplicate generic nudges during repeated worker polls, create a deterministic slot identity per local day.

A practical option:
- derive slot number from number of already-sent budget notifications;
- persist a stable scheduled_at/slot identity before send.

The exact representation may follow Reminder patterns, but duplicate polls/restarts must not create duplicate nudge intent.

## 14. Quiet hours / minimum gap

Generic nudges obey:
- attention_enabled;
- generic_motivation_enabled;
- quiet hours;
- PM-08 minimum gap;
- daily budget.

No catch-up nudge queue after quiet hours.

Only `SENT` reminders consume the shared PM-08 budget/minimum gap and separate
generic cap. Claims are committed before Telegram I/O. Final preparation
revalidates settings, pacing and current facts, updates the bounded payload/text
to the current candidate or cancels the claim, then releases SQLite before send.
Recovery increments claim generation; a stale owner cannot finalize a newer
claim. Telegram and SQLite cannot provide exactly-once delivery, so the
existing bounded at-least-once crash window remains.

## 15. Nudge repetition

Do not send the same nudge kind/template repeatedly.

Use Reminder history to avoid:
- same kind twice consecutively;
- same template for same kind twice consecutively when alternatives exist.

Facts may be re-evaluated each time.

## 16. Interaction with PM-09 hooks

Generic nudges do not use ATTENTION_HOOK.

PM-09 is Item-specific.
PM-10 is user/backlog-level.

Keep these paths separate.

## 17. Tests

### Facts

Exact tests for:
- stale important count;
- high-interest stale;
- quick wins;
- day-local inbox growth;
- streak across timezone boundaries;
- last-7-day progress.

### Truthfulness

- no eligible fact -> no nudge;
- counts in text equal DB result;
- no invented metric.

### Policy

- intensity 1 -> none;
- intensity 3 -> max one generic/day;
- intensity 5 -> max two;
- generic disabled -> none;
- quiet hours -> none;
- daily budget reached -> none;
- no consecutive generic nudges.

### Idempotency

- repeated worker poll does not duplicate same slot;
- restart does not reset generic daily count.

## 18. Acceptance criteria

1. Nudges are based only on real AIInbox facts.
2. User can disable generic motivation independently.
3. Calm mode sends no generic nudges.
4. Aggressive mode still caps generic nudges at two/day.
5. generic nudges share the PM-08 budget/gap.
6. duplicate polls/restarts do not duplicate a nudge.
7. no LLM is required.
8. quality gate passes.

## 19. Definition of Done

- setting/UI;
- MotivationService;
- deterministic facts;
- templates;
- MOTIVATION_NUDGE persistence;
- worker integration;
- tests;
- BOT_USAGE updated;
- no PM-11 feedback implementation early;
- repository review completed.

## 20. POLISH-04 current behavior

Every existing `MotivationKind` has four maintained, concise Russian copy
variants. These templates interpolate only the facts already produced by
`MotivationService`; fact queries, thresholds and `_KIND_SCORES` are unchanged.
There is no LLM call, source content, Item summary, or profile context on this
path.

Successful `SENT` Reminder history advances through the ordered variants and
wraps deterministically. Missing, unknown, or legacy template IDs start at the
first current variant. Failed or open claims do not advance rotation, and the
existing same-day kind suppression and notification caps remain authoritative.
