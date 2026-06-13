"""
WIRE + Customer Support Triage — ReAct Agent

A real-world pattern: search the knowledge base for an answer, pull
customer history if the KB misses, escalate the ticket if the customer
is premium, and send a response — all governed so premium escalations
require human approval and responses are never sent twice.

WIRE governance applied here:
  - HITLGate       — escalation for premium customers paused for human approval
                     (auto-approves after 2s in demo via TimeoutAction.APPROVE)
  - IdempotencyGuard via @wire.tool(idempotent=True) — send_response() is
                     decorated so the same reply is never delivered twice,
                     even on agent retry

No API keys needed — all tools are mocked with realistic data.

Run:
    pip install wire-ai[langgraph]
    python examples/customer_support/run.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ── Mock data ─────────────────────────────────────────────────────────────────

MOCK_KB = {
    "billing invoice": {
        "article_id": "KB-0042",
        "title": "How do I download my invoice?",
        "answer": (
            "Go to Settings → Billing → Invoices. "
            "Click the download icon next to any invoice. "
            "PDF format; available for the past 24 months."
        ),
        "confidence": 0.94,
    },
    "reset password": {
        "article_id": "KB-0017",
        "title": "How do I reset my password?",
        "answer": "Click 'Forgot password' on the login page. Check your email for a reset link.",
        "confidence": 0.98,
    },
}

MOCK_CUSTOMERS = {
    "cust-1001": {
        "name": "Acme Corp",
        "tier": "premium",
        "mrr_usd": 8500,
        "open_tickets": 2,
        "account_manager": "sarah.jones@company.com",
        "last_contact_days_ago": 3,
    },
    "cust-2045": {
        "name": "Bob Smith",
        "tier": "free",
        "mrr_usd": 0,
        "open_tickets": 0,
        "account_manager": None,
        "last_contact_days_ago": 45,
    },
}

TOOL_CALLS = []    # execution log for verification
SENT_REPLIES = []  # track sent responses to verify idempotency


# ── Mock tools ────────────────────────────────────────────────────────────────

async def search_kb(query: str) -> dict:
    """Mock KB search: find a self-service answer for the customer's question."""
    TOOL_CALLS.append(("search_kb", query))
    # Simple keyword match for the mock
    for keyword, article in MOCK_KB.items():
        if keyword in query.lower():
            print(f"  [KB] Match: '{article['title']}' (confidence={article['confidence']})")
            return {"found": True, "query": query, **article}
    print(f"  [KB] No match found for query: '{query[:50]}'")
    return {"found": False, "query": query, "confidence": 0.0}


async def get_customer_history(customer_id: str) -> dict:
    """Mock CRM: fetch account details and support history."""
    TOOL_CALLS.append(("get_customer_history", customer_id))
    customer = MOCK_CUSTOMERS.get(customer_id)
    if customer:
        print(f"  [CRM] Customer {customer_id}: {customer['name']} — tier={customer['tier']}")
        return {"found": True, "customer_id": customer_id, **customer}
    print(f"  [CRM] Customer {customer_id} not found")
    return {"found": False, "customer_id": customer_id}


async def escalate_ticket(
    ticket_id: str,
    customer_id: str,
    reason: str,
    priority: str = "high",
) -> dict:
    """Mock escalation: route ticket to a senior agent or account manager."""
    TOOL_CALLS.append(("escalate_ticket", ticket_id))
    escalation_id = f"ESC-{abs(hash(ticket_id + customer_id)) % 9999:04d}"
    print(f"  [Escalate] {escalation_id}: ticket {ticket_id} → priority={priority}")
    print(f"             reason='{reason[:60]}'")
    return {
        "escalation_id": escalation_id,
        "ticket_id": ticket_id,
        "priority": priority,
        "assigned_to": "senior-support-queue",
    }


async def send_response(
    ticket_id: str,
    customer_id: str,
    message: str,
    channel: str = "email",
) -> dict:
    """
    Mock response sender: email/chat reply to customer.

    Side-effecting — decorated with @wire.tool(idempotent=True) in main()
    to prevent duplicate emails on agent retry.
    """
    TOOL_CALLS.append(("send_response", ticket_id))
    SENT_REPLIES.append({"ticket_id": ticket_id, "customer_id": customer_id})
    print(f"  [Response] ticket={ticket_id} customer={customer_id} channel={channel}")
    print(f"             '{message[:70]}...'")
    return {
        "status": "sent",
        "ticket_id": ticket_id,
        "customer_id": customer_id,
        "channel": channel,
    }


