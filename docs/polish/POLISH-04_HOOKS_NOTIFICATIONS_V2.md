# POLISH-04 — Hooks & Notifications v2

Type: Stabilization / Engagement quality specification  
Depends on: POLISH-01, POLISH-03  
Status: IN_REVIEW

## 1. Problem

Current reminder infrastructure is technically strong but copy quality is weak.

Contextual hook wrappers such as:

- “Одна сильная мысль внутри:”;
- “Причина вернуться к этому:”;
- “Ты давно это сохранил…”

are safe but generic.

Generic PM-10 motivation templates are factual but often read like backlog telemetry rather than a reason to act.

The product needs fewer, stronger interruptions that expose a concrete tension, result or useful contradiction from the saved content.

## 2. Goal

Make every Item-specific notification answer:

> “Why would I want to open this right now?”

without inventing facts, exaggerating beyond evidence, or exposing ranking internals.

Desired voice:

- concrete;
- short;
- slightly provocative when source evidence supports it;
- specific to the material;
- avoids corporate/task-manager tone;
- no fake urgency;
- no invented statistics;
- no guilt/shaming.

## 3. Grounding remains mandatory

Keep all PM-09 evidence guarantees:

- allowed original Content kinds only;
- exact `source_content_id` allowlist;
- exact contiguous evidence excerpt;
- inherited source identity;
- no external facts;
- no web/refetch;
- no hook-on-hook grounding.

Quality improvements must not weaken validation.

## 4. Hook quality contract

A valid hook should normally expose at least one of:

- surprising result;
- before/after contrast;
- counterintuitive claim;
- concrete consequence;
- question whose answer is actually present in supplied evidence;
- practical challenge directly supported by source evidence.

Bad:

> Причина вернуться к этому: материал про AI-инструменты.

Good when supported:

> Полгода назад автор советовал подключать skills. Теперь предлагает удалить их почти все — и объясняет, почему агент от этого работает лучше.

The provocative element must come from the content, not from invented clickbait.

## 5. Hook type semantics

Keep current enum but clarify generation rules.

### SURPRISING_FACT

A concrete supported fact/claim that is genuinely non-obvious in the supplied source.

### PRACTICAL_VALUE

A specific technique/result the user can get from reopening the Item.

### QUESTION

A concise curiosity gap whose answer is represented in the supplied evidence/context. Never generic “Хочешь узнать больше?”.

### CHALLENGE

A source-grounded invitation to test/reconsider something. No guilt or pressure.

### CONTRAST

A meaningful before/after, expected/actual, or common-practice/author-conclusion contrast.

## 6. Hook length

Prefer one or two short sentences.

Keep a strict bound and consider lowering the current 320-character maximum after fixture testing.

Do not wrap a good hook in another generic paragraph.

## 7. Remove generic wrappers

Demote/remove templates like:

~~~text
Одна сильная мысль внутри:
<hook>
~~~

or:

~~~text
Причина вернуться к этому:
<hook>
~~~

if the hook already stands on its own.

Preferred final reminder structure:

~~~text
<title>

<hook>
~~~

or the reverse if usability tests show it reads better.

Use one consistent layout.

## 8. Hide ranking internals

Default proactive reminder must not print:

- attention score;
- priority score;
- interest level;
- Item age;
- “Почему сейчас” ranking explanation.

These may be available under Details through POLISH-02.

The notification itself should be about the content.

## 9. Fallback hierarchy

If no valid hook can be used:

1. valid stored v2 hook;
2. concise outcome-first `Item.summary` from POLISH-01 as presentation-only fallback;
3. title only + source/original action.

Important: canonical summary is generated content and must not become *evidence* for a new hook. It is only a rendering fallback.

Do not manufacture intrigue when grounding is weak.

## 10. Hook versioning

Increment:

~~~text
ATTENTION_HOOK_GENERATOR_VERSION
~~~

Old persisted hook rows remain derived historical data.

Do not mass-regenerate all Items.

A reminder candidate lazily generates/reuses only hooks compatible with the current version and still-valid evidence.

## 11. Candidate diversity

When evidence supports it, generate a small set of meaningfully different hook types.

Do not produce three paraphrases of the same claim.

Application may rotate valid candidates deterministically based on existing history/version rules.

## 12. Long-content behavior

Hook context selection must not systematically favor only the first transcript chunk.

Within existing bounded source/chunk limits, preserve opportunities to surface:

- conclusion/end result;
- strong contrast;
- central concrete claim;
- practical technique.

Do not add embeddings or new retrieval infrastructure here.

## 13. Generic motivation

PM-10 generic nudges are not source-specific and therefore cannot use PM-09 hooks.

