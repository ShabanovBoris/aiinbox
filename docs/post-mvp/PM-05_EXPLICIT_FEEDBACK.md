# PM-05 — Explicit Feedback

Type: Post-MVP Epic + Detailed Technical Specification
Status: DONE
Prerequisite: PM-01 recommended; existing durable Event log required

Current implementation: Telegram feedback controls and transactional canonical
corrections are part of the current main baseline. PM-05 records feedback and
canonical corrections; ranking, retraining, and automatic re-analysis are not
part of this phase.

## 1. Epic

### Problem

Current lifecycle actions give implicit feedback:

- Done;
- Snooze;
- Archive;
- Today shown.

But they do not answer:

- “Was this useful?”
- “You categorized this incorrectly.”
- “This should be higher/lower.”
- “The summary is wrong.”

Without explicit correction signals, future personalization can confuse disinterest with lack of time and cannot distinguish model mistakes from user behavior.

### Goal

Add a minimal explicit feedback surface that creates clean, durable signals for future PM-06 behaviour-aware ranking and quality analysis.

Do **not** add ML in this phase.

## 2. Feedback types

Initial set:

~~~text
USEFUL
NOT_INTERESTING

CATEGORY_CORRECTED
TYPE_CORRECTED

PRIORITY_HIGHER
PRIORITY_LOWER

SUMMARY_REPORTED_WRONG
~~~

The exact enum/storage representation may remain string-based to match the existing Event model.

## 3. Product distinction

### Feedback that changes canonical Item data

Examples:

- category correction;
- item type correction.

These should update Item metadata and write a durable event in the same transaction.

### Feedback that is only a signal

Examples:

- Useful;
- Not interesting;
- Priority higher/lower;
- Summary wrong.

These do not automatically rewrite every analysis field.

This distinction prevents feedback from becoming an uncontrolled second analysis engine.

## 4. In scope

- Telegram feedback controls;
- durable events;
- category correction;
- item type correction;
- priority-direction feedback;
- summary quality report;
- idempotent/concurrency-safe service operations;
- tests;
- compact user feedback confirmation.

## 5. Out of scope

- automatic retraining;
- ML ranking;
- automatic model fine-tuning;
- automatic full re-analysis after every feedback event;
- arbitrary free-form editing UI;
- changing priority formula in PM-05;
- category taxonomy redesign;
- moderation/reporting system.

## 6. Telegram UX

Do not place seven buttons on every message.

Use a compact primary row:

~~~text
[👍 Полезно] [👎 Не моё] [⚙ Исправить]
~~~

`Исправить` opens a secondary menu:

~~~text
[Категория]
[Тип]
[Приоритет выше]
[Приоритет ниже]
[Summary неверный]
[Назад]
~~~

Keep PM-01 interest buttons separate conceptually:

~~~text
Интерес: [1] [2] [3]
~~~

“High interest” is not the same as “Useful”.

The controls are shown only for READY Items, including READY/PARTIAL results.
The category picker is bounded to the 20 most frequently used existing user
categories. Category names travel through short opaque callback tokens rather
than raw callback data.

## 7. Useful / Not interesting

### USEFUL

Meaning:

> This Item/result is valuable to me.

### NOT_INTERESTING

Meaning:

> This is not something I want the system to prioritize/resurface much.

It does not necessarily archive the Item automatically.

The user may still keep it as reference.

Future PM-06/07 can use this signal.

## 8. Category correction

### UX

Show existing category and a minimal correction path.

Options:

1. choose from existing categories; or
2. send a short replacement category.

Prefer existing categories to avoid taxonomy fragmentation.

### Service

Conceptually:

~~~text
correct_item_category(user_id, item_id, new_category)
correct_item_category_by_token(user_id, item_id, category_token)
~~~

Requirements:

- trim/validate length;
- user-scoped;
- Item update + event in same transaction;
- resolve bounded category tokens and consume stale/unavailable callbacks in that same transaction;
- no LLM required;
- search index updated if category participates in searchable/display data.

Event payload:

~~~json
{
  "from": "Programming",
  "to": "AI"
}
~~~

## 9. Type correction

Allowed values remain:

- ACTION;
- LEARN;
- READ;
- WATCH;
- IDEA;
- REFERENCE;
- SOMEDAY.

Service updates canonical `item_type` + event atomically.

Payload:

~~~json
{
  "from": "REFERENCE",
  "to": "LEARN"
}
~~~

Changing type may change /today eligibility immediately.

Do not automatically recalculate semantic priority unless separately required by current deterministic logic.

## 10. Priority higher/lower

These are directional feedback signals.

They do not directly overwrite `priority_score` in PM-05.

Events:

~~~text
PRIORITY_HIGHER
PRIORITY_LOWER
~~~

Future PM-06 can aggregate them.

Optional payload:

~~~json
{
  "priority_score_at_feedback": 72
}
~~~

This preserves context.

## 11. Summary wrong

Event:

~~~text
SUMMARY_REPORTED_WRONG
~~~

This phase only records the signal and confirms it.

Do not automatically regenerate summary unless explicitly designed in a later issue.

Rationale: regeneration may repeat the same mistake and adds expensive behavior to a simple feedback action.

## 12. Event idempotency

Telegram callbacks can repeat.

For toggle-like one-off feedback:

- Event has a nullable 160-character idempotency_key and a unique
  (user_id, idempotency_key) database index;
