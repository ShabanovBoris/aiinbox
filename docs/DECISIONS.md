# Architecture Decisions

Журнал значимых архитектурных решений, чей рационал иначе потеряется.
Формат записи: Context / Decision / Reason / Consequences (см. AGENTS.md, раздел про DECISIONS.md).
Фиксируются только реально принятые решения, с указанием этапа.
Изменение ядра системы — только через новую запись здесь, а не молча в очередной задаче.

## D-001 — Resumable modular monolith (этап 0)

Context: тяжёлые этапы обработки (download страницы, ffmpeg, transcription, extraction)
дороги и медленны. PRODUCT_SPEC §59 требует продолжения обработки с последнего
доступного результата, §60 — хранение текущего stage.

Decision: Item — единица, накапливающая промежуточные результаты по стадиям:

```text
Item
 ├── source
 ├── processing_stage
 ├── extracted_content
 ├── transcript
 ├── visual_notes
 ├── analysis
 └── resulting state
```

Retry и перезапуск процесса продолжают с последнего готового результата, а не с нуля.

Reason: сбой позднего этапа (analysis) не должен повторять
download → ffmpeg → transcription; надёжность продукта и продолжаемость работы
новой сессией агента — одно и то же свойство системы.

Consequences: `processing_status` (QUEUED/PROCESSING/READY/FAILED) не отражает
глубину прогресса — глубину отражает набор сохранённых промежуточных результатов
плюс `processing_stage`.

## D-002 — Стабильное ядро, заменяемые края (этап 0)

Context: ТЗ требует возможности Android-клиента и сменного LLM provider
без переписывания доменной части.

Decision: каркас фиксирован:

```text
Telegram ─┐
          ├→ Ingestion → Extract → NormalizedContent
Future API┘                         ↓
                              Analyzer
                                  ↓
                            PriorityEngine
                                  ↓
                               SQLite
```

- Края системы — заменяемые, изолируются в handlers / extractors / llm adapters:
  Telegram, YouTube/yt-dlp, OpenAI, Playwright.
- Стабильное ядро — не переписывается при добавлении края: `NormalizedContent`,
  `AnalysisResult`, `PriorityEngine`, состояния Item.

Reason: каждую задачу выполняет новая сессия агента; без фиксированных инвариантов
каркас будет дрейфовать от задачи к задаче.

Consequences: новый край (HTTP API, Ollama, новый extractor) добавляется как adapter
поверх ядра и не требует переписывания проекта.

## D-003 — Внешний Orchestrator и PR-workflow (принято 2026-09-12)

Context: проект реализуется сессиями агента; без внешнего контроля acceptance
дрейфует (self-approval, scope creep, прямые коммиты в основную ветку).

Decision: принят `Оркестрационный протокол Codex → Reviewer.md`. Агент —
implementation agent; Orchestrator (ChatGPT, фиксированная беседа, открывается
через Browser Use) принимает acceptance, merge, архитектурные pivots, scope
change и переходы между фазами. Реализация фаз — в ветках `phase/NN-*`, PR →
`main`, `REVIEW REQUEST` → `APPROVED / CHANGES REQUIRED / BLOCKED`, squash merge.
`main` защищён: без direct commit, force push, self-merge.

Reason: протокол задан пользователем как высшая инструкция после его явных
решений; он же устраняет self-approval и делает историю фаз проверяемой через
GitHub (PR metadata, diff, SHA).

Consequences: `DONE` в IMPLEMENTATION_STATE достигается только через
`IN_REVIEW` + `APPROVED`; между `REVIEW REQUEST` и ответом Orchestrator'а —
никакого scope expansion (§12). AGENTS.md §2/§48/§57 и RUNBOOK приведены
в соответствие протоколу.
