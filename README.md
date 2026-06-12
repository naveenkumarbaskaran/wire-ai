# WIRE — Workforce Intelligence & Reasoning Engine

**Framework-agnostic governance layer for autonomous enterprise AI agents.**

> *"Describe the work. WIRE hires the workforce."*

[![PyPI](https://img.shields.io/pypi/v/wire-ai)](https://pypi.org/project/wire-ai)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Tests](https://github.com/naveenkumarbaskaran/wire-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/naveenkumarbaskaran/wire-ai/actions)

---

## The Problem

Every major agent framework ships without the five things enterprises actually need:

| Gap | LangGraph | CrewAI | AutoGen |
|-----|-----------|--------|---------|
| Loop protection | ❌ | ❌ | ❌ |
| Tamper-proof audit | ❌ | ❌ | ❌ |
| Hard cost ceilings | ❌ | ❌ | ❌ |
| HITL as first-class primitive | Partial | ❌ | Unstable |
| Live workforce visibility | ❌ | ❌ | ❌ |

WIRE adds all of them — without replacing your existing framework.

---

## Install

```bash
pip install wire-ai                  # core
pip install wire-ai[langgraph]       # + LangGraph adapter
pip install wire-ai[crewai]          # + CrewAI adapter  (Sprint 5)
pip install wire-ai[autogen]         # + AutoGen adapter (Sprint 5)
pip install wire-ai[all]             # everything
```

---

## Sprint 1 — 5 lines, full governance

```python
import wire

# Your existing LangGraph graph — unchanged
from langgraph.graph import StateGraph
graph = StateGraph(...).compile()

# Wrap with WIRE
workforce = wire.deploy(
    graph,
    backend="langgraph",
    max_iterations=30,       # LoopGuard: halt before runaway
    max_cost_usd=0.50,       # Budget: hard $0.50 ceiling
    hourly_budget_usd=0.10,  # Budget: $0.10/hour rolling
    audit_path="audit.jsonl" # AuditChain: tamper-proof log
)

# Run — same API as graph.ainvoke()
result = await workforce.ainvoke({"messages": [...]})

# Verify audit integrity
wire.AuditChain.verify("audit.jsonl")
# ✓ 12 entries · chain intact
```

---

## What You Get

### LoopGuard
Halts runaway agent loops before they exhaust your API quota. Configurable iteration and cost limits. Raises `LoopBreachError` with full context.

```python
# Fires when iterations > 30 OR cost > $0.50
workforce = wire.deploy(graph, max_iterations=30, max_cost_usd=0.50)
```

### AuditChain
Every agent action, node execution, and tool call is recorded in a tamper-proof SHA-256 hash-linked log. Verify integrity at any time.

```python
wire.AuditChain.verify("audit.jsonl")
# Raises AuditChainError with entry index if tampered

# Replay any past run
wire replay --run-id run_20260612_abc123
```

### Budget
Hard cost ceilings with rolling hourly and daily windows. Never get a surprise bill.

```python
budget = wire.Budget(hourly=0.50, daily=5.00)
# BudgetBreachError fires before the ceiling is exceeded
```

### EventBus
Typed events for every runtime moment. Subscribe handlers for alerting, logging, or custom business logic.

```python
@workforce.on(wire.EventKind.LOOP_BREACH)
async def alert(event):
    await slack.send(f"Loop breach in {event.run_id}!")
```

---

## CLI

```bash
wire version                          # v0.1.0
wire status                           # installed adapters
wire audit audit.jsonl                # verify chain integrity
wire replay --run-id abc123           # replay a past run
```

---

## Sprint Roadmap

| Sprint | Ships | Status |
|--------|-------|--------|
| **S1 — MVP** | `deploy()` + `LoopGuard` + `AuditChain` + `Budget` | ✅ **v0.1.0** |
| **S2 — Governance** | `HITLGate` + `IdempotencyGuard` + `SLATracker` | 🔜 v0.2.0 |
| **S3 — HIRE** | `wire.hire("...")` + 20 role templates | 🔜 v0.3.0 |
| **S4 — Visibility** | Live dashboard + Slack HITL + time-travel replay | 🔜 v0.4.0 |
| **S5 — Multi-Framework** | CrewAI + AutoGen + OpenAI adapters | 🔜 v0.5.0 |
| **S6 — Enterprise** | SOC-2 presets + SSO + RBAC + web dashboard | 🔜 v1.0.0 |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  HIRE LAYER     intent → role matching → workforce   │
├──────────────────────────────────────────────────────┤
│  RUNTIME LAYER  LoopGuard · HITLGate · SLATracker    │
│                 IdempotencyGuard · Budget · State     │
├──────────────────────────────────────────────────────┤
│  VISIBILITY     Dashboard · AuditChain · CostLedger  │
│                 TimeTravel · DriftDetector            │
├──────────────────────────────────────────────────────┤
│  ADAPTERS       LangGraph · CrewAI · AutoGen · OpenAI│
└──────────────────────────────────────────────────────┘
```

---

## Why WIRE?

Built from 95 adversarially-verified research claims across 24 sources:

- **LangGraph** self-describes as "very low-level" — no governance primitives
- **AutoGen** Studio is "not meant for production" — their own README
- **CrewAI** has no idempotency guard — payments and emails can fire twice on retry
- **No framework** ships a tamper-proof audit chain, built-in workforce visibility, or HITL routing

WIRE fills every gap. Framework-agnostic. Enterprise-ready from day 1.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.

---

*Built by [Naveen Kumar Baskaran](https://github.com/naveenkumarbaskaran)*
