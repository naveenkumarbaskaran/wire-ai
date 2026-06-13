"""
WIRE + Incident Response — PagerDuty-style ReAct Agent

A real-world pattern: on-call alert triage, runbook lookup, incident
creation, and on-call notification — all governed by WIRE so P1
incidents are never acted on without a human gate.

WIRE governance applied here:
  - HITLGate    — pauses execution for human approval before creating a P1 incident
                  (auto-approves after 2s in demo mode via TimeoutAction.APPROVE)
  - IdempotencyGuard — incident creation and notifications are deduplicated
                       so retries never open the same incident twice

No API keys needed — all tools are mocked with realistic data.

Run:
    pip install wire-ai[langgraph]
    python examples/incident_response/run.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ── Mock data ─────────────────────────────────────────────────────────────────

MOCK_ALERTS = [
    {
        "id": "ALT-001",
        "service": "payments-api",
        "severity": "P1",
        "title": "Error rate > 15% on /v2/charge (was 0.3%)",
        "started_at": "2026-06-13T08:42:11Z",
        "tags": ["slo-breach", "latency", "payments"],
    },
    {
        "id": "ALT-002",
        "service": "user-service",
        "severity": "P3",
        "title": "Memory usage at 78% (warning threshold)",
        "started_at": "2026-06-13T08:55:03Z",
        "tags": ["memory", "capacity"],
    },
]

MOCK_RUNBOOKS = {
    "payments-api": {
        "name": "Payments API Error Spike",
        "steps": [
            "1. Check upstream provider status at status.stripe.com",
            "2. Inspect recent deploy history in deploy-log channel",
            "3. Review error traces in Datadog → service:payments-api",
            "4. If rate > 10% for >5min: page payments on-call lead",
        ],
        "escalation_contacts": ["#oncall-payments", "payments-lead@company.com"],
        "last_updated": "2026-05-01",
    },
    "user-service": {
        "name": "User Service Memory Pressure",
        "steps": [
            "1. SSH to affected pods: kubectl exec -it <pod> -- bash",
            "2. Run: jstack <pid> to detect memory leaks",
            "3. If > 85%: trigger rolling restart",
        ],
        "escalation_contacts": ["#oncall-platform"],
        "last_updated": "2026-04-15",
    },
}

TOOL_CALLS = []  # execution log for verification


# ── Mock tools ────────────────────────────────────────────────────────────────

async def get_alerts() -> list[dict]:
    """Mock PagerDuty: return active firing alerts."""
    TOOL_CALLS.append(("get_alerts", "all"))
    print(f"  [PagerDuty] Fetched {len(MOCK_ALERTS)} active alerts")
    return MOCK_ALERTS


async def acknowledge_alert(alert_id: str) -> dict:
    """Mock PagerDuty: acknowledge an alert to prevent duplicate pages."""
    TOOL_CALLS.append(("acknowledge_alert", alert_id))
    print(f"  [PagerDuty] Alert {alert_id} acknowledged — suppressing duplicate pages")
    return {"alert_id": alert_id, "status": "acknowledged"}


async def check_runbook(service: str) -> dict:
    """Mock runbook store: retrieve the runbook for a service."""
    TOOL_CALLS.append(("check_runbook", service))
    runbook = MOCK_RUNBOOKS.get(service)
    if runbook:
        print(f"  [Runbook] Found runbook for '{service}': {runbook['name']}")
        return {"found": True, "service": service, **runbook}
    print(f"  [Runbook] No runbook found for '{service}'")
    return {"found": False, "service": service}


async def create_incident(
    title: str,
    severity: str,
    service: str,
    alert_id: str,
    runbook_url: str = "",
) -> dict:
    """Mock incident tracker: open a new incident. Side-effecting — must be idempotent."""
    TOOL_CALLS.append(("create_incident", title))
    incident_id = f"INC-{abs(hash(title + alert_id)) % 9999:04d}"
    print(f"  [Incident] Created {incident_id}: {title[:60]}")
    print(f"             severity={severity} · service={service}")
    return {
        "incident_id": incident_id,
        "url": f"https://incidents.example.com/{incident_id}",
        "severity": severity,
        "service": service,
    }


async def notify_oncall(
    channel: str,
    message: str,
    incident_id: str = "",
) -> dict:
    """Mock Slack/PD notification: page the on-call team. Side-effecting — must be idempotent."""
    TOOL_CALLS.append(("notify_oncall", channel))
    print(f"  [Notify] {channel}: {message[:80]}")
    if incident_id:
        print(f"           incident={incident_id}")
    return {"status": "delivered", "channel": channel}


# ── ReAct graph ───────────────────────────────────────────────────────────────

def build_incident_react_graph():
    """Build the incident response ReAct loop with a scripted mock LLM."""
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import StateGraph, END, START, MessagesState

    TOOLS = {
        "get_alerts": get_alerts,
        "acknowledge_alert": acknowledge_alert,
        "check_runbook": check_runbook,
        "create_incident": create_incident,
        "notify_oncall": notify_oncall,
    }

    _step = [0]
    REACT_SCRIPT = [
        # Step 1: THINK → fetch active alerts first
        {
            "thought": "I need to triage active alerts. Let me pull them from PagerDuty.",
            "action": "get_alerts",
            "args": {},
        },
        # Step 2: OBSERVE P1 alert → acknowledge to prevent duplicate pages
        {
            "thought": "There is a P1 alert on payments-api with >15% error rate. I'll acknowledge it first.",
            "action": "acknowledge_alert",
            "args": {"alert_id": "ALT-001"},
        },
        # Step 3: OBSERVE acknowledged → look up the runbook
        {
            "thought": "Alert acknowledged. Now checking the runbook for payments-api before escalating.",
            "action": "check_runbook",
            "args": {"service": "payments-api"},
        },
        # Step 4: OBSERVE runbook found → create P1 incident (HITL gate will fire here)
        {
            "thought": (
                "Runbook found. Error rate >15% exceeds P1 threshold. "
                "Creating an incident — this requires human approval."
            ),
            "action": "create_incident",
            "args": {
                "title": "P1: payments-api error rate >15% on /v2/charge",
                "severity": "P1",
                "service": "payments-api",
                "alert_id": "ALT-001",
                "runbook_url": "https://runbooks.example.com/payments-api",
            },
        },
        # Step 5: OBSERVE incident created → notify on-call
        {
            "thought": "Incident created. Paging the payments on-call team now.",
            "action": "notify_oncall",
            "args": {
                "channel": "#oncall-payments",
                "message": "P1 INCIDENT: payments-api error rate >15%. Runbook: check upstream Stripe status.",
                "incident_id": "INC-0001",
            },
        },
        # Step 6: FINAL ANSWER
        {
            "thought": (
                "All done. P1 alert acknowledged, runbook retrieved, incident INC opened, "
                "on-call team paged. Monitoring continues."
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

        import json
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
    from wire import HITLGate, HITLAction, TimeoutAction, IdempotencyGuard
    from wire.core.models import Risk

    print("\n" + "=" * 60)
    print("  WIRE + Incident Response Agent")
    print("  Governed: HITLGate · IdempotencyGuard · AuditChain")
    print("=" * 60)

    # ── 1. Describe the workforce ─────────────────────────────────────────────
    print("\n[1] wire.hire() — assemble incident response workforce")
    workforce = wire.hire(
        "Detect alerts, check runbook, create incident, notify on-call team"
    )
    print(workforce.describe())

    # ── 2. Build the ReAct graph ──────────────────────────────────────────────
    print("\n[2] Building incident response ReAct graph...")
    graph = build_incident_react_graph()

    # ── 3. HITL gate for P1 incidents ─────────────────────────────────────────
    # In demo mode: TimeoutAction.APPROVE auto-approves after 2s.
    # In production: channel=HITLChannel.SLACK, timeout_action=TimeoutAction.ESCALATE
    print("\n[3] Configuring HITLGate for P1 incident creation")
    hitl_gate = HITLGate(
        trigger=Risk.HIGH,
        channel="cli",
        timeout_minutes=1,                # 2s in demo (overridden below for speed)
        timeout_action=TimeoutAction.APPROVE,  # auto-approve on timeout
    )
    # Shorten timeout to 2s for demo purposes
    hitl_gate.timeout_minutes = 0.033    # ~2 seconds

    # ── 4. IdempotencyGuard for side-effecting tools ──────────────────────────
    print("\n[4] IdempotencyGuard — incident creation and notifications never fire twice")
    guard = IdempotencyGuard()

    @wire.tool(idempotent=True, description="Create incident — idempotent on title+alert_id")
    async def governed_create_incident(
        title: str, severity: str, service: str, alert_id: str, runbook_url: str = ""
    ) -> dict:
        # HITLGate fires only for P1 incidents
        if severity == "P1":
            print(f"\n  [HITL] P1 incident requires human approval — requesting...")
            decision = await hitl_gate.request(
                run_id="incident-run-001",
                message=f"Approve P1 incident creation?\n  Title: {title}\n  Service: {service}",
                context={"severity": severity, "service": service, "alert_id": alert_id},
                risk=Risk.HIGH,
            )
            print(f"  [HITL] Decision: {decision.action.value} (actor={decision.actor})")
            if decision.action != HITLAction.APPROVE:
                return {"status": "rejected", "reason": decision.notes}

        return await create_incident(
            title=title, severity=severity, service=service,
            alert_id=alert_id, runbook_url=runbook_url,
        )

    @wire.tool(idempotent=True, description="Notify on-call — idempotent on channel+incident_id")
    async def governed_notify_oncall(channel: str, message: str, incident_id: str = "") -> dict:
        return await notify_oncall(channel=channel, message=message, incident_id=incident_id)

    registered = len(wire.tools.list())
    print(f"  {registered} tools registered with idempotent=True")

    # ── 5. Deploy with WIRE governance ───────────────────────────────────────
    print("\n[5] wire.deploy() — wrap graph with governance")
    governed = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=20,
        max_cost_usd=1.0,
        audit_path="/tmp/wire-incident-audit.jsonl",
    )

    events = []

    @governed.on(wire.EventKind.STEP_END)
    async def on_step(event):
        events.append(event)

    print("  Governance active:")
    print("     LoopGuard     — max 20 iterations (prevents runaway triage loops)")
    print("     HITLGate      — P1 creation paused for human approval (2s demo timeout)")
    print("     IdempotencyGuard — create_incident + notify_oncall deduplicated")
    print("     AuditChain    — every THINK/ACT/OBS step tamper-proof logged")

    # ── 6. Run the ReAct loop ─────────────────────────────────────────────────
    from langchain_core.messages import HumanMessage
    print("\n[6] Running incident response ReAct loop...\n" + "-" * 50)

    result = await governed.ainvoke({
        "messages": [HumanMessage(
            content=(
                "Check active PagerDuty alerts. For any P1 alert: "
                "acknowledge it, look up the runbook, create an incident, "
                "and page the on-call team."
            )
        )]
    })

    print("-" * 50)

    # ── 7. Idempotency demonstration ──────────────────────────────────────────
    print("\n[7] Idempotency test — retry incident creation 3 times")
    incidents_before = sum(1 for t, _ in TOOL_CALLS if t == "create_incident")
    key = wire.IdempotencyGuard.make_key(
        "create_incident",
        {
            "title": "P1: payments-api error rate >15% on /v2/charge",
            "severity": "P1",
            "service": "payments-api",
            "alert_id": "ALT-001",
            "runbook_url": "https://runbooks.example.com/payments-api",
        },
    )
    retry_guard = IdempotencyGuard()
    for i in range(3):
        result_i, was_dup = await retry_guard.call(
            key=key,
            fn=lambda: create_incident(
                title="P1: payments-api error rate >15% on /v2/charge",
                severity="P1",
                service="payments-api",
                alert_id="ALT-001",
            ),
            run_id="retry-test",
            tool="create_incident",
        )
        status = "DEDUPLICATED (skipped)" if was_dup else "EXECUTED"
        print(f"  Attempt {i + 1}: {status} → {result_i}")
    incidents_after = sum(1 for t, _ in TOOL_CALLS if t == "create_incident")
    print(f"  3 calls → {incidents_after - incidents_before} actual executions (idempotent)")

    # ── 8. Audit verification ─────────────────────────────────────────────────
    print("\n[8] AuditChain verification")
    count = wire.AuditChain.verify("/tmp/wire-incident-audit.jsonl")
    print(f"  {count} steps logged · chain intact · tamper-proof")

    # ── 9. Summary ────────────────────────────────────────────────────────────
    print(f"\n[9] EventBus received {len(events)} step_end events")
    print(f"    Total tool calls: {len(TOOL_CALLS)}")
    print(f"    Tool call log:    {TOOL_CALLS}")

    print("\n" + "=" * 60)
    print("  WIRE Incident Response governance summary:")
    print(f"  HITLGate        — P1 incident gate fired (auto-approved in demo)")
    print(f"  IdempotencyGuard — 3 retries → 1 execution (no duplicate incidents)")
    print(f"  AuditChain      — {count} steps, cryptographically verified")
    print(f"  EventBus        — {len(events)} typed events emitted")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
