# Browser & Integration Track

Status: current product direction  
Baseline: PM-18 HTTP API DONE in PR #56

## 1. Execution sequence

~~~text
PM-18 HTTP API — DONE
        ↓
PM-33 Browser Extension
        ↓
Integration Sync Foundation
        ↓
Miro Connector v1
        ↓
additional outbound destinations
~~~

This sequence is product priority, not a renumbering of the historical PM
registry.

## 2. Specifications

- [PM-33 — Browser Extension](PM-33_BROWSER_EXTENSION.md)
- [Integration Sync Foundation](INTEGRATION_SYNC_FOUNDATION.md)
- [Miro Connector v1](INTEGRATION_MIRO_V1.md)
- [Additional Destination Strategy](INTEGRATION_DESTINATIONS.md)

## 3. Architectural split

External interactive clients:

~~~text
Browser Extension / future Android / external tools
                    ↓
                PM-18 /v1
                    ↓
           application services
~~~

Server-side outbound connectors:

~~~text
canonical AIInbox state
          ↓
integration projection service
          ↓
destination adapter
          ↓
Miro / later platform
~~~

Server-side connectors do not call the local PM-18 API just to reach data in the
same process.

## 4. Source of truth

AIInbox remains canonical for:

- Item identity;
- sources/provenance;
- analysis;
- processing state;
- lifecycle state;
- interest;
- priority/attention inputs;
- event/feedback history.

External systems are projections.

V1 integration direction is one-way:

~~~text
AIInbox → destination
~~~

Remote edits/deletions do not silently mutate canonical AIInbox state.

## 5. Current milestone statuses

- PM-18 HTTP API — DONE, PR #56.
- PM-33 Browser Extension — PLANNED / next client focus.
- Integration Sync Foundation — PLANNED after or alongside PM-33 stabilization.
- Miro Connector v1 — PLANNED, first destination over the foundation.
- Additional destinations — SELECT AFTER REAL MIRO USAGE.

## 6. Important non-dependencies

PM-33 does not require:

- PM-14 semantic search;
- PM-16 LLM routing;
- PM-17 Ollama;
- PM-19 Android;
- PM-20 calendar.

The integration foundation also does not require those epics.

## 7. Delivery principle

Do not implement several connectors in parallel.

Prove:

1. browser capture over PM-18;
2. one durable provider-neutral sync foundation;
3. one real Miro connector;
4. real usage and failure recovery.

Only then select the next destination.

## 8. Success outcome

The track succeeds when:

- saving from a browser does not require Telegram;
- every existing AIInbox save can be projected into Miro;
- new/changed Items propagate incrementally;
- provider outages never block capture/analysis;
- adding a second destination does not change Item business logic;
- no external platform becomes a hidden canonical database.
