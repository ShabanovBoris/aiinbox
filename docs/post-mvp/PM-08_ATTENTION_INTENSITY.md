# PM-08 — Attention Intensity and Proactive Scheduling

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-07 Attention Ranking Engine

Status: IN_REVIEW

## 1. Epic

### Problem

The system can rank what deserves attention but still needs a controlled interruption policy.

The user wants modes from calm one-reminder-per-day to explicitly aggressive five-to-six-reminders-per-day.

### Goal

Add proactive Item resurfacing with:
- attention_enabled;
- attention_intensity 1..5;
- daily caps;
- minimum gaps;
- same-Item cooldown;
- quiet hours;
- durable Reminder rows;
- restart-safe scheduling.

PM-08 sends Item-specific reminders only.
Generic motivation is PM-10.

## 2. Settings

Extend settings_json:

~~~json
{
  "attention_enabled": true,
  "attention_intensity": 3
}
~~~

Valid intensity: 1..5.
Default intensity: 3.

Rollout safety:
- existing users must not receive an unexpected burst after deployment;
- implementation may require explicit activation for existing accounts while preserving default intensity 3;
- document the rollout behavior.

Do not expose internal coefficients.

## 3. Policy table

| Level | Label | Max proactive/day | Minimum gap | Same-item cooldown |
|---|---|---:|---:|---:|
| 1 | Calm | 1 | 8h | 72h |
| 2 | Light | 2 | 5h | 48h |
| 3 | Normal | 3 | 3h | 30h |
| 4 | Active | 4 | 2h | 20h |
| 5 | Aggressive | 6 | 90m | 12h |

These are code-level policy constants.

## 4. Budget semantics

Count toward the local-day proactive budget:
- PROACTIVE_ATTENTION;
- DAILY_DIGEST;
- later PM-10 MOTIVATION_NUDGE.

Snooze resurfacing is user-requested:
- does not consume proactive quota;
- still respects quiet hours.

PM-08 implements budget accounting for proactive reminders + existing digest.

## 5. Reminder type

Add:

~~~text
PROACTIVE_ATTENTION
~~~

Payload snapshot example:

~~~json
{
  "attention_score": 91,
  "priority_score": 82,
  "interest_level": 3,
  "policy_level": 3,
  "reason": "High priority; waiting 44 days"
}
~~~

No full Item content.

## 6. Scheduling model

Reuse ReminderWorker.

No APScheduler/Celery/Redis.

Each poll for a user:

1. load settings/timezone;
2. skip if disabled;
3. skip quiet hours;
4. count successful budget-consuming notifications for local day;
5. enforce cap;
6. enforce min gap;
7. request PM-07 candidates;
8. exclude same-Item cooldown;
9. require quality threshold;
10. durably claim one reminder;
11. commit claim according to existing idempotency policy;
12. send;
13. record final state.

Only one proactive reminder per user per worker cycle.

## 7. Local-day semantics

Daily cap follows configured timezone.

Timezone change must not duplicate already-sent reminders.

Use current digest local-day conventions where appropriate.

## 8. Quiet hours

Never send proactive attention during quiet hours.

Do not queue catch-up spam.

After quiet hours, choose the best current candidate under normal policy.

## 9. Minimum gap

Measure from the latest successfully sent notification that interrupts the
user:
- DAILY_DIGEST;
- PROACTIVE_ATTENTION;
- SNOOZE_RESURFACE.

Only DAILY_DIGEST and PROACTIVE_ATTENTION consume the daily budget. An active
CLAIMED delivery of any of these three types is a cross-worker reservation:
proactive scheduling waits for it to resolve before checking the successful
send history again.

Failed delivery does not count as successful-send gap, but retries remain bounded.

## 10. Same-item cooldown

Use durable Reminder history.

If latest successful proactive reminder for Item is within cooldown:
- skip it;
- consider next candidate.

Restart must not reset cooldown.

## 11. Candidate selection

Request top N (e.g. 10) from PM-07, then apply scheduling filters.

Do not duplicate ranking formula in notification code.

## 12. Minimum quality threshold

Initial:

~~~text
attention_score >= 60
~~~

Intensity controls frequency, not permission to send junk.

No qualifying candidate -> no notification.

## 13. Telegram UX

