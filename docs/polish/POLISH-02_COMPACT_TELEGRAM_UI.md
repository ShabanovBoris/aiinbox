# POLISH-02 — Compact Telegram Item UI

Type: Stabilization / Telegram UX specification  
Depends on: POLISH-01  
Blocks: POLISH-03/04 presentation integration  
Status: PLANNED

## 1. Problem

The current READY Item card exposes too much internal state at once:

- Category;
- ItemType;
- priority score;
- interest 1–3;
- full summary;
- next action;
- priority reason;
- many rows of inline buttons.

The main action surface currently includes interest 1/2/3, Useful, Not mine, Fix, Done, Later, Archive, video-send buttons and one or more generic Open links.

The result is visually heavy and makes the user manage AIInbox internals instead of returning to content.

## 2. Goal

Default READY card should expose only:

- saved confirmation;
- strong content-specific title;
- short outcome-first summary;
- the most useful source action(s);
- one compact `••• Ещё` entry point for secondary controls.

Everything else remains available but moves behind detail/action submenus.

## 3. Default READY card

Target shape:

~~~text
✓ Сохранено

🎯 <title>

<compact outcome-first summary>
~~~

No default lines for:

- category;
- ItemType;
- priority;
- attention score;
- interest;
- next action;
- priority reason.

Analysis completeness warning remains visible only when it materially changes trust in the summary, e.g. caption-only, transcript-only, visual-only or partial analysis.

## 4. Primary keyboard principle

The first keyboard should answer:

> “How do I get back to the content?”

not:

> “How do I administer this Item?”

Primary rows may contain:

- `📩 Прислать YouTube`;
- `📩 Прислать Reel`;
- clear source buttons such as `↗ YouTube`, `↗ GitHub`, `↗ Статья — host`;
- `↩️ Оригинал` once POLISH-03 provides it;
- `••• Ещё`.

Exact source/original behavior belongs to POLISH-03; this PR prepares the compact UI seam.

## 5. Secondary menu

`••• Ещё` opens a submenu containing secondary actions:

- Done;
- Later;
- Archive;
- Interest;
- feedback/correction;
- Details.

Do not show all of them at top level.

## 6. Details submenu

`ℹ️ Детали` exposes system metadata on demand:

- Category;
- Type;
- Priority;
- Interest;
- optional Attention score when the calling surface has one;
- analysis completeness;
- optional next action / priority reason only if still useful after POLISH-01.

This information should never be permanently visible on every saved card.

## 7. Interest control

Replace the always-visible `1 | 2 | 3` row with a submenu.

Example:

~~~text
⭐ Интерес
→ 1 Низкий
→ 2 Обычный ✓
→ 3 Высокий
~~~

Existing event/idempotency semantics remain unchanged.

## 8. Feedback controls

Move:

- Useful;
- Not interesting;
- Fix;
- priority higher/lower;
- summary wrong;
- category correction;
- type correction

under one secondary `🛠 Исправить / Обратная связь` branch.

Do not delete PM-05 feedback signals.

## 9. Lifecycle controls

Done/Later/Archive remain available in `••• Ещё`.

Do not change canonical lifecycle semantics, Events, CAS/idempotency or snooze choices.

## 10. Source buttons

Do not use labels such as:

- `Открыть`;
- `Открыть 1`;
- `Открыть 2`.

Every direct URL button must describe destination.

Examples:

- `↗ YouTube`;
- `↗ Instagram Reel`;
- `↗ GitHub`;
- `↗ Статья — medium.com`;
- `↗ Сайт — example.com`.

For multiple same-type sources use bounded disambiguation such as `YouTube 1`, `YouTube 2` only when no better persisted source/title label exists.

## 11. Host labeling

Add a safe presentation helper that parses persisted HTTP(S) URL and produces a bounded host/source label.

Do not fetch the URL.

Do not display arbitrary URL schemes.

## 12. Video resend priority

For YouTube/Instagram sources, resend is a high-value action and remains top-level.

If both resend and direct-open exist, keep both only while the primary keyboard remains compact.

Preferred ordering:

~~~text
📩 Прислать Reel
↗ Instagram
••• Ещё
~~~

## 13. Composite Items

Do not create an unbounded row list for a multi-source Item.

Bound primary source actions.

If source actions exceed a small explicit limit, collapse them into `🔗 Источники`.

The chosen limit must be code-level and tested.

## 14. Menu callbacks

Use compact callback namespaces consistent with current limits, for example:

~~~text
item:more:<item_id>
item:details:<item_id>
item:sources:<item_id>
item:interest_menu:<item_id>
item:back:<item_id>
~~~

Reuse current owner checks and reload canonical state for each callback.

## 15. No new canonical state

Menu location is presentation state.

Do not persist “currently open submenu” in SQLite.

## 16. Stale callback behavior

If Item state changed between render and tap:

- reload canonical state;
- render the valid current submenu or concise unavailable state;
- never apply action based only on stale callback markup.

## 17. Formatting split

Refactor the presentation layer toward separate projections, e.g.:

- `format_ready_item_compact()`;
- `format_item_details()`.

Avoid one formatter accumulating every future field.

## 18. Attention cards

Manual `/attention` cards should also become compact.

Do not expose by default all of:

~~~text
Внимание: 83/100
Приоритет: 80/100
Интерес: 2/3
Возраст: 31 дн.
Почему сейчас: ...
~~~

Recommended default:

~~~text
<title>
<one short content/revisit reason>
~~~

Ranking diagnostics move to Details.

POLISH-04 owns final hook/revisit copy.

## 19. Failure/partial cards

Retry must remain easy to discover.

For FAILED or retryable PARTIAL Items, `🔁 Retry` may remain top-level because it is the primary recovery action.

## 20. Telegram bounds

Normal READY output should remain comfortably below 4096 chars.

Do not rely on `_fit_message` to routinely truncate core content or actions.

## 21. Readability

- use consistent Russian labels;
- avoid unnecessary English lifecycle terms where a clear Russian label exists;
- emoji aid scanning but must not be the only state signal;
- buttons should make sense without reading implementation metadata.

## 22. Tests

Cover:

- READY default card excludes category/type/priority/interest/next_action/reason;
- analysis completeness warning remains when relevant;
- top-level keyboard no longer contains interest 1/2/3;
- top-level keyboard no longer contains feedback row;
- `••• Ещё` exists;
- submenu exposes lifecycle/interest/feedback/details;
- direct URL labels describe destination;
- multiple sources collapse predictably;
- Retry remains visible for failed/retryable Items;
- callbacks are owner-scoped and stale-safe;
- existing lifecycle and feedback Events remain unchanged.

## 23. Acceptance criteria

1. Default READY card is materially shorter.
2. Next action and priority reason are absent from default presentation.
3. Category/type/priority/interest are available only on demand.
4. Interest 1–3 is no longer a permanent row.
5. Feedback controls are behind secondary navigation.
6. Done/Later/Archive remain reachable.
7. Video resend remains easy to reach.
8. Generic `Открыть 1/2` labels are gone.
9. Multi-source Items do not create an unbounded keyboard.
10. No lifecycle/feedback semantics regress.
11. Full pytest/ruff gate passes.

## 24. Definition of Done

POLISH-02 is complete when the default Item card shows the content, not AIInbox's internal scoring model, while every existing corrective/lifecycle action remains reachable within one additional submenu step.
