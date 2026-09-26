# PM-11 — Reminder Feedback Loop

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-08 proactive reminders; PM-09 hooks; PM-10 nudges

Status: DONE (PR #40 merged)

## 1. Epic

### Problem

The Attention Manager can send reminders, but without durable reaction data it cannot distinguish:
- useful timing;
- bad timing;
- repeated annoyance;
- a reminder that led to completion.

The loop must become:

~~~text
ranking
→ reminder
→ user reaction
→ durable feedback
→ future scheduling/ranking
~~~

### Goal

Add reminder-specific feedback events and feed simple deterministic suppression signals back into future attention decisions.

## 2. Events

Add:

~~~text
REMINDER_SENT
REMINDER_OPENED
REMINDER_SNOOZED
REMINDER_DONE
REMINDER_DISMISSED
REMINDER_DISLIKED
~~~

Semantics must be precise.

### REMINDER_SENT

Only record when Telegram delivery is considered successful by the current delivery contract.

### REMINDER_OPENED

Record only for bot-mediated source/open actions that AIInbox can actually observe.

Do not record an OPENED event for a plain Telegram URL button click if the bot cannot observe the click.

### REMINDER_SNOOZED

User chose Later from a reminder.

### REMINDER_DONE

User chose Done from the reminder.

The normal DONE lifecycle event still occurs.
REMINDER_DONE identifies the reminder outcome, not a replacement for DONE.

### REMINDER_DISMISSED

User chose “Not now” / dismiss for this reminder without changing Item lifecycle state.

### REMINDER_DISLIKED

User chose “Fewer like this”.

It does not automatically archive the Item.

## 3. Current event identity

`events.item_id` and `events.reminder_id` are nullable, with a constraint that
at least one reference is present. Reminder outcome Events carry
`reminder_id`; Item-specific reminders also carry `item_id`.

`MOTIVATION_NUDGE` keeps `Reminder.item_id=NULL` as its user-level claim key.
New focused motivation sends resolve `focus_item_id` from the Reminder payload
and record that Item on their Reminder Events. Historical focusless motivation
Events remain valid with only `reminder_id`; they are not backfilled.

## 4. Event payload

Keep small.

Example:

~~~json
{
  "reminder_type": "PROACTIVE_ATTENTION",
  "attention_score": 91,
  "category": "AI",
  "item_type": "READ",
  "hook_content_id": 991,
  "template_id": "reason_to_return_v1"
}
~~~

Prefer snapshotting relevant reminder payload facts at send time rather than reading mutable Item fields later when analysing outcomes.

Do not copy full source content.

## 5. Telegram actions

Item-specific proactive and focused motivation reminders expose the saved
material's Original and available source actions, then a compact More menu:

~~~text
[↩️ Оригинал]
[source actions]
[••• Ещё]
~~~

More contains lifecycle and feedback actions supported by that Reminder type.
Focused motivation exposes Later, Done and Fewer like this; it omits Not now,
whose current per-Item cooldown applies only to proactive reminders. Source
actions and callbacks resolve through the saved focus for new motivation sends.
A stale or crafted dismissal callback for motivation is rejected without an
Event because that type has no dismissal cooldown.

### Open

When bot-mediated:
- record REMINDER_OPENED;
- then expose/send the relevant source using current source actions.

### Later

- record REMINDER_SNOOZED;
- apply existing snooze transaction;
- avoid duplicate outcome events on repeated callback.

### Done

- record REMINDER_DONE;
- apply existing DONE lifecycle transition.

### Not now

This action is shown only on proactive Attention reminders, whose scheduler uses
its dismissal cooldown.

- record REMINDER_DISMISSED;
- do not change Item state;
- suppress the same Item for an additional dismissal cooldown.

### Fewer like this

- record REMINDER_DISLIKED;
- do not archive;
- future reminders for similar category/type receive a bounded penalty.

## 6. Transaction semantics

Where one action changes Item lifecycle and records reminder feedback:
- both changes must commit atomically where practical.

Example:
REMINDER_DONE + DONE transition should not leave one durable and the other missing after a normal transaction failure.

Use existing application services rather than SQL inside handlers.

## 7. Idempotency

Callback retries must not duplicate outcome events.

Recommended durable uniqueness:
- one terminal/choice outcome per reminder per outcome family, or
- application-level conditional insert keyed by reminder_id + event_type.

A user may legitimately interact with a reminder more than once only where semantics permit.

Examples:
- OPENED may be recorded once per reminder in v1;
- DISLIKED once;
- DONE once.

## 8. Dismissal cooldown

REMINDER_DISMISSED is primarily a timing signal.

Initial rule:
- same Item is ineligible for proactive reminder for at least 24 hours after dismissal;
- effective cooldown is max(PM-08 same-item cooldown, 24h).

Do not lower semantic priority.

## 9. “Fewer like this” penalty

REMINDER_DISLIKED is stronger.

Initial rule for 30 days:

~~~text
same category attention reminder adjustment: -10
same item_type adjustment: -4
combined minimum clamp: -12
~~~

If category/type is missing:
- apply only available dimension.

This is a reminder-preference penalty, not PM-06 semantic behaviour affinity.

It should decay:
- 0–30d full;
- 31–90d half;
- >90d zero.

## 10. Notification fatigue signal

Compute a bounded user-level recent fatigue penalty from last 7 days.

Initial informative outcomes:

| Outcome | Fatigue contribution |
|---|---:|
| REMINDER_DONE | +1 |
| REMINDER_OPENED | +0.5 |
| REMINDER_SNOOZED | -0.25 |
| REMINDER_DISMISSED | -1 |
| REMINDER_DISLIKED | -2 |

Derive a smoothed receptivity value and convert only negative excess into:

~~~text
notification_fatigue_penalty: 0 .. -10
~~~

Do not reward attention score above its prior value from good engagement in v1.
The feedback loop should first reduce annoyance.

## 11. PM-07 integration

Extend AttentionRank with:
- reminder_preference_penalty;
- notification_fatigue_penalty.

New formula appends these negative/bounded components.

Do not mutate priority_score or PM-06 personal_rank.

## 12. PM-08 integration

Scheduler additionally excludes:
- Item inside dismissal cooldown.

It continues to obey:
- daily cap;
- min gap;
- same-item cooldown;
- quiet hours.

REMINDER_DISLIKED may cause next-ranked candidate to win.

## 13. PM-10 generic nudge feedback

Focused generic motivation shows its saved material with source actions and
keeps PM-11 reactions in the reminder's More menu:

~~~text
[⏰ Отложить] [✅ Сделано]
[👎 Меньше таких]
~~~

“Mеньше таких”:
- records REMINDER_DISLIKED with reminder_id and the focus item_id when present;
- suppresses generic nudges for a fixed period, initial 7 days, or disables only the current nudge kind.

Recommended v1:
- suppress same nudge kind for 7 days;
- do not silently turn off generic_motivation_enabled.

## 14. Metrics

The new events must support:
- sent -> opened;
- sent -> snoozed;
- sent -> done;
- sent -> dismissed;
- sent -> disliked;
- outcome by category;
- outcome by intensity;
- outcome by hook/template.

No dashboard required.

## 15. Tests

### Migration

- old Item events survive;
- item_id nullable;
- reminder_id FK;
- at least one reference constraint;
- focused generic nudge event stores its focus item_id;
- historical focusless generic nudge event remains valid with reminder_id alone.

### Actions

- Done creates DONE + REMINDER_DONE exactly once;
- Later creates snooze + REMINDER_SNOOZED;
- dismiss keeps Item ACTIVE;
- disliked keeps Item ACTIVE;
- unauthorized callback rejected;
- stale callback safe.

### Tracking accuracy

- bot-mediated open records OPENED;
- plain unobservable URL click is not falsely recorded.

### Ranking feedback

- recent dismiss extends cooldown;
- disliked category gets penalty;
- penalty decays;
- fatigue penalty bounded;
- priority_score unchanged.

### Generic nudges

- focused disliked nudge retains its item_id and reminder_id attribution;
- historical focusless disliked nudge remains valid with reminder_id alone;
- same nudge kind suppression works.

## 16. Acceptance criteria

1. Every observable reminder reaction has precise durable semantics.
2. generic nudges can be represented without fake Item ids.
3. callback retries do not duplicate outcome events.
4. dismiss changes timing, not Item lifecycle.
5. fewer-like-this reduces similar future reminders.
6. recent negative reaction creates bounded fatigue suppression.
7. semantic priority remains unchanged.
8. metrics can be derived from events/reminders.
9. quality gate passes.

## 17. Definition of Done

- Event migration;
- reminder-aware action service;
- Telegram controls;
- outcome idempotency;
- PM-07/08 suppression integration;
- tests;
- current product docs updated;
- no ML;
- repository review completed.
