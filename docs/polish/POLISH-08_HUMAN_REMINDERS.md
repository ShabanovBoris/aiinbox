# POLISH-08 — Human-facing Reminders & Russian Object-centric UX

## Goal

Ordinary Telegram messages return the user's saved material instead of
reporting internal counters, ranking values, or database terminology. Technical
identifiers such as `Item`, `Reminder`, event types, and enum values remain
internal and unchanged.

## User-facing contract

- `/today` and the scheduled daily selection share one formatter. They show
  concrete titles and a saved next action or bounded summary when available;
  positions, priority scores, and aggregate counters stay hidden.
- `/weekly` presents up to three existing read-only recommendations. It does
  not expose flow, backlog, category, or reminder statistics, and an empty
  recommendation set does not fall back to those aggregates.
- Manual Attention and proactive/snooze messages show a concrete saved title
  and persisted summary or grounded hook without rank, score, or ordinal copy.
- Every new additional motivation reminder is tied to an existing saved item.
  The user's message uses that item's title, summary, source links, and lifecycle
  controls. If no eligible saved item exists, there is no motivation send.
- Ordinary bot labels use Russian product language. Explicitly opened
  notification status remains the diagnostic surface for operational counts.

## Preserved behavior

TodayService selection, WeeklyReview calculations, PM-07 ranking, scheduler
timing and arbitration, quiet hours, caps, minimum gaps, claim/finalization,
source provenance, and LLM-free motivation remain unchanged. Motivation stores
its focus id in the existing Reminder payload while keeping `Reminder.item_id`
null as the user-level claim key. Reminder callbacks and Event attribution
resolve that saved focus; historical motivation rows without a focus remain
valid and cannot trigger item-specific actions.

No schema migration or dependency is required.

## Verification

POLISH-08 is reviewed on its dedicated branch and PR. See the PR's exact-head
verification and external review results for the accepted revision.
