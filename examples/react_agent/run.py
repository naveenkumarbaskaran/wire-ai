"""
WIRE + ReAct Pattern — Governed Reasoning & Acting Agent

ReAct (Reasoning + Acting) is the most common production agent pattern:
  1. THINK  — LLM reasons about the current state
  2. ACT    — LLM calls a tool
  3. OBSERVE — Tool result fed back
  4. REPEAT until answer found

The problem: unprotected ReAct loops can:
  - Run forever (cost $100s before you notice)
  - Fire side-effecting tools twice on retry
  - Produce no audit trail
  - Have no human approval gate for risky actions

WIRE fixes all four — without changing your agent logic.

Run: pip install wire-ai[langgraph] langchain-anthropic
     python examples/react_agent/run.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ── Mock tools (no real APIs needed) ─────────────────────────────────────────

TOOL_CALLS = []  # track all tool executions


async def search_aws_costs(service: str, days: int = 7) -> dict:
    """Mock: query AWS cost explorer."""
    TOOL_CALLS.append(("search_aws_costs", service))
    import random; random.seed(hash(service) % 100)
    cost = random.uniform(200, 900)
    return {
        "service": service,
        "cost_usd": round(cost, 2),
        "days": days,
        "threshold_exceeded": cost > 500,
    }


async def get_budget_status() -> dict:
    """Mock: check current month budget."""
    TOOL_CALLS.append(("get_budget_status", "monthly"))
    return {"budget_usd": 5000, "spent_usd": 3847, "remaining_usd": 1153, "pct_used": 77}


async def create_jira_ticket(title: str, priority: str = "High", cost_usd: float = 0) -> dict:
    """Mock: create Jira ticket — side-effecting, must be idempotent."""
    TOOL_CALLS.append(("create_jira_ticket", title))
    ticket_id = f"COST-{abs(hash(title)) % 9999}"
    print(f"  📋 [Jira] Created {ticket_id}: {title[:50]} (priority={priority})")
    return {"ticket_id": ticket_id, "url": f"https://jira.example.com/browse/{ticket_id}"}


async def notify_slack(channel: str, message: str) -> dict:
    """Mock: send Slack notification — side-effecting, must be idempotent."""
    TOOL_CALLS.append(("notify_slack", channel))
    print(f"  💬 [Slack] #{channel}: {message[:80]}")
    return {"status": "sent", "channel": channel}


# ── ReAct agent using LangGraph ───────────────────────────────────────────────

def build_react_graph():
    """Build a simple ReAct LangGraph agent with mock LLM."""
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from langgraph.graph import StateGraph, END, START, MessagesState

    # Tool definitions
    TOOLS = {
        "search_aws_costs": search_aws_costs,
        "get_budget_status": get_budget_status,
        "create_jira_ticket": create_jira_ticket,
        "notify_slack": notify_slack,
    }

    # Scripted ReAct mock LLM — simulates real reasoning without API key
    _step = [0]
    REACT_SCRIPT = [
        # Step 1: THINK → decide to check costs
        {
            "thought": "I need to check AWS costs. Let me start with EC2.",
            "action": "search_aws_costs",
            "args": {"service": "EC2", "days": 7}
        },
        # Step 2: OBSERVE EC2 is high → check budget
        {
            "thought": "EC2 costs are high. Let me check the overall budget status.",
            "action": "get_budget_status",
            "args": {}
        },
        # Step 3: OBSERVE budget → create ticket
        {
            "thought": "Budget is 77% used and EC2 is over threshold. Creating a Jira ticket.",
            "action": "create_jira_ticket",
            "args": {"title": "EC2 cost anomaly — $534 in 7 days", "priority": "High", "cost_usd": 534}
        },
        # Step 4: OBSERVE ticket created → notify team
        {
            "thought": "Ticket created. Now notifying the ops team via Slack.",
            "action": "notify_slack",
            "args": {"channel": "ops-alerts", "message": "🚨 EC2 cost anomaly detected — Jira ticket created"}
        },
        # Step 5: FINAL ANSWER
        {
            "thought": "All actions complete. EC2 overspend detected, ticket created, team notified.",
            "action": None,
            "args": {}
        },
    ]

    def agent_node(state: MessagesState) -> dict:
        """ReAct reasoning step — THINK then decide to ACT or FINISH."""
        step = REACT_SCRIPT[min(_step[0], len(REACT_SCRIPT) - 1)]
        _step[0] += 1

        print(f"\n  🧠 THINK: {step['thought']}")

        if step["action"] is None:
            return {"messages": [AIMessage(content=f"FINAL ANSWER: {step['thought']}")]}

        # Emit tool call
        import json
        tool_call = {
            "id": f"call_{_step[0]}",
            "name": step["action"],
            "args": step["args"],
        }
        msg = AIMessage(
            content=f"Reasoning: {step['thought']}",
            tool_calls=[tool_call],
        )
        return {"messages": [msg]}

    async def tool_node(state: MessagesState) -> dict:
        """ACT + OBSERVE — execute tool and return result."""
        last = state["messages"][-1]
        results = []
        for tc in getattr(last, "tool_calls", []):
            tool_fn = TOOLS.get(tc["name"])
            print(f"  ⚡ ACT:   {tc['name']}({tc['args']})")
            if tool_fn:
                result = await tool_fn(**tc["args"])
            else:
                result = {"error": f"unknown tool: {tc['name']}"}
            print(f"  👁  OBS:  {result}")
            results.append(ToolMessage(
                content=str(result),
                tool_call_id=tc["id"],
            ))
        return {"messages": results}

    def should_continue(state: MessagesState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


# ── Main: WIRE-governed ReAct ─────────────────────────────────────────────────

async def main():
    import wire

    print("\n" + "="*60)
    print("  WIRE + ReAct Pattern Demo")
    print("  Governed: LoopGuard · Audit · Idempotency · EventBus")
    print("="*60)

    # ── 1. Describe the workforce ─────────────────────────────────────────────
    print("\n[1] wire.hire() — assemble workforce from plain language")
    workforce = wire.hire(
        "Monitor AWS costs, detect anomalies, create Jira tickets, notify ops team"
    )
    print(workforce.describe())

    # ── 2. Build the ReAct graph ──────────────────────────────────────────────
    print("\n[2] Building ReAct LangGraph agent...")
    graph = build_react_graph()

    # ── 3. Register tools with IdempotencyGuard ───────────────────────────────
    print("\n[3] Registering side-effecting tools with @wire.tool(idempotent=True)")

    @wire.tool(idempotent=True, description="Create Jira ticket for cost anomaly")
    async def governed_jira(title: str, priority: str = "High", cost_usd: float = 0) -> dict:
        return await create_jira_ticket(title=title, priority=priority, cost_usd=cost_usd)

    @wire.tool(idempotent=True, description="Send Slack notification to ops")
    async def governed_slack(channel: str, message: str) -> dict:
        return await notify_slack(channel=channel, message=message)

    print(f"  ✓ {len(wire.tools.list())} tools registered (idempotent=True)")

    # ── 4. Wrap with WIRE governance ──────────────────────────────────────────
    print("\n[4] wire.deploy() — wrap ReAct graph with governance")
    governed = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=20,        # LoopGuard: halt runaway ReAct loops
        max_cost_usd=2.0,         # Budget: $2 max per run
        hourly_budget_usd=5.0,    # Budget: $5/hour rolling
        audit_path="/tmp/wire-react-audit.jsonl",
    )

    # Subscribe to events
    events = []
    @governed.on(wire.EventKind.STEP_END)
    async def on_step(event):
        events.append(event)

    @governed.on(wire.EventKind.LOOP_BREACH)
    async def on_breach(event):
        print(f"\n  🚨 LOOP BREACH at iteration {event.data['iterations']}")

    print(f"  ✓ Governance active:")
    print(f"     LoopGuard   — max 20 iterations (ReAct loops can't run forever)")
    print(f"     Budget      — $2.00 ceiling (runaway costs blocked)")
    print(f"     AuditChain  — every THINK/ACT/OBSERVE step logged")
    print(f"     EventBus    — typed events on every state transition")

    # ── 5. Run the ReAct agent ────────────────────────────────────────────────
    from langchain_core.messages import HumanMessage
    print("\n[5] Running ReAct loop...\n" + "─"*50)

    result = await governed.ainvoke({
        "messages": [HumanMessage(
            content="Analyse our AWS costs for the past 7 days. "
                    "If any service exceeds $500, create a Jira ticket and notify the ops team."
        )]
    })

    print("─"*50)

    # ── 6. Idempotency demonstration ──────────────────────────────────────────
    print("\n[6] Idempotency test — retry the SAME task 3 times")
    jira_calls_before = sum(1 for t, _ in TOOL_CALLS if t == "create_jira_ticket")
    key = wire.IdempotencyGuard.make_key(
        "create_jira_ticket",
        {"title": "EC2 cost anomaly — $534 in 7 days", "priority": "High", "cost_usd": 534}
    )
    guard = wire.IdempotencyGuard()
    for i in range(3):
        result_i, was_dup = await guard.call(
            key=key,
            fn=lambda: create_jira_ticket(
                title="EC2 cost anomaly — $534 in 7 days", priority="High", cost_usd=534
            ),
            run_id="retry-test",
            tool="create_jira_ticket",
        )
        print(f"  Attempt {i+1}: {'DEDUPLICATED (skipped)' if was_dup else 'EXECUTED'} → {result_i}")
    jira_calls_after = sum(1 for t, _ in TOOL_CALLS if t == "create_jira_ticket")
    print(f"  ✓ 3 calls → {jira_calls_after - jira_calls_before} actual executions (idempotent)")

    # ── 7. Audit chain verification ───────────────────────────────────────────
    print("\n[7] AuditChain verification")
    count = wire.AuditChain.verify("/tmp/wire-react-audit.jsonl")
    print(f"  ✓ {count} steps logged · chain intact · tamper-proof")

    # ── 8. Summary ────────────────────────────────────────────────────────────
    print(f"\n[8] EventBus received {len(events)} step_end events")
    print(f"    Total tool calls: {len(TOOL_CALLS)}")
    print(f"    Tool call log: {TOOL_CALLS}")

    print("\n" + "="*60)
    print("  WIRE ReAct governance summary:")
    print(f"  ✓ LoopGuard     — {len(events)} iterations, never ran away")
    print(f"  ✓ AuditChain    — {count} steps, cryptographically verified")
    print(f"  ✓ Idempotency   — 3 retries → 1 execution (no duplicate tickets)")
    print(f"  ✓ EventBus      — {len(events)} typed events emitted")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
