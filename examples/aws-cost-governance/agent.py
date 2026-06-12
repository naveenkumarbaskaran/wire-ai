"""
agent.py — LangGraph cost-governance agent wrapped with WIRE governance.

Three nodes:
  cost_monitor     — fetches 7-day cost window, produces summary message
  anomaly_detector — checks per-service daily average against threshold,
                     sets state["anomaly_detected"] and state["anomaly_info"]
  action_executor  — creates Jira ticket + Slack alert when anomaly is present

No real LLM is used. A MockLLM class responds with scripted messages keyed
on keywords in the input, so the demo runs without any API credentials.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph


# ── Mock LLM ─────────────────────────────────────────────────────────────────

class MockLLM:
    """
    Scripted LLM that returns deterministic responses based on input keywords.
    Zero API calls, zero cost, zero credentials required.
    """

    def __call__(self, messages: list) -> AIMessage:
        # Flatten all message content into one string for keyword matching
        combined = " ".join(
            m.content if hasattr(m, "content") else str(m)
            for m in messages
        ).lower()

        if "analyze" in combined or "cost" in combined:
            return AIMessage(
                content=(
                    "Cost analysis complete. I have reviewed the 7-day AWS spend window. "
                    "Charges span EC2, RDS, Lambda, S3, and CloudFront. "
                    "I will now run anomaly detection to flag any services above threshold."
                )
            )

        if "anomaly" in combined or "threshold" in combined:
            return AIMessage(
                content=(
                    "Anomaly detection pass complete. "
                    "Services with daily average above $500 will be escalated. "
                    "Proceeding to action execution for confirmed anomalies."
                )
            )

        if "jira" in combined or "ticket" in combined or "action" in combined:
            return AIMessage(
                content=(
                    "Action execution complete. "
                    "Jira ticket created and Slack notification dispatched. "
                    "WIRE IdempotencyGuard ensured the ticket was created exactly once."
                )
            )

        # Default fallback
        return AIMessage(content="Step processed by WIRE cost governance agent.")

    async def ainvoke(self, messages: list) -> AIMessage:
        return self(messages)


# ── State ────────────────────────────────────────────────────────────────────

class CostState(TypedDict, total=False):
    messages:         list
    cost_data:        dict          # raw output from get_aws_costs()
    anomaly_detected: bool
    anomaly_info:     list[dict]    # list of {service, avg_daily_usd, threshold_usd}
    actions_taken:    list[dict]    # list of completed action results


# ── Nodes ─────────────────────────────────────────────────────────────────────

_llm = MockLLM()

# Anomaly threshold: flag any service whose 7-day average exceeds this
_ANOMALY_THRESHOLD_USD = 500.0


async def cost_monitor(state: CostState) -> CostState:
    """Fetch cost data and produce a human-readable summary message."""
    from tools import get_aws_costs

    cost_data = await get_aws_costs(days=7)

    summary_lines = [
        f"7-day AWS cost window ({cost_data['days']} days):",
        f"  Grand total: ${cost_data['grand_total_usd']:,.2f}",
        "",
        "  Totals by service:",
    ]
    for svc, total in sorted(cost_data["totals_by_service"].items(),
                              key=lambda x: -x[1]):
        avg = total / cost_data["days"]
        summary_lines.append(f"    {svc:<14} ${total:>8,.2f}  (avg ${avg:>6.2f}/day)")

    if cost_data["anomalies"]:
        summary_lines += ["", "  Detected anomalies in raw data:"]
        for a in cost_data["anomalies"]:
            summary_lines.append(
                f"    {a['date']}  {a['service']:<12} "
                f"${a['cost_usd']:>8.2f}  ({a['multiplier']}x spike)"
            )

    summary = "\n".join(summary_lines)

    # Ask mock LLM to interpret
    response = await _llm.ainvoke([
        HumanMessage(content=f"Analyze these AWS costs:\n{summary}")
    ])

    return {
        "cost_data": cost_data,
        "messages": state.get("messages", []) + [response],
    }


async def anomaly_detector(state: CostState) -> CostState:
    """
    Identify services whose 7-day daily average exceeds the threshold.
    Sets anomaly_detected=True and populates anomaly_info.
    """
    cost_data  = state.get("cost_data", {})
    totals     = cost_data.get("totals_by_service", {})
    days       = cost_data.get("days", 7)

    flagged = []
    for service, total in totals.items():
        avg_daily = total / days
        if avg_daily > _ANOMALY_THRESHOLD_USD:
            flagged.append({
                "service":       service,
                "avg_daily_usd": round(avg_daily, 2),
                "total_7d_usd":  round(total, 2),
                "threshold_usd": _ANOMALY_THRESHOLD_USD,
            })

    # Also include single-day spikes from raw anomaly data
    raw_anomalies = cost_data.get("anomalies", [])
    for a in raw_anomalies:
        already_flagged = any(f["service"] == a["service"] for f in flagged)
        if not already_flagged and a["cost_usd"] > _ANOMALY_THRESHOLD_USD:
            flagged.append({
                "service":       a["service"],
                "avg_daily_usd": round(a["cost_usd"], 2),   # peak day cost
                "total_7d_usd":  totals.get(a["service"], a["cost_usd"]),
                "threshold_usd": _ANOMALY_THRESHOLD_USD,
                "spike_date":    a["date"],
                "spike_multiplier": a["multiplier"],
            })

    detected = len(flagged) > 0

    response = await _llm.ainvoke([
        HumanMessage(
            content=(
                f"Anomaly threshold check: ${_ANOMALY_THRESHOLD_USD}/day. "
                f"Found {len(flagged)} service(s) above threshold."
            )
        )
    ])

    return {
        "anomaly_detected": detected,
        "anomaly_info":     flagged,
        "messages": state.get("messages", []) + [response],
    }


async def action_executor(state: CostState) -> CostState:
    """
    For each flagged anomaly: create a Jira ticket and send a Slack alert.
    Both calls are protected by IdempotencyGuard to prevent duplicates on retry.
    """
    from tools import create_jira_ticket, send_slack_notification
    import wire

    anomalies  = state.get("anomaly_info", [])
    run_id     = "cost-gov-demo-001"
    actions    = []

    # IdempotencyGuard — prevents duplicate tickets if node is retried
    guard = wire.IdempotencyGuard()

    for anomaly in anomalies:
        service    = anomaly["service"]
        cost       = anomaly["avg_daily_usd"]
        is_spike   = "spike_date" in anomaly
        priority   = "Highest" if cost > 1_000 else "High"

        title = (
            f"[AWS Cost Anomaly] {service} spike on {anomaly['spike_date']}: "
            f"${cost:,.2f}"
            if is_spike
            else f"[AWS Cost Anomaly] {service} avg ${cost:,.2f}/day exceeds threshold"
        )

        # Idempotency key — same service + cost on same run → single ticket
        jira_key = wire.IdempotencyGuard.make_key(
            "create_jira_ticket",
            {"service": service, "cost_usd": cost, "run_id": run_id},
        )
        ticket_result, was_dup = await guard.call(
            key=jira_key,
            fn=lambda t=title, p=priority, c=cost: create_jira_ticket(t, p, c),
            run_id=run_id,
            tool="create_jira_ticket",
        )

        slack_message = (
            f":rotating_light: AWS cost anomaly detected — "
            f"*{service}* ${cost:,.2f}/day "
            f"(threshold ${_ANOMALY_THRESHOLD_USD:,.0f}). "
            f"Jira: {ticket_result.get('url', 'n/a')} "
            f"{'[DEDUPLICATED]' if was_dup else ''}"
        )

        slack_key = wire.IdempotencyGuard.make_key(
            "send_slack_notification",
            {"channel": "ops-alerts", "service": service, "run_id": run_id},
        )
        slack_result, _ = await guard.call(
            key=slack_key,
            fn=lambda msg=slack_message: send_slack_notification("ops-alerts", msg),
            run_id=run_id,
            tool="send_slack_notification",
        )

        actions.append({
            "service":       service,
            "cost_usd":      cost,
            "jira":          ticket_result,
            "slack":         slack_result,
            "was_duplicate": was_dup,
        })

    response = await _llm.ainvoke([
        HumanMessage(
            content=f"Action execution: created {len(actions)} Jira ticket(s) and Slack alert(s)."
        )
    ])

    return {
        "actions_taken": actions,
        "messages": state.get("messages", []) + [response],
    }


# ── Routing ────────────────────────────────────────────────────────────────────

def route_after_detection(state: CostState) -> str:
    """Conditional edge: go to action_executor only if anomaly was found."""
    return "action_executor" if state.get("anomaly_detected") else END


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph():
    """
    Build and compile the cost governance LangGraph graph.

    Flow:
        START → cost_monitor → anomaly_detector
                                      ↓ (anomaly)        ↓ (clean)
                               action_executor           END
                                      ↓
                                     END
    """
    graph = StateGraph(CostState)

    graph.add_node("cost_monitor",    cost_monitor)
    graph.add_node("anomaly_detector", anomaly_detector)
    graph.add_node("action_executor",  action_executor)

    graph.add_edge(START,            "cost_monitor")
    graph.add_edge("cost_monitor",   "anomaly_detector")
    graph.add_conditional_edges(
        "anomaly_detector",
        route_after_detection,
        {"action_executor": "action_executor", END: END},
    )
    graph.add_edge("action_executor", END)

    return graph.compile()
