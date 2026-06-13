# WIRE Examples

Runnable production patterns — no API keys required.

All examples use a mock LLM with scripted responses and mock tool implementations, so they work out of the box after a single `pip install`.

```
pip install wire-ai[langgraph]
```

## Examples

| Example | Directory | WIRE primitives | Run |
|---|---|---|---|
| **ReAct Cost Monitor** | `react_agent/` | LoopGuard, IdempotencyGuard, AuditChain, EventBus, Budget | `python examples/react_agent/run.py` |
| **Incident Response** | `incident_response/` | HITLGate, IdempotencyGuard, AuditChain | `python examples/incident_response/run.py` |
| **Data Pipeline QA** | `data_pipeline/` | SLATracker, AuditChain, Budget($1), EventBus | `python examples/data_pipeline/run.py` |
| **Customer Support Triage** | `customer_support/` | HITLGate, @wire.tool(idempotent=True), IdempotencyGuard, AuditChain | `python examples/customer_support/run.py` |

## What each example shows

### ReAct Cost Monitor (`react_agent/`)
Full ReAct (Reasoning + Acting) loop — the most common production agent pattern. Detects AWS cost anomalies, opens Jira tickets, and pages the ops team. Demonstrates:
- `LoopGuard` preventing runaway loops (max 20 iterations)
- `IdempotencyGuard` deduplicating Jira ticket creation on retry
- `AuditChain` logging every THINK/ACT/OBSERVE step

### Incident Response (`incident_response/`)
PagerDuty-style triage: acknowledge alerts, look up the runbook, create P1 incidents, notify on-call. Demonstrates:
- `HITLGate` pausing execution for human approval before any P1 incident is created (auto-approves after 2 s in demo via `TimeoutAction.APPROVE`)
- `IdempotencyGuard` ensuring no duplicate incidents or pages on retry

### Data Pipeline QA (`data_pipeline/`)
Data quality monitoring: run rule checks, quarantine bad rows, send compliance report. Demonstrates:
- `SLATracker` measuring end-to-end wall-clock time via the `async with tracker.measure()` context manager
- 60 s response ceiling and $1 cost cap enforced per pipeline run

### Customer Support Triage (`customer_support/`)
Ticket triage: search KB, fetch CRM history, escalate premium customers, send reply. Demonstrates:
- `@wire.tool(idempotent=True)` decorator preventing duplicate customer emails
- `HITLGate` requiring human approval before escalating a premium customer
