# AWS Cost Governance — WIRE AI Demo

A complete, runnable demo showing WIRE AI governance on a real LangGraph agent.
No API keys required — all LLM calls are mocked.

## What it demonstrates

| WIRE Feature | Where used |
|---|---|
| `wire.deploy()` | `run.py` — wraps LangGraph graph with full governance |
| **LoopGuard** | Halts the agent at 20 iterations; prevents runaway loops |
| **AuditChain** | Every node execution is hash-linked to a tamper-proof JSONL log |
| **IdempotencyGuard** | Jira tickets and Slack alerts are created exactly once, even on retry |
| **HITL** | CRITICAL-risk anomalies (>$1 000/day) pause for human approval |
| **EventBus** | Typed runtime events (`STEP_END`, `LOOP_BREACH`, `BUDGET_BREACH`) captured live |
| `wire.hire()` | Natural-language intent → workforce plan (no LLM required) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     WIRE Governance Layer                        │
│  LoopGuard · AuditChain · Budget · EventBus · IdempotencyGuard  │
└────────────────────────┬────────────────────────────────────────┘
                         │  wire.deploy(graph, backend="langgraph")
┌────────────────────────▼────────────────────────────────────────┐
│                  LangGraph Agent (no real LLM)                   │
│                                                                  │
│   START                                                          │
│     │                                                            │
│     ▼                                                            │
│  cost_monitor ─── get_aws_costs(7 days) ──► summary message     │
│     │                                                            │
│     ▼                                                            │
│  anomaly_detector ── avg > $500/day? ──► anomaly_info           │
│     │                    │                                       │
│   (clean)           (anomaly found)                              │
│     │                    │                                       │
│    END           action_executor                                 │
│                       │                                          │
│                 create_jira_ticket()  ◄── IdempotencyGuard       │
│                 send_slack_notification()                        │
│                       │                                          │
│                       ▼                                          │
│                      END                                         │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼  (if CRITICAL anomaly > $1 000/day)
┌─────────────────────────┐
│    HITL Gate (CLI)       │
│  approve / reject / modify  →  auto-approve after 3s in demo    │
└─────────────────────────┘
```

## 5-minute setup

**1. Install dependencies**

```bash
cd examples/aws-cost-governance
pip install -r requirements.txt
```

> If you are working from the repo root, install the wire package first:
> ```bash
> pip install -e ".[langgraph]"
> ```

**2. Run the demo**

```bash
python run.py
```

That is all. No `.env` file, no API key, no cloud credentials.

**3. Inspect the audit log**

After running, a `wire-cost-audit.jsonl` file is created in the working directory.

```bash
# Verify chain integrity (CLI)
wire audit wire-cost-audit.jsonl

# Replay a specific run
wire replay --run-id <run_id>

# Or verify programmatically
python - <<'EOF'
import wire
count = wire.AuditChain.verify("wire-cost-audit.jsonl")
print(f"Chain intact — {count} entries")
EOF
```

## What you will see

```
╭──────────────────────────────────────────────────────────────╮
│      WIRE AI Governance Demo                                  │
│      AWS Cost Governance on LangGraph — no API keys required  │
╰──────────────────────────────────────────────────────────────╯

── WIRE hire() — natural-language workforce assembly ───────────
╭─ Assembled workforce plan ────────────────────────────────────╮
│  WorkforceGraph                                               │
│    roles: CostMonitor, AnomalyDetector, ActionExecutor        │
│    ...                                                        │
╰───────────────────────────────────────────────────────────────╯

── AWS Cost Window (7 days) ────────────────────────────────────
  Date        EC2       RDS     Lambda   S3    CloudFront  Total     Anomaly
  2025-01-09  $312.50   $187.20  $28.40  $42.10  $31.80  $602.00
  2025-01-10  $748.30   $214.60  $18.90  $61.40  $44.20  $1087.40  🔥 EC2 2.4x
  ...

── Anomaly Detection Results ────────────────────────────────────
  ╭─ Risk: HIGH ─────────────────────────────────────────────────╮
  │  Service:        EC2                                          │
  │  Avg daily spend: $487.20  (threshold: $500)                 │
  │  ...                                                          │
  ╰──────────────────────────────────────────────────────────────╯

── Actions Taken ────────────────────────────────────────────────
  ✓ Jira ticket: COST-4821  priority=High
  ✓ Slack: #ops-alerts  status=sent

── HITL Gate (CRITICAL anomalies only) ──────────────────────────
  (appears only if any service avg > $1 000/day)
  Auto-approving in 3... 2... 1...
  ✓ HITL decision: APPROVE  actor=wire:demo-auto-approve

── AuditChain Verification ──────────────────────────────────────
  ✓ Audit chain intact — 8 entries verified
```

## File structure

```
aws-cost-governance/
├── mock_aws.py       Mock AWS Cost Explorer — deterministic, seeded by date
├── tools.py          Async tool stubs: create_jira_ticket, send_slack_notification
├── agent.py          LangGraph graph with MockLLM — no API keys needed
├── run.py            Main entry point with Rich UI and all WIRE features
├── requirements.txt  Minimal dependencies
└── README.md         This file
```

## Adapting to a real AWS environment

1. Replace `mock_aws.py` with real AWS Cost Explorer calls using `boto3`:
   ```python
   import boto3
   ce = boto3.client("ce", region_name="us-east-1")
   ```

2. Replace the mock functions in `tools.py` with real Jira and Slack clients.

3. Change the `MockLLM` in `agent.py` to a real model:
   ```python
   from langchain_anthropic import ChatAnthropic
   model = ChatAnthropic(model="claude-haiku-4-5-20251001")
   ```

4. For production HITL, set `channel=HITLChannel.SLACK` in `run_hitl_simulation()`
   and supply a `SLACK_BOT_TOKEN` environment variable.
