"""
tools.py — Mock async tools used by the AWS Cost Governance agent.

In a real deployment these would hit Jira REST API, Slack Web API, and
AWS Cost Explorer. Here they print rich output and return realistic
response shapes so the governance layer (WIRE) can be demonstrated
without credentials.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta

from mock_aws import get_cost_data


# ── Jira ─────────────────────────────────────────────────────────────────────

async def create_jira_ticket(
    title: str,
    priority: str,
    cost_usd: float,
) -> dict:
    """
    Mock Jira ticket creation.

    Simulates a ~100ms network round-trip and returns a ticket stub.
    Priority should be one of: Highest, High, Medium, Low.

    Returns:
        {
            "ticket_id": "COST-1042",
            "url": "https://jira.example.com/browse/COST-1042",
            "title": "...",
            "priority": "High",
            "cost_usd": 847.30,
            "status": "Open",
        }
    """
    import os
    silent = os.environ.get("WIRE_DEMO_SILENT") == "1"

    await asyncio.sleep(0.1)   # simulate network latency

    ticket_num = random.randint(1000, 9999)
    ticket_id  = f"COST-{ticket_num}"
    url        = f"https://jira.example.com/browse/{ticket_id}"

    if not silent:
        print(f"  [Jira]  Created {ticket_id}: {title!r}  priority={priority}  cost=${cost_usd:.2f}")

    return {
        "ticket_id": ticket_id,
        "url": url,
        "title": title,
        "priority": priority,
        "cost_usd": cost_usd,
        "status": "Open",
    }


# ── Slack ─────────────────────────────────────────────────────────────────────

async def send_slack_notification(
    channel: str,
    message: str,
) -> dict:
    """
    Mock Slack message delivery.

    Returns:
        {
            "status": "sent",
            "channel": "#ops-alerts",
            "ts": "1716912345.123456",
        }
    """
    import os, time
    silent = os.environ.get("WIRE_DEMO_SILENT") == "1"
    ts = str(round(time.time(), 6))

    if not silent:
        print(f"  [Slack] #{channel}: {message}")

    return {
        "status": "sent",
        "channel": f"#{channel}",
        "ts": ts,
    }


# ── AWS Cost Explorer ─────────────────────────────────────────────────────────

async def get_aws_costs(days: int = 7) -> dict:
    """
    Aggregate AWS cost data for the past N days using mock data.

    Returns:
        {
            "days": 7,
            "daily": [{"date": "...", "total_usd": ..., "services": {...}}, ...],
            "totals_by_service": {"EC2": 2134.50, ...},
            "grand_total_usd": 4821.10,
            "anomalies": [{"date": "...", "service": "...", "cost_usd": ..., "multiplier": ...}],
        }
    """
    today = date.today()
    daily_records = []
    totals_by_service: dict[str, float] = {}
    anomalies = []

    for offset in range(days - 1, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        record = get_cost_data(d)
        daily_records.append({
            "date":      record["date"],
            "total_usd": record["total_usd"],
            "services":  record["services"],
        })

        for svc, cost in record["services"].items():
            totals_by_service[svc] = round(
                totals_by_service.get(svc, 0.0) + cost, 2
            )

        if record["has_anomaly"]:
            anomalies.append({
                "date":        record["date"],
                "service":     record["anomaly_service"],
                "cost_usd":    record["services"][record["anomaly_service"]],
                "multiplier":  record["anomaly_multiplier"],
            })

    grand_total = round(sum(r["total_usd"] for r in daily_records), 2)

    return {
        "days":                days,
        "daily":               daily_records,
        "totals_by_service":   totals_by_service,
        "grand_total_usd":     grand_total,
        "anomalies":           anomalies,
    }
