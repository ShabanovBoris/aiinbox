# PM-20 — Calendar-aware Attention

Type: Post-MVP Epic + Detailed Technical Specification  
Prerequisites: PM-07 Attention Ranking; PM-08 scheduling; PM-18 API; PM-19 Android recommended for initial calendar source  
Status: PLANNED

## 1. Problem

AIInbox can estimate Item effort and rank what deserves attention, but it does not know whether the user currently has five minutes, forty minutes or is inside a meeting.

The product can improve timing by using real availability windows without turning calendar data into another source of semantic content.

## 2. Goal

Use optional calendar busy/free windows together with:

- current Attention score;
- `estimated_action_minutes`;
- age/neglect;
- interest;
- existing PM-08 notification policy.

Example:

> 24 minutes free before the next event. A high-value Item estimated at 18 minutes has been waiting 31 days.

## 3. Privacy-first initial integration

PM-20 v1 should **not require server-side access to calendar event titles, descriptions, attendees or locations**.

Recommended first integration after PM-19:

~~~text
Android Calendar Provider
→ local permission
→ busy interval projection only
→ POST /v1/calendar/busy
→ AIInbox availability snapshot
~~~

This avoids Google OAuth/token storage on the server while still using real calendar availability.

A future provider may implement Google/CalDAV directly behind the same availability boundary.

## 4. Core principle

AIInbox needs:

~~~text
when am I busy/free?
~~~

not:

~~~text
what is the meeting about?
who attends?
what is the description?
~~~

Do not upload those fields in v1.

## 5. In scope

- optional calendar-attention setting;
- server model for bounded busy-window snapshots;
- Android busy interval sync;
- overlap normalization/merge;
- current free-window calculation;
- stale snapshot handling;
- derived calendar-fit signal for Attention candidates;
- PM-08 discretionary-notification busy suppression;
- tests for timezone/DST/boundaries;
- no-calendar graceful fallback.

## 6. Out of scope

- creating/updating calendar events;
- reading event title/description/attendees/location;
- rescheduling meetings;
- automatic task time-blocking;
- replacing PM-07 ranking;
- using calendar content in LLM prompts;
- requiring Android/calendar to use AIInbox;
- full server-side Google OAuth in v1.

## 7. Provider-neutral domain boundary

Define a small availability projection independent of Android/Google:

~~~text
AvailabilitySnapshot
- user_id
- source
- range_start
- range_end
- synced_at
- busy intervals
~~~

Business logic consumes busy intervals only.

## 8. Storage model

For personal scale, store normalized busy intervals in SQLite.

Possible schema:

~~~text
calendar_snapshots
- id
- user_id
- source
- range_start
- range_end
- synced_at
- created_at

calendar_busy_intervals
- snapshot_id
- starts_at
- ends_at
~~~

Or a bounded JSON interval list if query/update semantics remain simpler.

Do not store event text.

## 9. Snapshot replacement

Android uploads a complete busy snapshot for a bounded future range, e.g. the next 7–14 days.

Server validates then atomically replaces the previous snapshot for that source/user.

Do not incrementally accumulate duplicate calendar events forever.

## 10. API endpoint

Add authenticated PM-18 endpoint conceptually:

~~~text
PUT /v1/calendar/busy
~~~

Request contains:

- source identifier;
- range start/end;
- generated/synced timestamp;
- list of UTC or offset-aware busy intervals.

No title/description fields are accepted.

## 11. Android permission

Calendar access is opt-in.

The client must explain that it uploads only busy time ranges, not calendar text.

Denying permission leaves all current AIInbox behavior unchanged.

## 12. Interval validation

Reject/normalize:

- start >= end;
- non-timezone-aware timestamps at API boundary;
- intervals outside the declared snapshot range;
- pathological future range;
- excessive interval count.

Apply hard limits.

## 13. Merge overlaps

Before persistence or evaluation, merge overlapping/touching busy intervals deterministically.

Example:

~~~text
10:00–10:30
10:20–11:00
→ 10:00–11:00
~~~

This avoids double counting and simplifies free-window logic.

## 14. Time basis

Store normalized timestamps in UTC.

Presentation/calendar-day interpretation uses `User.timezone` where required.

Tests must cover DST transitions.

## 15. Snapshot freshness

Stale calendar data can be worse than no calendar data.

Add an exact bounded freshness rule, e.g. ignore snapshot if:

~~~text
now - synced_at > 6 hours
~~~

Choose/document the actual v1 value and test exact boundaries.

