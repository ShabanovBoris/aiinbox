# PM-06 — Behaviour-Aware Ranking

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisite: PM-05 Explicit Feedback merged and its event contract stable
Status: DONE

## 1. Epic

### Problem

AIInbox already has:
- deterministic semantic priority;
- manual interest_level 1..3;
- lifecycle events such as TODAY_SHOWN, DONE, SNOOZED and ARCHIVED;
- PM-05 explicit feedback such as USEFUL, NOT_INTERESTING and PRIORITY_HIGHER/LOWER.

These signals are durable but are not yet converted into a personalized behavioural preference.

### Goal

Introduce an explainable personal affinity layer derived from historical events.

The layer must:
- remain separate from priority_score;
- remain deterministic;
- work with sparse history;
- avoid ML;
- be bounded so behaviour cannot overwhelm semantic importance;
- expose component values for later PM-07 Attention Ranking.

Target concepts:

~~~text
semantic priority
+
manual interest
+
behaviour affinity
=
inputs for later attention ranking
~~~

PM-06 itself must not send proactive notifications.

## 2. Current architecture assumptions

The implementation must reuse:
- Item as canonical user-visible object;
- Event as the durable auxiliary history;
- interest_level already stored on Item;
- dynamic category strings;
- ItemType enum;
- SQLite;
- current user scoping and transactional patterns.

Do not introduce:
- a recommendation microservice;
- Redis;
- a feature store;
- a vector database;
- an ML training pipeline.

## 3. In scope

- BehaviourAffinityService or similarly small application/domain service;
- deterministic signal weights;
- category affinity;
- ItemType affinity;
- sparse-data smoothing;
- optional recency weighting;
- personal_rank calculation;
- explainable score components;
- exact unit tests;
- integration tests using real Event rows.

## 4. Out of scope

- proactive reminders;
- age/neglect ranking;
- attention intensity;
- reminder fatigue;
- contextual hooks;
- embeddings;
- model training;
- altering PriorityEngine;
- rewriting historical LLM analysis.

## 5. Canonical separation of scores

### priority_score

Existing semantic score 0..100.

Meaning:

> How important/useful is this Item according to goals, urgency, importance and model-derived factors?

PM-06 must not mutate its formula or historical values.

### interest_level

Existing explicit user input:
- 1 low;
- 2 normal;
- 3 high.

PM-06 does not fold this into behavioural affinity.
PM-07 consumes it separately.

### behaviour_affinity

New derived score:

~~~text
-1.0 .. +1.0
~~~

Meaning:

> Based on past reactions, how much does this user tend to value Items like this one?

### personal_rank

Derived convenience score:

~~~text
personal_rank = clamp(priority_score + behaviour_adjustment, 0, 100)
~~~

Maximum behaviour adjustment:

~~~text
-15 .. +15
~~~

personal_rank is derived, not canonical truth.

## 6. Signal vocabulary

Initial recommended weights:

| Event | Weight | Rationale |
|---|---:|---|
| USEFUL | +1.00 | strongest explicit positive signal |
| NOT_INTERESTING | -1.00 | strongest explicit negative signal |
| PRIORITY_HIGHER | +0.60 | explicit request to surface more strongly |
| PRIORITY_LOWER | -0.60 | explicit request to surface less strongly |
| DONE | +0.35 | weak positive completion signal |
| SNOOZED | -0.15 | weak negative/deferral signal |
| ARCHIVED | -0.10 | very weak negative; semantics are ambiguous |
| TODAY_SHOWN | 0.00 | exposure/context only |
| INTEREST_CHANGED | 0.00 | current interest_level is consumed separately |
| CATEGORY_CORRECTED | 0.00 | correction, not affinity |
| TYPE_CORRECTED | 0.00 | correction, not affinity |
| SUMMARY_REPORTED_WRONG | 0.00 | model-quality signal |

Unknown future events must be ignored safely.

Weights live in one obvious domain/config location and have exact tests.

## 7. Avoid double-counting one Item

One noisy Item must not dominate a category.

Rules:

### Explicit sentiment family

From USEFUL / NOT_INTERESTING:
- only the latest explicit sentiment for that Item contributes.

