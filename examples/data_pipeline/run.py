"""
WIRE + Data Pipeline QA — Data Quality Monitoring Agent

A real-world pattern: run automated data quality checks on pipeline
output, quarantine bad records if the failure rate exceeds a threshold,
and send a compliance report — all tracked by WIRE governance so SLA
violations are surfaced immediately and every action is audited.

WIRE governance applied here:
  - SLATracker    — enforces a 60s response time ceiling; measures elapsed
                    time via the `async with tracker.measure()` context manager
  - AuditChain    — every QC check, quarantine action, and report is logged
                    in a tamper-proof chain
  - Budget($1)    — run cost is capped at $1 per pipeline execution

No API keys needed — all tools are mocked with realistic data.

Run:
    pip install wire-ai[langgraph]
    python examples/data_pipeline/run.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ── Mock data ─────────────────────────────────────────────────────────────────

MOCK_PIPELINE_STATUS = {
    "pipeline_id": "orders-etl-prod",
    "last_run": "2026-06-13T07:00:00Z",
    "rows_processed": 124_500,
    "status": "completed_with_warnings",
    "duration_seconds": 342,
}

# Simulates DQ check results: each rule has a pass/fail ratio
MOCK_DQ_RESULTS = {
    "orders-etl-prod": [
        {
            "rule": "not_null:order_id",
            "passed": 124_450,
            "failed": 50,
            "failure_rate": 0.0004,
            "severity": "critical",
        },
        {
            "rule": "not_null:customer_email",
            "passed": 123_300,
            "failed": 1200,
            "failure_rate": 0.0096,
            "severity": "high",
        },
        {
            "rule": "range:order_amount",
            "passed": 120_000,
            "failed": 4500,
            "failure_rate": 0.0361,  # 3.6% — exceeds the 2% quarantine threshold
            "severity": "medium",
        },
        {
            "rule": "referential:product_id",
            "passed": 124_490,
            "failed": 10,
            "failure_rate": 0.00008,
            "severity": "low",
        },
    ]
}

QUARANTINE_THRESHOLD = 0.02   # quarantine if any rule's failure rate > 2%
TOOL_CALLS = []               # execution log for verification


# ── Mock tools ────────────────────────────────────────────────────────────────

async def run_data_quality_check(pipeline_id: str) -> dict:
    """Mock GE / Soda: run data quality rules against the pipeline output."""
    TOOL_CALLS.append(("run_data_quality_check", pipeline_id))
    results = MOCK_DQ_RESULTS.get(pipeline_id, [])
    failures = [r for r in results if r["failure_rate"] > QUARANTINE_THRESHOLD]
    print(f"  [DQ] Pipeline '{pipeline_id}': {len(results)} rules checked")
    print(f"       {len(failures)} rule(s) exceeded {QUARANTINE_THRESHOLD*100:.0f}% failure threshold")
    return {
        "pipeline_id": pipeline_id,
        "rules_checked": len(results),
        "results": results,
        "failures_above_threshold": failures,
        "needs_quarantine": len(failures) > 0,
    }


async def get_pipeline_status(pipeline_id: str) -> dict:
    """Mock pipeline orchestrator: fetch the latest run status."""
    TOOL_CALLS.append(("get_pipeline_status", pipeline_id))
    status = MOCK_PIPELINE_STATUS.copy()
    status["pipeline_id"] = pipeline_id
    print(f"  [Pipeline] {pipeline_id}: {status['status']} — {status['rows_processed']:,} rows")
    return status


async def quarantine_bad_data(
    pipeline_id: str,
    rule: str,
    failed_rows: int,
    reason: str,
) -> dict:
    """Mock quarantine store: move failed records to a quarantine partition."""
    TOOL_CALLS.append(("quarantine_bad_data", f"{pipeline_id}:{rule}"))
    quarantine_id = f"QUA-{abs(hash(pipeline_id + rule)) % 9999:04d}"
    print(f"  [Quarantine] {quarantine_id}: {failed_rows:,} rows quarantined")
    print(f"               rule='{rule}' · reason='{reason[:60]}'")
    return {
        "quarantine_id": quarantine_id,
        "pipeline_id": pipeline_id,
        "rule": rule,
        "rows_quarantined": failed_rows,
        "status": "quarantined",
    }


async def send_report(
    pipeline_id: str,
    summary: str,
    quarantined_rules: list,
    total_rows: int,
) -> dict:
    """Mock report sender: email/Slack DQ report to data team."""
    TOOL_CALLS.append(("send_report", pipeline_id))
    print(f"  [Report] Sending DQ report for '{pipeline_id}'")
    print(f"           {summary[:80]}")
    print(f"           Quarantined rules: {len(quarantined_rules)} · Total rows: {total_rows:,}")
    return {
        "status": "sent",
        "recipients": ["data-team@company.com", "#data-quality-alerts"],
        "pipeline_id": pipeline_id,
    }


# ── ReAct graph ───────────────────────────────────────────────────────────────

def build_dq_react_graph():
    """Build the data quality ReAct loop with a scripted mock LLM."""
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import StateGraph, END, START, MessagesState

    TOOLS = {
        "run_data_quality_check": run_data_quality_check,
        "get_pipeline_status": get_pipeline_status,
        "quarantine_bad_data": quarantine_bad_data,
        "send_report": send_report,
    }

    _step = [0]
    REACT_SCRIPT = [
        # Step 1: THINK → check pipeline status first
        {
            "thought": "Let me start by checking the pipeline's last run status.",
            "action": "get_pipeline_status",
            "args": {"pipeline_id": "orders-etl-prod"},
        },
        # Step 2: OBSERVE status completed_with_warnings → run DQ checks
        {
            "thought": "Pipeline completed with warnings. Running data quality checks now.",
            "action": "run_data_quality_check",
            "args": {"pipeline_id": "orders-etl-prod"},
        },
        # Step 3: OBSERVE DQ failures → quarantine bad data for range:order_amount
        {
            "thought": (
                "Rule 'range:order_amount' has 3.6% failure rate — above 2% threshold. "
                "Quarantining those 4,500 rows."
            ),
            "action": "quarantine_bad_data",
            "args": {
                "pipeline_id": "orders-etl-prod",
                "rule": "range:order_amount",
                "failed_rows": 4500,
                "reason": "Order amount outside expected range [0, 100000] — likely upstream data corruption",
            },
        },
        # Step 4: OBSERVE quarantine done → send report
        {
            "thought": "Quarantine complete. Sending the DQ report to the data team.",
            "action": "send_report",
            "args": {
                "pipeline_id": "orders-etl-prod",
                "summary": (
                    "DQ run: 4 rules checked. 1 critical rule breached threshold "
                    "(range:order_amount at 3.6%). 4,500 rows quarantined."
                ),
                "quarantined_rules": ["range:order_amount"],
                "total_rows": 124500,
            },
        },
        # Step 5: FINAL ANSWER
        {
            "thought": (
                "All done. DQ checks complete, 4,500 bad rows quarantined, "
                "report sent to data team."
            ),
            "action": None,
            "args": {},
        },
    ]

    def agent_node(state: MessagesState) -> dict:
        step = REACT_SCRIPT[min(_step[0], len(REACT_SCRIPT) - 1)]
        _step[0] += 1
        print(f"\n  THINK: {step['thought']}")

        if step["action"] is None:
            return {"messages": [AIMessage(content=f"FINAL ANSWER: {step['thought']}")]}

        tool_call = {
            "id": f"call_{_step[0]}",
            "name": step["action"],
            "args": step["args"],
        }
        return {"messages": [AIMessage(
            content=f"Reasoning: {step['thought']}",
            tool_calls=[tool_call],
        )]}

    async def tool_node(state: MessagesState) -> dict:
        last = state["messages"][-1]
        results = []
        for tc in getattr(last, "tool_calls", []):
            fn = TOOLS.get(tc["name"])
            print(f"  ACT:   {tc['name']}({tc['args']})")
            result = await fn(**tc["args"]) if fn else {"error": f"unknown tool: {tc['name']}"}
            print(f"  OBS:   {result}")
            results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        return {"messages": results}

    def should_continue(state: MessagesState) -> str:
        last = state["messages"][-1]
        return "tools" if (hasattr(last, "tool_calls") and last.tool_calls) else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    import wire
    from wire import SLATracker, SLABreachError

    print("\n" + "=" * 60)
    print("  WIRE + Data Pipeline QA Agent")
    print("  Governed: SLATracker · AuditChain · Budget($1)")
    print("=" * 60)

    # ── 1. Describe the workforce ─────────────────────────────────────────────
    print("\n[1] wire.hire() — assemble data quality workforce")
    workforce = wire.hire(
        "Monitor data quality, quarantine bad records, send compliance report"
    )
    print(workforce.describe())

    # ── 2. SLATracker setup ───────────────────────────────────────────────────
    print("\n[2] SLATracker — 60s response time SLA, $1 cost ceiling")
    tracker = SLATracker(
        role="data_quality_monitor",
        response_seconds=60.0,      # entire pipeline check must finish in 60s
        max_cost_usd=1.0,
        raise_on_breach=False,      # surface breach in report, don't halt demo
    )

    # ── 3. Build ReAct graph ──────────────────────────────────────────────────
    print("\n[3] Building data quality ReAct graph...")
    graph = build_dq_react_graph()

    # ── 4. Deploy with WIRE governance ───────────────────────────────────────
    print("\n[4] wire.deploy() — wrap graph with governance")
    governed = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=20,
        max_cost_usd=1.0,
        audit_path="/tmp/wire-datapipeline-audit.jsonl",
    )

    events = []

    @governed.on(wire.EventKind.STEP_END)
    async def on_step(event):
        events.append(event)

    print("  Governance active:")
    print("     SLATracker    — 60s response ceiling (time the entire run)")
    print("     Budget        — $1.00 per pipeline execution")
    print("     AuditChain    — every check + quarantine + report logged")
    print("     EventBus      — typed events on every state transition")

    # ── 5. Run inside SLATracker.measure() ───────────────────────────────────
    # This is the key pattern: async with tracker.measure() wraps the entire
    # agent invocation so wall-clock time is measured end-to-end.
    from langchain_core.messages import HumanMessage

    print("\n[5] Running DQ agent inside SLATracker.measure()...\n" + "-" * 50)

    sla_breached = False
    try:
        async with tracker.measure(run_id="dq-run-001") as t:
            result = await governed.ainvoke({
                "messages": [HumanMessage(
                    content=(
                        "Run data quality checks on the orders-etl-prod pipeline. "
                        "If any rule's failure rate exceeds 2%, quarantine the bad rows "
                        "and send a DQ report to the data team."
                    )
                )]
            })
            # Record a mock cost for demonstration (real cost from LLM tokens)
            t.record_cost(0.003)
    except SLABreachError as e:
        print(f"\n  SLA BREACH: {e}")
        sla_breached = True

    print("-" * 50)

    # ── 6. SLA measurement report ─────────────────────────────────────────────
    print("\n[6] SLA measurement results")
    if tracker.history:
        m = tracker.history[-1]
        print(f"  Elapsed:   {m.elapsed_seconds:.3f}s  (SLA limit: 60.0s)")
        print(f"  Cost:      ${m.cost_usd:.4f}  (SLA limit: $1.00)")
        print(f"  Breached:  {m.breached}")
        if m.breached:
            print(f"  Dimension: {m.breach_dimension}")
    else:
        print("  No measurements recorded.")
    print(f"  SLA breach rate (all runs): {tracker.breach_rate:.0%}")
    if not sla_breached:
        print("  SLA: PASSED — pipeline QA completed within all thresholds")

    # ── 7. Audit verification ─────────────────────────────────────────────────
    print("\n[7] AuditChain verification")
    count = wire.AuditChain.verify("/tmp/wire-datapipeline-audit.jsonl")
    print(f"  {count} steps logged · chain intact · tamper-proof")

    # ── 8. Summary ────────────────────────────────────────────────────────────
    print(f"\n[8] EventBus received {len(events)} step_end events")
    print(f"    Total tool calls: {len(TOOL_CALLS)}")
    print(f"    Tool call log:    {TOOL_CALLS}")

    print("\n" + "=" * 60)
    print("  WIRE Data Pipeline QA governance summary:")
    print(f"  SLATracker      — {tracker.history[-1].elapsed_seconds:.3f}s elapsed (60s limit)")
    print(f"  AuditChain      — {count} steps, cryptographically verified")
    print(f"  Budget          — $1.00 ceiling enforced per run")
    print(f"  EventBus        — {len(events)} typed events emitted")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
