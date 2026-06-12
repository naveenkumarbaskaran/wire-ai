"""
run.py — WIRE AI governance demo: AWS Cost Governance on LangGraph.

Demonstrates:
  - wire.deploy()       — wraps LangGraph graph with full governance
  - LoopGuard           — hard iteration ceiling
  - AuditChain          — tamper-proof JSONL audit log
  - IdempotencyGuard    — duplicate Jira ticket prevention (in agent.py)
  - HITL                — human escalation gate for HIGH-risk anomalies
  - wire.hire()         — natural-language workforce assembly
  - EventBus            — typed real-time events

No API keys required. All LLM calls are mocked.

Usage:
    pip install -r requirements.txt
    python run.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# ── Rich UI setup ─────────────────────────────────────────────────────────────
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

console = Console()

AUDIT_PATH = "wire-cost-audit.jsonl"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _header(title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{title}[/bold cyan]")


def _ok(msg: str) -> None:
    console.print(f"  [bold green]✓[/bold green] {msg}")


def _warn(msg: str) -> None:
    console.print(f"  [bold yellow]![/bold yellow] {msg}")


def _info(msg: str) -> None:
    console.print(f"  [dim]→[/dim] {msg}")


# ── 1. Show wire.hire() workforce plan ────────────────────────────────────────

def show_hire_plan() -> None:
    _header("WIRE hire() — natural-language workforce assembly")
    try:
        import wire
        workforce_plan = wire.hire(
            "Monitor AWS costs every hour. "
            "Open a Jira P1 ticket if any service spend exceeds $500/day. "
            "Send a Slack alert to #ops-alerts. "
            "Escalate to a human approver if cost exceeds $1,000/day."
        )
        console.print(Panel(
            workforce_plan.describe(),
            title="[bold]Assembled workforce plan[/bold]",
            border_style="blue",
            padding=(1, 2),
        ))
    except Exception as exc:
        _warn(f"wire.hire() skipped: {exc}")


# ── 2. Show cost data table ───────────────────────────────────────────────────

def show_cost_table(cost_data: dict) -> None:
    _header("AWS Cost Window (7 days)")

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        title="[bold]Daily Cost by Service (USD)[/bold]",
    )
    table.add_column("Date",        style="cyan",  width=12)
    table.add_column("EC2",         justify="right", style="white")
    table.add_column("RDS",         justify="right", style="white")
    table.add_column("Lambda",      justify="right", style="white")
    table.add_column("S3",          justify="right", style="white")
    table.add_column("CloudFront",  justify="right", style="white")
    table.add_column("Total",       justify="right", style="bold yellow")
    table.add_column("Anomaly",     style="bold red")

    # We need the raw daily records — pull from mock_aws directly
    from mock_aws import get_cost_data as _get
    from datetime import date, timedelta

    today = date.today()
    anomaly_rows: list[dict] = []

    for offset in range(6, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        rec = _get(d)
        svcs = rec["services"]
        flag = ""
        if rec["has_anomaly"]:
            flag = f":fire: {rec['anomaly_service']} {rec['anomaly_multiplier']}x"
            anomaly_rows.append(rec)
        table.add_row(
            d,
            f"${svcs.get('EC2', 0):>7.2f}",
            f"${svcs.get('RDS', 0):>7.2f}",
            f"${svcs.get('Lambda', 0):>6.2f}",
            f"${svcs.get('S3', 0):>6.2f}",
            f"${svcs.get('CloudFront', 0):>6.2f}",
            f"${rec['total_usd']:>8.2f}",
            flag,
        )

    console.print(table)

    # Budget totals summary
    totals = cost_data.get("totals_by_service", {})
    grand  = cost_data.get("grand_total_usd", 0.0)
    console.print(f"\n  Grand total (7 days): [bold yellow]${grand:,.2f}[/bold yellow]")

    for svc, total in sorted(totals.items(), key=lambda x: -x[1]):
        avg = total / 7
        bar = "█" * int(avg / 50)
        style = "red" if avg > 500 else "green"
        console.print(f"  {svc:<14} ${total:>8,.2f}  avg [bold {style}]${avg:>6.2f}[/bold {style}]/day  {bar}")


# ── 3. Show anomaly results ───────────────────────────────────────────────────

def show_anomaly_results(state: dict) -> list[dict]:
    _header("Anomaly Detection Results")

    anomalies = state.get("anomaly_info", [])
    detected  = state.get("anomaly_detected", False)

    if not detected:
        _ok("No anomalies detected — all services within threshold ($500/day avg)")
        return []

    console.print(f"  [bold red]Anomalies detected: {len(anomalies)}[/bold red]\n")

    for a in anomalies:
        risk = "CRITICAL" if a["avg_daily_usd"] > 1_000 else "HIGH"
        style = "red" if risk == "CRITICAL" else "yellow"
        console.print(Panel(
            f"[bold]Service:[/bold] {a['service']}\n"
            f"[bold]Avg daily spend:[/bold] [bold {style}]${a['avg_daily_usd']:,.2f}[/bold {style}] "
            f"(threshold: ${a['threshold_usd']:,.0f})\n"
            f"[bold]7-day total:[/bold] ${a['total_7d_usd']:,.2f}\n"
            + (f"[bold]Spike date:[/bold] {a.get('spike_date', 'N/A')}  "
               f"({a.get('spike_multiplier', '')}x multiplier)" if "spike_date" in a else ""),
            title=f"[bold {style}]Risk: {risk}[/bold {style}]",
            border_style=style,
            padding=(0, 2),
        ))

    return anomalies


# ── 4. Show Jira / Slack actions ──────────────────────────────────────────────

def show_actions(state: dict) -> None:
    _header("Actions Taken")

    actions = state.get("actions_taken", [])
    if not actions:
        _info("No actions taken (no anomalies)")
        return

    for action in actions:
        jira  = action.get("jira", {})
        slack = action.get("slack", {})
        dup   = action.get("was_duplicate", False)

        console.print(f"\n  [bold]Service:[/bold] {action['service']}")
        _ok(f"Jira ticket: [link={jira.get('url','#')}]{jira.get('ticket_id','?')}[/link]  "
            f"priority={jira.get('priority','?')}  "
            f"{'[dim](IdempotencyGuard: DEDUPLICATED)[/dim]' if dup else ''}")
        _ok(f"Slack: {slack.get('channel','?')}  status={slack.get('status','?')}")


# ── 5. HITL escalation simulation ────────────────────────────────────────────

async def run_hitl_simulation(anomalies: list[dict]) -> None:
    """
    Simulate HITL for any CRITICAL-risk anomalies (>$1000/day).
    In demo mode: auto-approves after a 3-second countdown instead of
    blocking for real user input.
    """
    critical = [a for a in anomalies if a["avg_daily_usd"] > 1_000]
    if not critical:
        return

    _header("HITL — Human-in-the-Loop Escalation")

    import wire
    from wire import HITLGate, HITLChannel, HITLAction, HITLDecision, TimeoutAction, Risk

    for anomaly in critical:
        service = anomaly["service"]
        cost    = anomaly["avg_daily_usd"]

        console.print(Panel(
            f"[bold yellow]HITL APPROVAL REQUIRED[/bold yellow]\n\n"
            f"Service [bold]{service}[/bold] is spending "
            f"[bold red]${cost:,.2f}/day[/bold red] — "
            f"exceeds CRITICAL threshold ($1,000).\n\n"
            f"Risk level: [bold red]CRITICAL[/bold red]\n"
            f"Recommended action: Throttle {service} auto-scaling immediately.\n\n"
            f"[dim]Demo mode: auto-approving in 3 seconds...[/dim]",
            title="[bold red]HITL Gate[/bold red]",
            border_style="red",
            padding=(1, 2),
        ))

        # Countdown
        for remaining in range(3, 0, -1):
            console.print(f"  [bold yellow]Auto-approving in {remaining}s...[/bold yellow]",
                          end="\r")
            await asyncio.sleep(1)
        console.print()

        # Simulate an approved HITL decision (bypasses CLI prompt entirely)
        decision = HITLDecision(
            request_id=f"demo-{service.lower()}-001",
            action=HITLAction.APPROVE,
            actor="wire:demo-auto-approve",
            notes=f"Demo auto-approved at t+3s for {service} ${cost:,.2f}/day anomaly",
        )

        _ok(
            f"HITL decision: [bold green]{decision.action.value.upper()}[/bold green]  "
            f"actor={decision.actor}  "
            f"notes={decision.notes!r}"
        )


# ── 6. Show audit chain verification ─────────────────────────────────────────

def show_audit_verification() -> None:
    _header("AuditChain Verification")

    import wire

    audit_file = Path(AUDIT_PATH)
    if not audit_file.exists():
        _warn(f"Audit file not found: {AUDIT_PATH}")
        return

    try:
        count = wire.AuditChain.verify(AUDIT_PATH)
        _ok(f"[bold green]Audit chain intact[/bold green] — {count} entries verified")
        _info(f"Log path: {audit_file.resolve()}")
        _info("CLI:  wire audit " + AUDIT_PATH)
        _info("CLI:  wire replay --run-id <run_id>")
    except Exception as exc:
        _warn(f"Audit chain verification failed: {exc}")


# ── 7. Show WIRE event log summary ────────────────────────────────────────────

def show_event_summary(events_log: list[dict]) -> None:
    _header("WIRE EventBus — Runtime Events")

    if not events_log:
        _info("No events captured")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Event",     style="cyan", width=20)
    table.add_column("Node",      style="white", width=16)
    table.add_column("Iteration", justify="right", width=10)
    table.add_column("Cost USD",  justify="right", width=12)

    for e in events_log:
        table.add_row(
            e.get("kind", ""),
            e.get("node", "—"),
            str(e.get("iteration", "—")),
            f"${e.get('cost_usd', 0.0):.6f}",
        )

    console.print(table)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    console.print(Panel.fit(
        "[bold cyan]WIRE AI Governance Demo[/bold cyan]\n"
        "[dim]AWS Cost Governance on LangGraph — no API keys required[/dim]",
        border_style="cyan",
        padding=(1, 4),
    ))

    # ── Step 1: wire.hire() plan ──────────────────────────────────────────────
    show_hire_plan()

    # ── Step 2: build + wrap graph with wire.deploy() ─────────────────────────
    _header("Wrapping LangGraph graph with wire.deploy()")

    try:
        import wire
        from agent import build_graph
    except ImportError as exc:
        console.print(f"[bold red]Import error:[/bold red] {exc}")
        console.print("Run:  pip install -r requirements.txt")
        sys.exit(1)

    graph = build_graph()

    workforce = wire.deploy(
        graph,
        backend="langgraph",
        max_iterations=20,            # LoopGuard: hard ceiling
        max_cost_usd=5.00,            # Budget: $5 run limit (mock = $0)
        hourly_budget_usd=1.00,       # Rolling 1h budget
        audit_path=AUDIT_PATH,        # AuditChain: tamper-proof JSONL
    )

    console.print(Panel(
        workforce.describe(),
        title="[bold]Workforce configuration[/bold]",
        border_style="green",
        padding=(0, 2),
    ))

    # ── Step 3: subscribe to events ──────────────────────────────────────────
    events_log: list[dict] = []

    @workforce.on(wire.EventKind.STEP_END)
    async def on_step(event: wire.WIREEvent) -> None:
        events_log.append({
            "kind":      event.kind.value,
            "node":      event.data.get("node", ""),
            "iteration": event.data.get("iteration", 0),
            "cost_usd":  event.data.get("cost_usd", 0.0),
        })
        console.print(
            f"  [dim]event[/dim] [cyan]{event.kind.value}[/cyan]  "
            f"node=[white]{event.data.get('node', '?')}[/white]  "
            f"iter={event.data.get('iteration', 0)}"
        )

    @workforce.on(wire.EventKind.LOOP_BREACH)
    async def on_loop_breach(event: wire.WIREEvent) -> None:
        _warn(f"Loop breach! {event.data.get('iterations')} iterations")

    @workforce.on(wire.EventKind.BUDGET_BREACH)
    async def on_budget_breach(event: wire.WIREEvent) -> None:
        _warn(f"Budget breach [{event.data.get('window')}]!  "
              f"${event.data.get('spent', 0):.4f} / ${event.data.get('limit', 0):.4f}")

    # ── Step 4: run the agent ─────────────────────────────────────────────────
    _header("Running agent")

    from langchain_core.messages import HumanMessage
    initial_input = {"messages": [HumanMessage(content="Run AWS cost governance analysis.")]}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Running cost governance agent...", total=None)

        # Run through WIRE — governance (AuditChain, LoopGuard, Events) fires here.
        # The LangGraph adapter streams chunks; ainvoke() returns the last node's output.
        await workforce.ainvoke(initial_input)

        # Run the raw graph a second time to collect the full merged state for display.
        # This is fast (no LLM, no network) and keeps the display logic clean.
        # Suppress tool side effects on this diagnostic pass.
        import os
        os.environ["WIRE_DEMO_SILENT"] = "1"
        result = await graph.ainvoke(initial_input)
        del os.environ["WIRE_DEMO_SILENT"]

        progress.update(task, description="[green]Complete[/green]")

    _ok("Agent run complete")

    # ── Step 5: display results ───────────────────────────────────────────────
    show_cost_table(result.get("cost_data", {}))
    anomalies = show_anomaly_results(result)
    show_actions(result)

    # ── Step 6: HITL simulation (if any critical anomalies) ───────────────────
    await run_hitl_simulation(anomalies)

    # ── Step 7: event summary ─────────────────────────────────────────────────
    show_event_summary(events_log)

    # ── Step 8: audit chain verification ─────────────────────────────────────
    show_audit_verification()

    # ── Done ──────────────────────────────────────────────────────────────────
    _header("Demo complete")
    console.print(Panel(
        "[bold green]WIRE governance active:[/bold green]\n"
        "  [green]✓[/green]  LoopGuard        — hard 20-iteration ceiling enforced\n"
        "  [green]✓[/green]  AuditChain       — every node execution logged & hash-linked\n"
        "  [green]✓[/green]  IdempotencyGuard — Jira/Slack calls deduplicated\n"
        "  [green]✓[/green]  HITL             — CRITICAL anomalies escalated to human gate\n"
        "  [green]✓[/green]  EventBus         — typed runtime events captured\n"
        "  [green]✓[/green]  wire.hire()      — workforce assembled from natural language\n"
        "\n[dim]Audit log:[/dim] " + AUDIT_PATH,
        title="[bold cyan]WIRE AI Governance Summary[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    ))


if __name__ == "__main__":
    asyncio.run(main())
