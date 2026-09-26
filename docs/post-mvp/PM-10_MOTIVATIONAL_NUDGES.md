# PM-10 — Motivational Nudges

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-08 Attention Intensity; PM-07 Attention Ranking

Status: DONE

## 1. Epic

### Problem

Motivation signals can summarize Item/Event facts, but a user-facing reminder
must return a concrete saved material. The signal remains an internal reason to
select a focus; the message shows that saved material rather than a backlog or
progress report.

### Goal

Keep optional motivational nudges based only on deterministic facts derived from
AIInbox data. Every sendable candidate is paired with one eligible saved
material in existing PM-07 order. Presentation remains LLM-free.

## 2. In scope

- generic_motivation_enabled setting;
- MotivationService;
- deterministic fact computation;
- nudge eligibility;
- several deterministic nudge kinds;
- pairing each candidate with a concrete PM-07-ranked focus;
- integration with PM-08 notification budget;
- durable Reminder rows;
- source and lifecycle actions for the focused material;
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
  focus_item_id
~~~

Score chooses among motivation signals, not Item priority. `focus_item_id`
identifies the best eligible Item in existing PM-07 order. Telegram text is
projected later from the saved Item; the candidate contains no rendered copy.

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

## 9. Presentation

New motivation messages use the focused Item's saved title and bounded summary
through the existing proactive reminder formatter. They expose that material's
Original/source actions and supported lifecycle controls. “Не сейчас” is shown
only for proactive reminders because its same-Item cooldown applies to that
Reminder type. “Меньше таких” remains available for motivation-kind feedback.

No aggregate fact, template id, rendered template, or motivation-kind reason is
shown to the user. If there is no eligible focus, no candidate is sent. If the
Item has no summary, the formatter shows its title only. No LLM is used to write
or translate this message.

## 10. Truthfulness

Every interpolated fact must come from deterministic DB queries.

No motivation signal or message may imply:
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
- keep the four-hour generic repeat gap;
- Item-specific reminder wins at levels 1–3 when a candidate >= PM-08 threshold exists;
- at levels 4–5, a nudge may consume an available slot but still cannot exceed generic cap.

Arbitration is deterministic: at levels 1–3 a sendable proactive candidate
wins. At levels 4–5 the scheduler prefers variety based on the last successful
Attention-family Reminder, while a generic candidate may compete again after
four hours without requiring an intervening proactive send. The generic cap and
shared budget/gap remain authoritative. After digest and snooze phases, no more
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
  "focus_item_id": 123,
  "policy_level": 4,
  "local_date": "2026-09-24",
  "slot": 1
}
~~~

Because item_id is NULL, durable uniqueness/idempotency must explicitly handle user-level reminders.
`focus_item_id` in the payload attributes new messages and callbacks to their
selected save without changing the Reminder's user-level claim identity.

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
revalidates settings, pacing, current facts and focused Item; it updates the
bounded payload to the current candidate or cancels the claim, then releases
SQLite before send.
Recovery increments claim generation; a stale owner cannot finalize a newer
claim. Telegram and SQLite cannot provide exactly-once delivery, so the
existing bounded at-least-once crash window remains.

## 15. Nudge repetition

Reminder history suppresses a motivation kind already sent on the current local
day. “Меньше таких” suppresses that kind for seven elapsed days. The scheduler
also enforces the four-hour generic repeat gap. Facts and focus are re-evaluated
at final preparation; there is no user-facing template rotation.

## 16. Interaction with PM-09 hooks

Generic motivation does not use `ATTENTION_HOOK`. PM-09 hooks remain
proactive-only; focused motivation shows the saved summary as presentation and
keeps source content and profile data out of the motivation copy path.

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
- every new send has an eligible `focus_item_id`;
- user-facing text comes from that Item's saved title and summary;
- no aggregate metric or motivation reason is exposed.

### Policy

- intensity 1 -> none;
- intensity 3 -> max one generic/day;
- intensity 5 -> max two;
- generic disabled -> none;
- quiet hours -> none;
- daily budget reached -> none;
- generic repeat gap is four hours;
- opening More for focused motivation does not expose the proactive-only dismiss action.

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
7. Every sendable candidate is paired with a concrete saved material in PM-07 order.
8. User-facing text and actions resolve to that material; no focus means no send.
9. No LLM is required, and generic motivation does not create Attention exposure.
10. Reminder claim identity remains user-level with `item_id=NULL`.
11. Quality gate passes.

## 19. Definition of Done

- setting/UI;
- MotivationService;
- deterministic facts;
- concrete focus selection and summary-based presentation;
- MOTIVATION_NUDGE persistence;
- worker integration;
- tests;
- BOT_USAGE updated;
- PM-11 feedback attribution;
- repository review completed.

## 20. Current implementation

Motivation kinds and deterministic facts remain internal selection signals.
Each new sendable `MotivationCandidate` includes a `focus_item_id` chosen in
existing PM-07 order. Its durable `MOTIVATION_NUDGE` Reminder keeps
`Reminder.item_id=NULL`; the focus id lives in the existing payload and is used
for owner-scoped source/lifecycle callbacks and focused Reminder Events.

The message uses the focused Item's title and persisted summary, with no
template rotation or LLM. A missing eligible focus produces no send. Historical
focusless Reminder rows remain valid and are not rewritten. Fact thresholds,
motivation scores, daily caps, four-hour repeat pacing, durable claims, and
PM-11 feedback remain governed by the current product spec and tests.