If stale:

~~~text
calendar context unavailable
→ existing PM-07/08 behavior
~~~

Do not assume the old schedule remains true.

## 16. Current busy state

At time `now`:

~~~text
if now is inside a busy interval:
    busy = true
else:
    busy = false
~~~

Use half-open intervals:

~~~text
[start, end)
~~~

At exactly event end, the user is no longer busy.

## 17. Current free window

If not busy, compute minutes until the next busy interval within the fresh snapshot.

If there is no upcoming busy interval in a practical lookahead, clamp the effective window to a documented maximum rather than treating it as infinite.

Example v1:

~~~text
MAX_FREE_WINDOW_MINUTES = 240
~~~

Exact value should be code-level/tested.

## 18. Fit buffer

A task estimated at 20 minutes should not be recommended into an exactly 20-minute gap with no transition time.

Use a small deterministic buffer, e.g.:

~~~text
required_window = estimated_action_minutes + 5 minutes
~~~

Exact buffer is configurable/code-level and tested.

## 19. Unknown estimates

If `estimated_action_minutes` is NULL:

- do not claim the Item fits a short calendar window;
- keep its base Attention score unchanged;
- it may still rank normally when calendar fit is not required.

Do not invent a duration.

## 20. Calendar fit signal

Extend `AttentionRank` with a derived explainable component such as:

~~~text
calendar_fit_adjustment
~~~

It remains non-canonical and is not persisted in Item.

Initial behavior should be bounded and modest; calendar fit helps choose among already relevant Items rather than overriding semantic priority.

Example policy:

- Item estimate + buffer fits current free window → small positive boost;
- does not fit → no boost or small scheduler exclusion for the current slot;
- unknown duration → 0.

Do not use a huge bonus that makes low-value Items dominate purely because they are short.

## 21. Ranking vs scheduling

Keep two concerns distinct:

### Manual `/attention` / API Attention
Calendar-fit may affect ordering/explanation when fresh context exists.

### Unsolicited PM-08 scheduling
Calendar context may additionally block discretionary interruption while the user is busy.

Do not hide this distinction inside one opaque score.

## 22. Busy suppression

When a fresh calendar snapshot says the user is currently busy, suppress discretionary Attention-family sends:

~~~text
PROACTIVE_ATTENTION
MOTIVATION_NUDGE
~~~

Do not automatically suppress:

- explicit user commands;
- lifecycle actions;
- source delivery;
- snooze resurfacing unless a later policy explicitly changes it.

Daily digest may keep its configured schedule in v1; changing digest semantics is out of scope.

## 23. Why motivation is suppressed

`MOTIVATION_NUDGE` is also an unsolicited discretionary interruption. It should not bypass a known current meeting merely because it has no Item duration.

## 24. PM-08 precedence

Calendar context is an additional gate, not a replacement.

All existing PM-08 rules still apply:

- attention enabled;
- intensity;
- daily cap;
- quiet hours;
- minimum gap;
- reminder fatigue;
- generic cap;
- item cooldown;
- durable claims.

Calendar availability can further reduce sends, never increase past those limits.

## 25. Final prepare revalidation

Calendar busy/free state must be re-evaluated in the final serialized preparation path immediately before a proactive/motivation send, using current time and the latest fresh snapshot.

Do not rely solely on ranking done several minutes earlier.

## 26. No catch-up

If a reminder was suppressed because the user was busy:

- do not queue a backlog of missed Attention messages;
- next worker cycle recomputes current policy/candidates normally.

## 27. Calendar explanation

Manual Attention UI may show a factual reason, e.g.:

~~~text
Помещается в текущее окно ~24 мин
~~~

Only if:

- snapshot is fresh;
- free window is actually computed;
- Item has a stored estimate;
- estimate + buffer fits.

Do not state meeting/event details.

## 28. No calendar semantic influence

Calendar data does not change:

- `priority_score`;
- `personal_rank`;
- category/type;
- interest level;
- goal fit.

It is current context only.

## 29. No LLM

PM-20 calendar logic is deterministic.

Do not send busy intervals to LLM to ask whether the user is free.

## 30. Settings

Add minimal user-facing controls, conceptually:

~~~json
{
  "calendar_attention_enabled": false
}
~~~

Default for existing and new users should be **false** until a calendar source is explicitly connected/synced.

Do not expose dozens of fit coefficients.

## 31. Disconnect

User must be able to disable calendar-aware Attention.

On disable/disconnect:

- ignore/delete calendar snapshots according to documented privacy policy;
- immediately fall back to existing ranking/scheduling;
- keep core AIInbox usable.

## 32. Retention

Busy snapshots are short-lived operational context.

Keep only the current bounded future snapshot and a minimal sync timestamp; do not build years of calendar history.

Old snapshots should be deleted/replaced.

## 33. Export

PM-15 export should not include transient busy calendar intervals by default.

If included later, it must be explicitly documented as optional context data.

## 34. Logs

Log only:

- source;
- user id;
- interval count;
- range;
- freshness/validation failures.

Never log calendar event text because PM-20 v1 never receives it.

## 35. Tests — interval normalization

Cover:

- overlap merge;
- adjacent intervals;
- invalid start/end;
- range clipping/rejection;
- excessive interval count;
- user isolation.

## 36. Tests — boundaries

Cover:

- exactly at busy start → busy;
- exactly at busy end → free;
- next busy interval calculation;
- no next interval clamp;
- exact freshness boundary;
- stale snapshot ignored.

## 37. Tests — DST/timezone

Use Europe/Helsinki around DST transitions.

Busy interval UTC truth must remain correct while user-local presentation changes appropriately.

## 38. Tests — fit

Cover:

- 18-minute Item in 24-minute window with buffer → fit;
- Item exactly beyond available window → no fit;
- unknown estimate → no fit claim;
- very long free gap clamps to max window;
- low-priority short Item does not receive an unbounded boost.

## 39. Tests — scheduling

Fresh current busy state:

- blocks PROACTIVE_ATTENTION;
- blocks MOTIVATION_NUDGE;
- does not mutate budgets as if a notification were sent;
- no catch-up row created.

At event end/new free window, later cycles can send normally subject to PM-08.

## 40. Tests — final prepare race

Scenario:

~~~text
ranking says free
→ new snapshot arrives marking current time busy
→ final prepare
~~~

Expected:

~~~text
no send
~~~

## 41. Tests — disabled/no calendar

With calendar setting disabled, missing, stale or revoked:

current PM-07/08 behavior must be regression-identical.

## 42. Android sync tests

Cover:

- permission denied;
- local calendar read produces busy intervals only;
- token/auth errors;
- offline retry;
- complete snapshot replacement;
- no title/description serialized in request.

## 43. Future server-side providers

A later Google/CalDAV integration may implement the same availability snapshot boundary.

If server OAuth is introduced later, it must separately specify:

- read-only scope;
- encrypted token storage;
- revocation;
- refresh token handling;
- secret backup/export policy.

Do not pull that complexity into PM-20 v1.

## 44. Documentation

Update:

- PRODUCT_SPEC;
- Android permission/privacy text;
- API calendar endpoint;
- Attention explanation semantics;
- RUNBOOK snapshot/freshness diagnostics;
- settings docs.

## 45. Acceptance criteria

1. Calendar-aware Attention is optional.
2. Core AIInbox works with no calendar.
3. Initial calendar source uploads busy intervals only.
4. No titles/descriptions/attendees/locations are stored.
5. Snapshot range/count is bounded.
6. Overlaps are normalized.
7. UTC storage and user timezone semantics are correct.
8. Stale snapshots are ignored.
9. Current free window is deterministic.
10. Estimated action duration is never invented.
11. Calendar fit is bounded/explainable and non-canonical.
12. Priority/personal rank remain unchanged.
13. Fresh busy state suppresses discretionary proactive + motivation sends.
14. Existing PM-08 caps/gaps/quiet hours remain authoritative.
15. Final prepare revalidates current calendar state.
16. Busy suppression creates no catch-up queue.
17. Disabling calendar restores current behavior immediately.
18. Calendar history is not retained indefinitely.
19. No LLM is involved.
20. Full quality gate passes.

## 46. Definition of Done

~~~text
Android calendar (local)
      ↓ permission
busy intervals only
      ↓
/v1/calendar/busy
      ↓
fresh normalized snapshot
      ↓
AvailabilityService
      ↓
current busy/free window
      ↓
┌────────────────────┬────────────────────┐
│ manual Attention   │ PM-08 scheduler    │
│ bounded fit signal │ busy send gate     │
└────────────────────┴────────────────────┘
      ↓
existing Attention semantics remain usable without calendar
~~~

PM-20 is complete when AIInbox can use real availability to improve timing and task fit while storing the minimum calendar information necessary and remaining fully functional when calendar context is absent.