### Priority direction family

From PRIORITY_HIGHER / PRIORITY_LOWER:
- only the latest event for that Item contributes.

### DONE / ARCHIVED

At most one terminal lifecycle signal contributes.

### SNOOZED

Repeated snoozes are meaningful, but cap contribution per Item to the latest three SNOOZED events.

## 8. Recency

Initial deterministic buckets:

| Event age | Multiplier |
|---|---:|
| 0–30 days | 1.00 |
| 31–90 days | 0.75 |
| >90 days | 0.50 |

Do not discard old history completely.

## 9. Dimensions

Compute affinity independently for:
1. category;
2. item_type.

If PM-05 corrected category/type, aggregate history using the current canonical Item metadata.

## 10. Smoothing sparse data

For each dimension:

~~~text
raw = weighted_signal_sum / informative_event_count

confidence = informative_event_count / (informative_event_count + K)

smoothed_affinity = clamp(raw * confidence, -1, +1)
~~~

Initial:

~~~text
K = 8
~~~

Informative events are events with non-zero signal weight after per-Item collapsing.
The mean is over event count rather than total absolute weight so the signal's
policy weight and recency multiplier retain their magnitude even when all
signals point in the same direction. Sparse-history confidence supplies the
separate prior toward neutral affinity.

## 11. Combining dimensions

~~~text
combined_affinity =
    category_affinity * 0.70
  + type_affinity     * 0.30
~~~

Missing dimension history contributes 0.

Overall confidence is the same weighted projection of the two dimension
confidences. A missing dimension has zero affinity and zero confidence.

## 12. Behaviour adjustment

~~~text
behaviour_adjustment =
    round(clamp(combined_affinity, -1, +1) * 15)

personal_rank =
    clamp(priority_score + behaviour_adjustment, 0, 100)
~~~

priority_score remains unchanged.

PM-06 resolves half-point ties by rounding away from zero, so positive and
negative adjustments use symmetric deterministic boundaries.

## 13. Explainability object

Return a value object similar to:

~~~text
BehaviourRank:
  category_affinity
  type_affinity
  combined_affinity
  confidence
  adjustment_points
  personal_rank
  informative_event_count
~~~

Optional reason is deterministic/template-based.

No LLM call is required.

## 14. Storage policy

Do not add personal_rank or affinity columns initially.

They are derived from:
- current Item;
- Event history;
- policy constants.

For personal scale, calculate on demand.

Only add a cache later if profiling proves it necessary.

## 15. Query strategy

Avoid N+1 event queries.

When ranking several candidates:
- load candidates;
- load relevant user events in a bounded number of queries;
- aggregate clearly in Python or SQL.

## 16. Interaction with archived/done Items

DONE and ARCHIVED Items remain behaviour history.

Do not filter them out of aggregation.

## 17. Tests

### Formula

Cover:
- no history -> zero adjustment;
- one positive event -> strongly smoothed;
- many positives;
- many negatives;
- mixed signals;
- recency buckets;
- category/type combination;
- adjustment clamp;
- personal_rank clamp.

### Per-Item collapsing

Cover:
- USEFUL then NOT_INTERESTING -> latest wins;
- repeated priority-direction feedback collapses;
- SNOOZED is capped;
- terminal event counted once.

### Integration

Using SQLite:
- correct user scoping;
- another user has no influence;
- corrected category changes aggregation;
- DONE/ARCHIVED history included;
- unknown events ignored.

## 18. Acceptance criteria

1. priority_score is unchanged.
2. behaviour_affinity is deterministic and bounded.
3. sparse history stays close to neutral.
4. category and type behaviour are represented.
5. explicit PM-05 feedback is stronger than lifecycle inference.
6. one Item cannot dominate by repeated callbacks.
7. personal_rank is explainable.
8. no ML/new infrastructure.
9. full quality gate passes.

## 19. Definition of Done

- BehaviourAffinityService;
- formula/constants;
- exact tests;
- SQLite integration tests;
- no schema migration unless strictly justified;
- no PM-07+ notification work;
- relevant current docs updated only when behavior changes;
- repository review workflow completed.