Initial reminder:

~~~text
⏳ Вернём это в фокус

<Title>

Почему сейчас:
<deterministic attention reason>

Priority: 82
Interest: 3
Saved: 44 days ago
~~~

Reuse existing source/action controls:
- Open/send media;
- Done;
- Later;
- Archive.

PM-09 enriches the reason.

## 14. Settings UI

Add:

~~~text
/settings attention
~~~

Example:

~~~text
Attention Manager: ON
Intensity: 3 — Normal
Up to 3 proactive reminders/day

[1 Calm]
[2 Light]
[3 Normal ✓]
[4 Active]
[5 Aggressive]
[On/Off]
~~~

Preserve existing settings commands/callbacks.

## 15. Restart/idempotency

Durable state must prevent restart from:
- resetting quota;
- resetting cooldown;
- immediately duplicating a recorded reminder.

Use SQLite claims, not memory.

## 16. Concurrency

Two worker attempts must not claim the same reminder.

Use uniqueness/conditional write.

No in-memory lock as correctness boundary.

## 17. Snooze interaction

Later -> existing SNOOZED state is authoritative.

When snooze resurfaces:
- no immediate second proactive reminder because min-gap applies.

## 18. Tests

### Settings
- default intensity;
- invalid values;
- toggle;
- restart persistence.

### Policy
Exact each-level cap/gap/cooldown tests.

### Worker
- disabled;
- quiet hours;
- no candidate;
- successful candidate;
- cap reached;
- gap not reached;
- digest consumes budget;
- same Item cooldown;
- next candidate chosen.

### Recovery
- restart preserves cap/cooldown;
- duplicate claim does not create duplicate intent.

## 19. Acceptance criteria

1. Intensity 1..5 works.
2. Level 1 max one proactive/day.
3. Level 5 max six/day and respects gap.
4. quiet hours always respected.
5. digest participates in budget.
6. same Item respects cooldown.
7. low-score candidate is not sent to fill quota.
8. anti-duplicate state survives restart.
9. PM-07 is the single rank source.
10. quality gate passes.

## 20. Definition of Done

- settings/UI;
- reminder type;
- policy;
- worker integration;
- durable claims;
- tests;
- BOT_USAGE/RUNBOOK as needed;
- no PM-09/10 scope creep;
- repository review completed.

## 21. Implemented delivery details

- New users default to `attention_enabled=true` and intensity 3. The migration
  explicitly sets existing users to OFF only when that key is absent, preserving
  all saved settings and any existing PM-08 values.
- The local-day cap counts successful `DAILY_DIGEST` and `PROACTIVE_ATTENTION`
  Reminder rows in the user's current IANA timezone. `SNOOZE_RESURFACE` does not
  use quota, but it anchors the minimum gap. DST days use their actual UTC span.
- A five-minute durable lease and partial unique index allow only one open
  proactive intent per user. Digest, snooze, and proactive claim transactions
  serialize per-user send intents under SQLite `BEGIN IMMEDIATE`. Every claim
  records `claimed_at` and a generation; recovered proactive work increments
  the generation so a stale sender cannot finalize the new owner's claim.
- The two-minute Telegram deadline is anchored to `claimed_at`, leaving three
  minutes before lease recovery. A worker delayed past that deadline marks its
  claim failed without entering Telegram; retries cannot restart the window.
  All network I/O runs after the claim transaction commits.
- Immediately before proactive delivery, the worker recomputes the selected
  actionable Item's PM-07 rank under the serialized prepare transaction. It
  cancels a claim if the Item is no longer eligible or its current score is
  below 60, and snapshots the validated score/reason into the Reminder.
- Recovery re-ranks through PM-07 and cancels an intent whose Item is no longer
  actionable or whose current attention score is below 60. `/attention` still
  uses its existing one-to-five preview limit; scheduling can inspect ten.
- After Telegram confirms a proactive message, `Reminder.SENT` and the
  `ATTENTION_SHOWN` exposure Event are committed together. If Telegram accepts
  a message but its response is lost, or the process stops before this
  transaction commits, recovery may retry it: Telegram and SQLite cannot
  provide exactly-once delivery across that boundary. A stale generation cannot
  overwrite a claim already recovered by another worker.
