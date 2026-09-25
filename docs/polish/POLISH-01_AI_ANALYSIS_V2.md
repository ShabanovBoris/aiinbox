# POLISH-01 — AI Analysis v2: Outcome-first Understanding

Type: Stabilization / Product-quality specification  
Depends on: current ingestion + Analyzer + PriorityEngine  
Blocks: POLISH-02, POLISH-04, PM-14+ rollout  
Status: IN_REVIEW

## 1. Problem

The current analyzer is structurally correct but user-facing analysis is too generic and too influenced by profile/category history.

Observed problems:

- summaries often begin with meta language such as “the video discusses…” instead of the conclusion;
- long videos do not immediately answer “what did this lead to / what is the conclusion?”;
- `next_action` and `priority_reason` are generated but consume presentation space without adding proportional user value;
- category selection overuses profile context and previously used categories, causing broad programming content to collapse into `Android-разработка`;
- category and ItemType are currently treated as more product-important than the user perceives them;
- quality is not protected by a representative fixture set, so prompt changes can fix one example while regressing another.

## 2. Goal

Make every analyzed Item answer, in this order:

1. **What happened / what did the author conclude?**
2. **What are the 1–3 essential supporting points?**
3. **What is the actual topic/category of the content?**

The analysis should be concise, specific and content-grounded. Internal ranking fields may remain available to the system but must not drive verbose user-facing prose.

## 3. Core product rule

For article/video/document content, the first sentence of `summary` should communicate the outcome, conclusion, recommendation, result, or main claim whenever one exists.

Bad:

> Видео обсуждает изменение подхода к skills и plugins в AI-разработке.

Good:

> Автор пришёл к выводу, что постоянно подключённые skills и plugins чаще мешают агенту, чем помогают, и советует добавлять их только при конкретной проблеме.

Do not fabricate a conclusion if the source is exploratory or genuinely inconclusive. In that case state the unresolved point directly.

## 4. In scope

- revise `AnalysisResult` semantics and analyzer prompt;
- outcome-first summary contract;
- stricter category semantics;
- profile/category-history separation from topic classification;
- preserve internal priority factors;
- make `next_action` / `priority_reason` internal metadata rather than default copy;
- representative AI-quality fixture suite;
- regression coverage for multi-source, long-content and preferred-language behavior;
- documentation.

## 5. Out of scope

- Telegram keyboard redesign (POLISH-02);
- original-message/source navigation (POLISH-03);
- hook/nudge copy redesign (POLISH-04);
- command menu / Ask reliability (POLISH-05);
- semantic search (PM-14);
- model routing (PM-16).

## 6. AnalysisResult compatibility

Do not remove existing persisted fields in this PR unless a strong migration reason is discovered.

Keep initially:

- `importance`;
- `urgency`;
- `goal_fit`;
- `long_term_value`;
- `interest_fit`;
- `estimated_action_minutes`;
- `next_action`;
- `priority_reason`.

Reason: PriorityEngine, Attention and future calendar work already consume these fields.

The PR changes generation semantics and the presentation contract, not necessarily schema shape.

## 7. Outcome-first summary contract

Update the analyzer system prompt so `summary` normally follows:

~~~text
Sentence 1:
- conclusion / result / recommendation / strongest resolved claim

Sentence 2–4:
- only the few points necessary to understand why that conclusion matters
~~~

Maximum remains bounded; normal output should be much shorter than the current 1200-char ceiling.

Canonical summary should remain reusable outside Telegram, so do not force Telegram-specific bullet markup into the stored field.

## 8. Forbidden summary style

Explicitly discourage empty meta-language:

- “Видео рассказывает…”;
- “Статья рассматривает…”;
- “Автор обсуждает…” when it can be replaced by the actual claim;
- “Это может быть полезно…”;
- “Стоит посмотреть/прочитать…”;
- generic praise such as “интересный материал”.

Such wording is acceptable only when the fact that something is a discussion/review is itself substantive.

## 9. Long-content behavior

Chunk summaries remain intermediate evidence, not final copy.

For 30+ minute content, aggregate analysis should prioritize:

- final recommendation/conclusion;
- explicit before/after contrast;
- result of an experiment/demo;
- what changed over the course of the material;
- unresolved question only when no conclusion exists.

Do not let the first chunk dominate the final summary.

## 10. Multi-source behavior

Existing composite-Item semantics remain.

If sources form one topic, synthesize them.

If sources genuinely contain distinct topics, summary may mention multiple themes compactly; do not invent a unifying conclusion.

Failed/partial source content must never be inferred.

## 11. Category semantics

Category answers:

> “What is this content mainly about?”

Category does **not** answer:

> “What is relevant to this user's profession?”

Examples:

- piano technique → `Пианино`;
- general AI agents → `ИИ` or stable equivalent;
- Kotlin language feature → `Kotlin`;
- Android-specific API/library → `Android-разработка`;
- job search/interviews → `Поиск работы`;
- property/mortgage → `Недвижимость`;
- investing/budgeting → `Финансы`.

## 12. Profile separation

User profile may influence:

- `goal_fit`;
- `interest_fit`;
- relevance/importance interpretation where appropriate;
- response language.

User profile must not force category/topic.

Add an explicit system-level rule conceptually:

