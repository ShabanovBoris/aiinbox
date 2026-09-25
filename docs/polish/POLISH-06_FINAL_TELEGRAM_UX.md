# POLISH-06 — Final Telegram UX Cleanup

Status: IN_REVIEW

## Why this corrective PR exists

The explicit real-user quality review after POLISH-05 found several problems in
ordinary Telegram use: `/inbox` silently stopped after 20 saved Items, long
lists used grids of numbered buttons, and choosing a category could stop at the
first 20 categories. A list row also did not open its Item directly. When
extraction or AI analysis failed, Items could appear as `Без названия` and the
normal failure message exposed internal codes such as `DOWNLOAD_FAILED`.

The review also found two unnecessary interaction steps: main-menu Export asked
for a format even though compact is the normal export, and Profile had no visible
action to update it.

## Decision

Saved Item collections have no presentation-level total cap. Inbox and category
selection use bounded SQL pages, and category Items use their existing stable
owner-scoped category token. A page's Item title is its full-width action and
opens the existing owner-scoped `item:view:<id>` card. Search stays bounded and
ranked by SQLite FTS5; Today keeps its small actionable selection; Weekly stays
read-only.

The analyzed `Item.title` remains canonical. If it is absent, Telegram derives
a local display title from safe persisted source metadata: a validated public
web hostname, a persisted document filename, or a source-type label. A
`SECURITY_REJECTED` URL never supplies a host label. This fallback does not
mutate an Item, make a request, or call an LLM. Source data is loaded in one
bounded batch for each rendered Item page.

FAILED cards describe the outcome in user language and keep the existing Retry
eligibility and Original/source controls. Internal error codes remain in
persistence and diagnostics, not the normal card.

Main-menu Export queues the existing durable COMPACT job and removes that
message's control after acknowledgement. Help keeps explicit Compact and Full
choices. Profile exposes a one-shot instruction prompt that creates the existing
durable `ProfileUpdateJob`; it does not run the LLM in a Telegram handler or
turn the instruction into an Item.

## Preserved behavior

- A bounded page is not a storage quota; every saved Item remains reachable.
- Item ownership continues to come from the acting Telegram user.
- Inbox order stays `created_at DESC, id DESC`; category item order stays
  priority-first.
- Search FTS ranking, Today ordering and post-send `TODAY_SHOWN` semantics stay
  unchanged.
- Digest claims, quiet hours, delivery retries and `REMINDER_SENT` semantics
  stay unchanged. Weekly Review remains read-only.
- ExportJob and ProfileUpdateJob remain the durable work boundaries.
- Canonical analysis title, export title semantics, schema and dependencies stay
  unchanged; this PR adds no migration or dependency.
- PM-14, semantic search, routing and other later roadmap work remain out of
  scope.

## Review focus

- All Inbox/category pages remain owner-scoped and each Item query is bounded.
- The page controls do not introduce a total accessible-Item ceiling or an
  unbounded `OFFSET` from callback data.
- Every visible Item row maps to the matching `item:view` callback; no numeric
  selector grid remains.
- Safe fallback titles and user-facing failure copy preserve provenance and do
  not expose internal codes.
- Retry eligibility, exposure history, durable export identity and guided
  Profile update behavior are unchanged.
