# PM-01 — User Interest Level 1–3

Type: Post-MVP Epic + Detailed Technical Specification  
Status: DONE
Prerequisite: stabilized MVP / PM-00

## 1. Epic

### Problem

Today AIInbox estimates semantic importance and personal fit using the model, but it has no direct lightweight signal for:

> “How interested am I in this particular thing?”

The user often knows this immediately at capture time, while urgency/importance may be different.

### Goal

Every Item has a user-controlled interest level:

- 1 — low interest / “store it”;
- 2 — normal interest;
- 3 — high interest / “I really want to return to this”.

Default is always 2. Capture must remain zero-friction: if the user does nothing, Item stays at 2.

### Product principle

`interest_level` is **not** `priority_score`.

Example:

~~~text
Interesting movie:
interest_level = 3
priority_score = 35

Important architecture article:
interest_level = 2
priority_score = 91
~~~

Both are valid.

## 2. In scope

- database field and constraint;
- migration;
- default value 2 for existing and new Items;
- Telegram inline interest control;
- update service;
- durable feedback event;
- formatting in relevant Item/result views;
- test coverage;
- documentation update.

## 3. Out of scope

- behaviour-aware ranking;
- changing semantic priority formula;
- ML;
- re-running LLM analysis after interest change;
- more than three interest levels;
- per-category interest settings.

## 4. User stories

### US-01 Default

As a user, when I send something and do nothing else, the Item is stored with interest level 2.

### US-02 High interest

As a user, I can tap 3 on a freshly analyzed Item to indicate strong interest.

### US-03 Low interest

As a user, I can tap 1 without archiving the Item.

### US-04 Change later

As a user, I can change the level again later; the latest value becomes canonical.

### US-05 Explainability

As a future ranking component, the system can distinguish explicit manual interest from model-derived `interest_fit`.

## 5. Data model

Add to `items`:

~~~text
interest_level INTEGER NOT NULL DEFAULT 2
~~~

Database constraint:

~~~text
CHECK interest_level BETWEEN 1 AND 3
~~~

Requirements:

- existing rows migrate to 2;
- ORM default = 2;
- server/database default = 2 where practical;
- invalid values are rejected at application validation and DB boundary.

Do not rename or repurpose current `interest_fit`.

## 6. Events

Add event:

~~~text
INTEREST_CHANGED
~~~

Recommended payload:

~~~json
{
  "from": 2,
  "to": 3,
  "source": "telegram"
}
~~~

Rules:

- write event only when canonical value actually changes;
- duplicate callback that requests current value is a no-op;
- event and Item update are in one transaction.

## 7. Service boundary

Add an application-level operation, conceptually:

~~~text
set_item_interest(user_id, item_id, level)
~~~

Requirements:

- user-scoped;
- validates 1..3;
- updates via a conditional transaction;
- returns canonical persisted Item;
- Telegram handler contains no persistence business logic.

Do not add a generic “ItemPreferenceFramework”.

## 8. Telegram UX

After successful analysis, include a compact control:

~~~text
Интерес: ②

[1] [2 ✓] [3]
~~~

The common path requires zero interaction.

Callback identity should include:

- Item id;
- requested level.

Example shape:

~~~text
item:interest:<item_id>:<level>
~~~

Exact callback format may follow existing keyboard conventions.

### Rendering

When level changes, update only the relevant message/keyboard if feasible.

Do not resend a full LLM summary.

### Other views

At minimum, expose the level where it is useful:

- detailed Item result;
- optionally /today formatting if visual noise remains low.

Do not clutter compact lists if the signal adds no decision value.

## 9. Interaction with current priority

PM-01 must not modify `PriorityEngine`.

No:

~~~text
priority_score += interest_level
~~~

in this phase.

Future PM-07 Attention Ranking will consume `interest_level` separately.

## 10. Migration requirements

Migration must verify:

- fresh DB → field exists;
- upgrade existing DB → all previous Items = 2;
- constraint rejects 0 and 4;
- existing Item data remains intact.

## 11. Concurrency and idempotency

Two rapid callbacks may race.

Canonical behavior:

- last successful committed change wins;
- no duplicate event for no-op;
- no corrupt state;
- callback response reflects persisted value.

No distributed lock is needed.

## 12. Tests

### Unit / service

- valid levels 1/2/3;
- invalid 0/4 rejected;
- default = 2;
- changing 2→3 writes exactly one event;
- repeated 3→3 writes no additional event;
- changing 3→1 persists correct payload;
- cannot mutate another user's Item.

### Migration

- upgrade existing Item to default 2;
- DB check constraint.

### Telegram

- result keyboard defaults to 2;
- tapping 3 updates keyboard/state;
- malformed callback does not mutate data;
- unauthorized user cannot mutate.

### Regression

Full existing suite remains green.

## 13. Acceptance criteria

PM-01 is accepted when:

1. Every Item always has 1, 2 or 3.
2. Existing Items become 2 after migration.
3. New Items default to 2 without user action.
4. User can change level via Telegram.
5. Change is durable across restart.
6. INTEREST_CHANGED event is transactional and non-duplicating.
7. `priority_score` remains unchanged by this feature.
8. Full quality gate passes.

## 14. Definition of Done

- migration committed;
- service implemented;
- Telegram control implemented;
- tests added;
- README/BOT_USAGE updated if user-visible behavior changes;
- no unrelated ranking feature added;
- implementation state/review documentation updated according to repository protocol.