~~~text
The user profile is preference context, not topical evidence.
Do not assign a category because it matches the user's profession,
domains or goals. Assign category from the captured content itself.
~~~

## 13. Existing-category reuse

Current “prefer an existing category when it fits” rule remains only with a stricter definition of “fits”.

Reuse an existing category only when semantically equivalent to the primary content topic.

Do not use a broad familiar personal-domain category merely to avoid creating a new topic.

Examples:

- existing `Android-разработка` must not absorb `Kotlin` unless Android is actually central;
- existing `ИИ` must not absorb `Поиск работы` just because AI tools appear in the source.

## 14. Category granularity

Avoid both extremes:

- too broad: `Разное`, `Программирование`, `Интересное` for clearly specific content;
- too narrow: one-off categories that are effectively article titles.

Prefer a reusable topic noun/phrase.

## 15. ItemType semantics

Keep the existing enum for compatibility:

`ACTION`, `LEARN`, `READ`, `WATCH`, `IDEA`, `REFERENCE`, `SOMEDAY`.

But make prompt semantics explicit:

- type describes interaction/nature of the saved unit;
- it is not the main user-facing taxonomy;
- source medium alone must not determine type;
- type must not distort category.

POLISH-02 hides ItemType from default presentation.

Do not alter Attention eligibility in this PR; that requires a separate explicit policy decision.

## 16. next_action

Continue generating it because current ranking/future scheduling may consume it.

Rules:

- concrete and short;
- may be null;
- do not merely restate source medium;
- no generic “прочитать статью” / “посмотреть видео” when the source itself is already saved for later.

Bad:

> Посмотреть видео и оценить, нужно ли удалять skills.

Better when supported:

> Проверить один рабочий агент без глобальных skills/plugins.

If no meaningful action follows from the content, return null.

## 17. priority_reason

Keep as internal explainability metadata.

It should:

- explain scoring-relevant factors;
- not duplicate summary;
- not act as “why you should open this” copy;
- remain bounded and factual.

POLISH-02 removes it from the default card.

## 18. Title contract

Title should communicate the substantive idea rather than source format.

Avoid:

- “Видео про…”;
- “Статья о…”;
- generic imported page title when it hides the thesis.

Prefer concise content-specific wording.

## 19. Language

Preserve current preferred-language behavior.

Title and summary may be generated in `preferred_language` where current product rules require it; canonical `language` continues to represent source language.

Do not translate exact evidence fields used by other grounding systems.

## 20. Prompt injection

All current untrusted-content protections remain.

The stronger outcome/category instructions belong to system-level task rules and must not be overridable by source text.

## 21. Quality fixture set

Add a versioned, sanitized representative fixture/evaluation set.

Minimum themes:

- general AI;
- AI agents/tools;
- Android-specific programming;
- Kotlin not tied to Android;
- job search;
- piano/music practice;
- finance;
- real estate;
- short Reel;
- long YouTube video;
- article;
- document;
- composite source;
- inconclusive discussion;
- prompt-injection content.

Prefer examples derived from real observed failures while removing private material.

## 22. Fixture assertions

Do not require exact live-model prose in CI.

Test deterministic application constraints and, for mocked provider outputs/evaluation fixtures, properties such as:

- expected category or explicit narrow allowlist;
- first summary sentence contains the actual outcome concept;
- forbidden generic meta phrases absent;
- no unsupported invented detail;
- correct response language;
- `next_action` null or concrete;
- multi-source substantive themes retained.

Optional live-model evaluation may be developer-run, not default CI.

## 23. Existing scoring

PriorityEngine stays deterministic.

Do not let the LLM output `priority_score`.

Do not modify PM-06/07 ranking formulas in this PR.

## 24. Tests

Cover at minimum:

- strict `AnalysisResult` validation remains;
- system prompt contains content-only category rule;
- profile cannot be topical evidence in fixture expectations;
- long-content aggregate prompt prioritizes conclusion/result;
- category-history reuse is not forced;
- prompt-injection handling remains;
- preferred language remains;
- multi-source completeness remains;
- PriorityEngine inputs still populate correctly.

## 25. Docs

Update:

- `docs/PRODUCT_SPEC.md`;
- analyzer/prompt sections in developer docs where present;
- `docs/DECISIONS.md` if profile/topic separation warrants a durable decision.

## 26. Acceptance criteria

1. Summary is outcome-first when the source has an outcome/conclusion.
2. Long-video analysis does not merely describe the topic.
3. Generic meta-summary phrases are explicitly discouraged and fixture-covered.
4. Category is derived from content, not profession/profile.
5. Existing category reuse cannot force unrelated topics into a familiar category.
6. Kotlin, job-search, piano, finance and real-estate fixtures classify sensibly.
7. ItemType remains compatible but is not the primary taxonomy.
8. `next_action` is concrete-or-null rather than medium boilerplate.
9. `priority_reason` remains internal explainability metadata.
10. PriorityEngine remains deterministic.
11. Prompt-injection boundary remains intact.
12. Multi-source and partial-source behavior remains correct.
13. Representative quality fixtures exist.
14. Full pytest/ruff gate passes.

## 27. Definition of Done

POLISH-01 is complete when saved content is represented first by its actual conclusion/result and topic, while profile personalization affects relevance rather than corrupting topic classification.
