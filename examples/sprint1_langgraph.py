"""
Sprint 1 example — wrap an existing LangGraph graph with WIRE governance.

What this adds in 5 lines:
  - LoopGuard:   halts at 30 iterations (no more runaway loops)
  - Budget:      hard $0.50 ceiling per run, $0.10/hour
  - AuditChain:  tamper-proof JSONL log of every node execution
  - EventBus:    typed events for downstream handlers (alerting, logging)

Run:
    pip install wire-ai[langgraph] langchain-anthropic
    python examples/sprint1_langgraph.py
"""

from __future__ import annotations

import asyncio

import wire
from wire.core.events import EventKind


# ── 1. Build any LangGraph graph as you normally would ──────────────────────

def build_example_graph():
    """Minimal LangGraph graph for demonstration."""
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage
        from langgraph.graph import END, START, StateGraph, MessagesState

        model = ChatAnthropic(model="claude-haiku-4-5-20251001")

        def call_model(state: MessagesState):
            return {"messages": [model.invoke(state["messages"])]}

        graph = StateGraph(MessagesState)
        graph.add_node("agent", call_model)
        graph.add_edge(START, "agent")
        graph.add_edge("agent", END)
        return graph.compile()

    except ImportError:
        print("Install langchain-anthropic to run the full example.")
        print("Showing WIRE setup only.\n")
        return None


async def main() -> None:
    graph = build_example_graph()

    # ── 2. Wrap with WIRE — this is the only change ─────────────────────────
    workforce = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=30,          # LoopGuard: halt at 30 steps
        max_cost_usd=0.50,          # Budget: lifetime $0.50 ceiling
        hourly_budget_usd=0.10,     # Budget: $0.10/hour rolling window
        audit_path="wire-audit.jsonl",  # AuditChain: tamper-proof log
    )

    # ── 3. Subscribe to WIRE events ──────────────────────────────────────────
    @workforce.on(EventKind.LOOP_BREACH)
    async def on_loop_breach(event):
        print(f"\n🚨 Loop breach! {event.data['iterations']} iterations, "
              f"${event.data['cost_usd']:.4f} spent")

    @workforce.on(EventKind.BUDGET_BREACH)
    async def on_budget_breach(event):
        print(f"\n💸 Budget breach [{event.data['window']}]! "
              f"${event.data['spent']:.4f} / ${event.data['limit']:.4f}")

    @workforce.on(EventKind.STEP_END)
    async def on_step(event):
        print(f"  ✓ {event.data['node']} (iter {event.data['iteration']}, "
              f"${event.data.get('cost_usd', 0):.6f})")

    # ── 4. Run ───────────────────────────────────────────────────────────────
    print(workforce.describe())
    print()

    if graph is not None:
        from langchain_core.messages import HumanMessage
        result = await workforce.ainvoke(
            {"messages": [HumanMessage(content="What is WIRE?")]}
        )
        print("\nResult:", result)

    # ── 5. Verify the audit chain ────────────────────────────────────────────
    from pathlib import Path
    if Path("wire-audit.jsonl").exists():
        count = wire.AuditChain.verify("wire-audit.jsonl")
        print(f"\n✓ Audit chain intact — {count} entries")
        print("  Run: wire audit wire-audit.jsonl")
        print("  Run: wire replay --run-id <run_id>")


if __name__ == "__main__":
    asyncio.run(main())