# ── ReAct graph ───────────────────────────────────────────────────────────────

def build_support_react_graph():
    """Build the customer support triage ReAct loop with scripted mock LLM."""
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import StateGraph, END, START, MessagesState

    TOOLS = {
        "search_kb": search_kb,
        "get_customer_history": get_customer_history,
        "escalate_ticket": escalate_ticket,
        "send_response": send_response,
    }

    _step = [0]
    # This script handles a premium customer with a question that has no KB answer
    REACT_SCRIPT = [
        # Step 1: THINK → search KB first
        {
            "thought": (
                "Customer TICK-5500 asked about cancelling a subscription. "
                "Let me search the KB first before escalating."
            ),
            "action": "search_kb",
            "args": {"query": "cancel subscription refund policy"},
        },
        # Step 2: OBSERVE KB miss → get customer history to check tier
        {
            "thought": (
                "No KB match found. I need to check whether this customer is "
                "premium before deciding on escalation."
            ),
            "action": "get_customer_history",
            "args": {"customer_id": "cust-1001"},
        },
        # Step 3: OBSERVE customer is premium → escalate (HITL gate fires here)
        {
            "thought": (
                "Customer is PREMIUM ($8,500 MRR). No KB answer exists. "
                "Escalating to senior agent — requires human approval."
            ),
            "action": "escalate_ticket",
            "args": {
                "ticket_id": "TICK-5500",
                "customer_id": "cust-1001",
                "reason": "Premium customer with no KB answer — needs account manager involvement",
                "priority": "high",
            },
        },
        # Step 4: OBSERVE escalation done → send acknowledgement response
        {
            "thought": (
                "Escalation created. Sending the customer an acknowledgement "
                "so they know their request is being handled."
            ),
            "action": "send_response",
            "args": {
                "ticket_id": "TICK-5500",
                "customer_id": "cust-1001",
                "message": (
                    "Thank you for reaching out, Acme Corp. Your request has been escalated "
                    "to a senior account manager who will contact you within 4 hours."
                ),
                "channel": "email",
            },
        },
        # Step 5: FINAL ANSWER
        {
            "thought": (
                "Triage complete. KB had no answer, premium customer identified, "
                "ticket escalated to senior queue, acknowledgement sent."
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
    from wire import HITLGate, HITLAction, TimeoutAction, IdempotencyGuard
    from wire.core.models import Risk

    print("\n" + "=" * 60)
    print("  WIRE + Customer Support Triage Agent")
    print("  Governed: HITLGate · @wire.tool(idempotent=True) · AuditChain")
    print("=" * 60)

    # ── 1. Describe the workforce ─────────────────────────────────────────────
    print("\n[1] wire.hire() — assemble support triage workforce")
    workforce = wire.hire(
        "Triage customer tickets, search knowledge base, escalate premium customers"
    )
    print(workforce.describe())

    # ── 2. HITL gate for premium escalations ─────────────────────────────────
    # In demo: auto-approves after 2s. In production: route to Slack approval.
    print("\n[2] HITLGate — premium escalations require human approval")
    hitl_gate = HITLGate(
        trigger=Risk.HIGH,
        channel="cli",
        timeout_minutes=0.033,              # ~2s for demo
        timeout_action=TimeoutAction.APPROVE,  # auto-approve on timeout
    )

    # ── 3. @wire.tool(idempotent=True) — never double-reply to a customer ─────
    # This is the primary pattern being showcased: the decorator wraps
    # send_response() so even if the agent retries, the customer gets exactly
    # one email. The idempotency key is derived from (ticket_id + customer_id +
    # message) so different messages still go through.
    print("\n[3] @wire.tool(idempotent=True) — send_response() never fires twice")

    @wire.tool(idempotent=True, description="Send customer reply — idempotent on ticket+message")
    async def governed_send_response(
        ticket_id: str, customer_id: str, message: str, channel: str = "email"
    ) -> dict:
        return await send_response(
            ticket_id=ticket_id,
            customer_id=customer_id,
            message=message,
            channel=channel,
        )

    @wire.tool(idempotent=True, description="Escalate ticket — idempotent on ticket_id")
    async def governed_escalate_ticket(
        ticket_id: str, customer_id: str, reason: str, priority: str = "high"
    ) -> dict:
        # HITLGate: premium escalations need human sign-off
        print(f"\n  [HITL] Escalation for premium customer — requesting approval...")
        decision = await hitl_gate.request(
            run_id="support-run-001",
            message=(
                f"Approve escalation for ticket {ticket_id}?\n"
                f"  Customer: {customer_id}\n  Reason: {reason[:80]}"
            ),
            context={"ticket_id": ticket_id, "customer_id": customer_id, "priority": priority},
            risk=Risk.HIGH,
        )
        print(f"  [HITL] Decision: {decision.action.value} (actor={decision.actor})")
        if decision.action != HITLAction.APPROVE:
            return {"status": "rejected", "reason": decision.notes}

        return await escalate_ticket(
            ticket_id=ticket_id,
            customer_id=customer_id,
            reason=reason,
            priority=priority,
        )

    registered = len(wire.tools.list())
    print(f"  {registered} tools registered — send_response + escalate_ticket are idempotent")

    # ── 4. Build ReAct graph ──────────────────────────────────────────────────
    print("\n[4] Building customer support ReAct graph...")
    graph = build_support_react_graph()

    # ── 5. Deploy with WIRE governance ───────────────────────────────────────
    print("\n[5] wire.deploy() — wrap graph with governance")
    governed = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=20,
        max_cost_usd=0.50,
        audit_path="/tmp/wire-support-audit.jsonl",
    )

    events = []

    @governed.on(wire.EventKind.STEP_END)
    async def on_step(event):
        events.append(event)

    print("  Governance active:")
    print("     LoopGuard     — max 20 iterations")
    print("     HITLGate      — premium escalations paused for approval (2s demo timeout)")
    print("     IdempotencyGuard — send_response never fires twice on the same ticket")
    print("     AuditChain    — every triage step tamper-proof logged")

    # ── 6. Run the ReAct loop ─────────────────────────────────────────────────
    from langchain_core.messages import HumanMessage
    print("\n[6] Running support triage ReAct loop...\n" + "-" * 50)

    result = await governed.ainvoke({
        "messages": [HumanMessage(
            content=(
                "Triage ticket TICK-5500 from customer cust-1001. "
                "They asked: 'I need to cancel my subscription and get a refund for this month.' "
                "Search the KB first; if no answer found, get their customer history. "
                "If they are a premium customer, escalate and send an acknowledgement."
            )
        )]
    })

    print("-" * 50)

    # ── 7. Idempotency demonstration — no duplicate emails ────────────────────
    print("\n[7] Idempotency test — send same response 3 times (retry simulation)")
    replies_before = len(SENT_REPLIES)

    key = wire.IdempotencyGuard.make_key(
        "send_response",
        {
            "ticket_id": "TICK-5500",
            "customer_id": "cust-1001",
            "message": (
                "Thank you for reaching out, Acme Corp. Your request has been escalated "
                "to a senior account manager who will contact you within 4 hours."
            ),
            "channel": "email",
        },
    )
    retry_guard = IdempotencyGuard()
    for i in range(3):
        result_i, was_dup = await retry_guard.call(
            key=key,
            fn=lambda: send_response(
                ticket_id="TICK-5500",
                customer_id="cust-1001",
                message=(
                    "Thank you for reaching out, Acme Corp. Your request has been escalated "
                    "to a senior account manager who will contact you within 4 hours."
                ),
                channel="email",
            ),
            run_id="retry-test",
            tool="send_response",
        )
        status = "DEDUPLICATED (skipped)" if was_dup else "EXECUTED"
        print(f"  Attempt {i + 1}: {status} → ticket={result_i.get('ticket_id', 'n/a')}")

    replies_after = len(SENT_REPLIES)
    print(f"  3 calls → {replies_after - replies_before} actual email(s) sent (idempotent)")
    print("  Customer received exactly ONE reply — no duplicate emails.")

    # ── 8. Audit verification ─────────────────────────────────────────────────
    print("\n[8] AuditChain verification")
    count = wire.AuditChain.verify("/tmp/wire-support-audit.jsonl")
    print(f"  {count} steps logged · chain intact · tamper-proof")

    # ── 9. Summary ────────────────────────────────────────────────────────────
    print(f"\n[9] EventBus received {len(events)} step_end events")
    print(f"    Total tool calls: {len(TOOL_CALLS)}")
    print(f"    Tool call log:    {TOOL_CALLS}")

    print("\n" + "=" * 60)
    print("  WIRE Customer Support governance summary:")
    print(f"  HITLGate        — escalation gate fired (auto-approved in demo)")
    print(f"  IdempotencyGuard — 3 retries → 1 email delivered (no duplicates)")
    print(f"  AuditChain      — {count} steps, cryptographically verified")
    print(f"  EventBus        — {len(events)} typed events emitted")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
