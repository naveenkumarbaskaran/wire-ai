# WIRE + ReAct Pattern

ReAct (Reasoning + Acting) is the most common production agent pattern.
WIRE governs it without changing your agent logic.

## The ReAct loop

```
THINK → ACT (tool call) → OBSERVE (result) → THINK → ...
```

## What goes wrong without WIRE

| Problem | What happens | WIRE fix |
|---|---|---|
| Infinite loop | Agent thinks forever, costs $100s | `LoopGuard(max_iterations=20)` |
| Double tool fire | Jira ticket created twice on retry | `IdempotencyGuard(idempotent=True)` |
| No audit trail | Can't see what the agent did | `AuditChain` logs every step |
| Runaway cost | Budget exhausted silently | `Budget(max_cost_usd=2.0)` |
| No human gate | Risky actions execute automatically | `HITLGate(trigger=Risk.HIGH)` |

## Run the demo

```bash
pip install wire-ai[langgraph]
python examples/react_agent/run.py
```

No API keys needed — uses a scripted mock LLM.

## Key pattern

```python
import wire

# 1. Register side-effecting tools as idempotent
@wire.tool(idempotent=True)
async def create_jira_ticket(title: str, priority: str) -> dict:
    # Your real Jira API call here
    ...

# 2. Build your ReAct graph (unchanged)
graph = build_react_graph()

# 3. Wrap with WIRE — one call, full governance
governed = wire.deploy(
    graph,
    backend="langgraph",
    max_iterations=20,        # ReAct loops can't run forever
    max_cost_usd=2.0,         # Hard cost ceiling
    audit_path="react-audit.jsonl",
)

# 4. Run exactly as before
result = await governed.ainvoke({"messages": [HumanMessage(content="...")]})

# 5. Verify the audit trail
wire.AuditChain.verify("react-audit.jsonl")
# ✓ 12 steps logged · chain intact
```

## What WIRE adds to ReAct

```
ReAct Loop                    WIRE Governance
──────────────────────────    ──────────────────────────────────────
THINK (step 1)              → AuditChain.write("node_executed")
ACT   (tool call)           → IdempotencyGuard.call(key, fn)
OBSERVE (tool result)       → AuditChain.write("node_executed")
THINK (step 2)              → LoopGuard.tick() — count iterations
ACT   (risky action)        → HITLGate.request() — pause for human
...
FINISH                      → AuditChain.verify() — tamper-proof proof
```