They remain deterministic templates over already-computed facts.

Improve voice without changing facts or adding an LLM.

Bad:

> В активном списке есть 4 важных Item, сохранённых не меньше месяца назад.

Better:

> Четыре важных штуки лежат уже месяц. Вытащить одну обратно в фокус?

The exact wording remains deterministic and testable.

## 14. Generic template families

For each `MotivationKind`, keep several variants with different tones such as:

- direct;
- playful;
- challenge;
- curiosity;
- progress acknowledgment.

Every placeholder must come from deterministic facts already computed by `MotivationService`.

No free-form invented metrics.

## 15. Avoid manipulation

Do not use:

- shame (“ты опять ничего не сделал”);
- insults;
- fake scarcity;
- fake social comparison;
- threats;
- unsupported streak/loss pressure.

The goal is curiosity and useful momentum, not coercion.

## 16. Notification density unchanged

Do not increase:

- daily caps;
- minimum gaps;
- same-item cooldown;
- intensity limits;
- generic nudge caps.

This PR improves **quality per interruption**, not volume.

## 17. Existing scheduling policy remains authoritative

Preserve PM-08/11:

- quiet hours;
- durable Reminder claims;
- final revalidation;
- reminder fatigue;
- dismissal cooldown;
- explicit negative feedback;
- at-least-once Telegram boundary.

No scheduler rewrite.

## 18. Source/original actions

Every Item-specific reminder should use POLISH-03 source/original projection.

The primary action should make reopening/resending the content obvious.

## 19. Reminder keyboard density

Align with POLISH-02 compact interaction.

Keep source/original access primary.

Done/Later/Not now/Fewer like this remain reachable, but should not visually dominate the hook.

Do not remove PM-11 feedback semantics.

## 20. Hook evaluation fixtures

Add sanitized representative cases:

- strong before/after contrast;
- concrete result;
- practical technique;
- non-obvious fact;
- no-good-hook case;
- prompt-injection source;
- long transcript;
- multiple source languages;
- visual-only/caption-only degradation.

## 21. Quality assertions

For deterministic provider fixtures/mocks verify:

- hook refers only to supplied evidence;
- evidence excerpt is exact;
- no outside statistic/fact;
- no generic “this material is interesting” wording;
- QUESTION is tied to answerable supplied evidence;
- candidates differ meaningfully;
- zero candidates is valid when grounding is weak.

## 22. Notification rendering tests

Cover:

- default proactive body contains no ranking numbers;
- valid hook path;
- no-hook → outcome-first summary fallback;
- no usable summary → title fallback;
- source/original actions remain present;
- reminder feedback/cooldown behavior remains unchanged.

## 23. Generic motivation tests

For every `MotivationKind`:

- placeholders match deterministic facts;
- variants rotate deterministically/history-aware;
- no LLM call;
- current pacing/caps remain;
- negative feedback suppression remains.

## 24. Acceptance criteria

1. Item-specific reminder leads with content-specific hook/result, not ranking explanation.
2. Generic hook wrappers are removed or demoted.
3. Hook generator version changes.
4. Exact PM-09 evidence validation remains.
5. No outside facts or fabricated clickbait.
6. Fallback uses outcome-first summary/title, not score explanation.
7. Priority/attention/interest numbers disappear from default reminder body.
8. Generic motivation becomes shorter/more engaging without LLM or invented facts.
9. Notification frequency policy is unchanged.
10. Source/original actions are clear.
11. Full pytest/ruff gate passes.

## 25. Definition of Done

POLISH-04 is complete when a proactive notification feels like a specific reason to reopen saved content, while PM-08/09/11 grounding, pacing, feedback and delivery guarantees remain intact.

## 26. Current implementation (DONE — PR #50 merged)

- Attention Hook generation uses version 2, keeps exact original-Content evidence
  validation, stores at most one candidate per hook type, and selects bounded
  transcript regions near the beginning, one-third, two-thirds, and end.
- Reminder copy is `🎯 title` plus a direct grounded hook, a whitespace-normalized
  summary preview capped at 700 characters, or the title alone. Summary is only
  a presentation fallback and is never supplied as hook evidence.
- Source/original actions stay primary. Up to two source actions appear beside
  Original; overflow opens Sources, while PM-11 reactions live under More.
- Generic PM-10 copy has four maintained variants per kind and advances through
  them deterministically from successful SENT history. Its facts and scheduler
  policy are unchanged.
- No migration or dependency change is required. PM-08 pacing and PM-11 event
  semantics remain unchanged.
- Merged into `main` in PR #50. POLISH-05 is the next implementation review.