- Telegram stores telegram-callback:<CallbackQuery.id> as the key;
- repeated delivery of that callback is a no-op, while a later user click has a
  new key and remains a distinct Event.

For feedback that may be intentionally repeated across time, preserve legitimate later events.

Do not globally deduplicate “USEFUL forever” if future semantics need time-series feedback.

Canonical corrections serialize writers with SQLite BEGIN IMMEDIATE and store
a user-scoped row in feedback_callback_receipts for each accepted callback,
including a no-op selection. Recognized callbacks whose owned Item or category
target is stale are also consumed without an Event. A real change commits the
Item update, receipt, and correction Event together; a no-op or stale action
commits only the receipt. This keeps a late retry from becoming a later
mutation without adding a fake Event. READY/category-token resolution and the
receipt decision happen within this same serialized transaction; handlers do
not split applicability checks from callback consumption.

## 13. Event payload contract

Keep payload small and version-tolerant.

Do not store full Item content in event payload.

Useful fields:

- old/new value;
- source surface;
- score at feedback time;
- optional reminder id in future phases.

## 14. Service boundary

Create explicit application functions rather than embedding SQL in callback handlers.

Potential operations:

~~~text
record_item_feedback(...)
correct_item_category(...)
correct_item_type(...)
~~~

Do not introduce event sourcing.

Item remains canonical state; Event remains auxiliary durable history.

## 15. Search/index consistency

If category/type corrections affect displayed/retrieved data:

- update FTS/index projection if needed;
- commit correction and index update consistently with current application-controlled FTS policy.

## 16. Interaction with PM-01

`interest_level` answers:

> How much do I personally want to return to this?

`USEFUL` answers:

> Was this Item/result useful?

`NOT_INTERESTING` answers:

> Do not spend much future attention on this.

They may correlate, but must remain independent.

Do not implicitly set:

~~~text
NOT_INTERESTING → interest_level = 1
~~~

unless a later explicit product decision chooses that behavior.

## 17. Interaction with lifecycle actions

Do not conflate:

~~~text
Archive == Not interesting
Snooze == Not useful
Done == Useful
~~~

They mean different things.

Examples:

- useful article may be archived after reading;
- important task may be snoozed due to lack of time;
- completed task may have been unpleasant but necessary.

This distinction is essential for PM-06.

## 18. Analytics readiness

PM-05 should make it possible to query:

- feedback count by category/type;
- ratio USEFUL / NOT_INTERESTING;
- manual correction frequency;
- categories frequently corrected into another category;
- priority higher/lower signals.

No dashboard is required yet.

## 19. Telegram state flow

Correction menus should not leave the user stuck.

Requirements:

- Back/Cancel;
- malformed/stale callback handled;
- deleted/missing Item handled;
- unauthorized user cannot modify;
- after correction show canonical persisted value;
- Back from the correction menu restores the current source-aware Item keyboard;
- category/type menu navigation creates no Event.

If free-text category correction requires conversational state, keep state minimal and bounded. Do not add a large workflow framework.

## 20. Tests

### Useful / not interesting

- event persisted;
- correct user/item;
- duplicate callback harmless;
- lifecycle state unchanged.

### Category correction

- atomic Item + event;
- old/new payload correct;
- other user's Item denied;
- invalid/empty category rejected;
- search/index consistency.

### Type correction

- allowed types only;
- /today eligibility reflects new type;
- event payload correct.

### Priority feedback

- events store current priority context;
- no mutation of `priority_score`.

### Summary wrong

- event recorded;
- no automatic LLM call.

### Telegram

- compact primary buttons;
- correction submenu;
- cancel/back;
- stale item;
- malformed callback;
- unauthorized access.

## 21. Migration

Migrations add nullable Event.idempotency_key with a unique
(user_id, idempotency_key) index, plus feedback_callback_receipts with a unique
(user_id, idempotency_key) constraint. Existing Events remain intact with a
NULL key; SQLite permits multiple NULL keys, and the same non-NULL key may be
used by different users. The receipt table stores transport outcomes that do
not correspond to semantic Event rows.

Do not create a new feedback event table merely for these event types;
feedback_callback_receipts stores transport identities only, not feedback data.

## 22. Acceptance criteria

1. User can mark an Item Useful or Not interesting.
2. User can correct category.
3. User can correct item type.
4. User can signal higher/lower priority.
5. User can report bad summary.
6. Canonical corrections and their events are atomic.
7. Directional feedback does not mutate semantic priority in PM-05.
8. Lifecycle events retain their original meaning.
9. No ML/retraining is introduced.
10. Full quality gate passes.

## 23. Data contract for PM-06

At the end of PM-05, PM-06 must have reliable access to:

~~~text
CREATED
TODAY_SHOWN
DONE
SNOOZED
ARCHIVED
RETRIED
INTEREST_CHANGED
USEFUL
NOT_INTERESTING
CATEGORY_CORRECTED
TYPE_CORRECTED
PRIORITY_HIGHER
PRIORITY_LOWER
SUMMARY_REPORTED_WRONG
~~~

PM-05 succeeds partly by making this feedback vocabulary clean enough that behaviour ranking does not have to guess what user actions meant.

## 24. Definition of Done

- feedback UI;
- correction UI;
- application service boundaries;
- durable events;
- canonical corrections transactional;
- tests;
- BOT_USAGE updated;
- no ML or ranking scope creep;
- repository review workflow completed.
