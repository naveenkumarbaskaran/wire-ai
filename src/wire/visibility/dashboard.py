"""
WorkforceDashboard — live terminal UI showing the workforce in real time.

Built on Textual (full TUI) with Rich fallback for simpler environments.
Shows:
  - Active roles with status, confidence, SLA health
  - HITL queue with inline approve/reject
  - Cost ledger per role, real-time
  - Recent events feed
  - Budget progress bar

Usage:
    from wire.visibility.dashboard import WorkforceDashboard
    dashboard = WorkforceDashboard(workforce_name="aws-cost-monitor")
    dashboard.update_role("cost_monitor", status="running", confidence=0.94, cost=0.04)
    dashboard.add_event("anomaly_detector", "FLAGGED cost spike +$847")
    dashboard.run()   # blocks — live TUI
    # or:
    dashboard.print_snapshot()  # one-shot Rich print, non-blocking
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

log = structlog.get_logger(__name__)


class AgentStatus(str, Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    WAITING  = "waiting"    # HITL pending
    COMPLETE = "complete"
    ERROR    = "error"


@dataclass
class RoleState:
    name: str
    status: AgentStatus = AgentStatus.IDLE
    confidence: float | None = None
    cost_usd: float = 0.0
    sla_ok: bool = True
    sla_elapsed_s: float = 0.0
    sla_limit_s: float | None = None
    last_event: str = ""
    last_event_ts: str = ""
    iteration: int = 0


@dataclass
class HITLQueueItem:
    id: str
    role: str
    message: str
    risk: str
    expires_at: str
    options: list[str] = field(default_factory=lambda: ["approve", "reject"])


@dataclass
class EventLogEntry:
    ts: str
    role: str
    message: str
    level: str = "info"   # info | warning | error | hitl


class WorkforceDashboard:
    """
    Live workforce dashboard — terminal UI for engineers and executives.

    Non-blocking: call print_snapshot() for a one-time Rich render.
    Blocking:     call run() to launch the live auto-refreshing TUI.
    Async:        call run_async() to launch in an async context.
    """

    def __init__(
        self,
        *,
        workforce_name: str = "workforce",
        backend: str = "langgraph",
        audit_path: str = "wire-audit.jsonl",
        budget_daily: float | None = None,
        refresh_rate: float = 1.0,
    ) -> None:
        self.workforce_name = workforce_name
        self.backend = backend
        self.audit_path = audit_path
        self.budget_daily = budget_daily
        self.refresh_rate = refresh_rate
        self._roles: dict[str, RoleState] = {}
        self._hitl_queue: list[HITLQueueItem] = []
        self._event_log: list[EventLogEntry] = []
        self._total_cost: float = 0.0
        self._started_at: str = datetime.now(timezone.utc).isoformat()[:19]
        self._console = Console()

    # ── Public update API ─────────────────────────────────────────────────────

    def update_role(
        self,
        role: str,
        *,
        status: str | AgentStatus | None = None,
        confidence: float | None = None,
        cost_usd: float = 0.0,
        sla_ok: bool = True,
        sla_elapsed_s: float = 0.0,
        sla_limit_s: float | None = None,
        last_event: str = "",
        iteration: int = 0,
    ) -> None:
        if role not in self._roles:
            self._roles[role] = RoleState(name=role)
        state = self._roles[role]
        if status:
            state.status = AgentStatus(status) if isinstance(status, str) else status
        if confidence is not None:
            state.confidence = confidence
        state.cost_usd += cost_usd
        self._total_cost += cost_usd
        state.sla_ok = sla_ok
        state.sla_elapsed_s = sla_elapsed_s
        if sla_limit_s:
            state.sla_limit_s = sla_limit_s
        if last_event:
            state.last_event = last_event
            state.last_event_ts = datetime.now(timezone.utc).isoformat()[11:19]
        if iteration:
            state.iteration = iteration

    def add_event(self, role: str, message: str, level: str = "info") -> None:
        ts = datetime.now(timezone.utc).isoformat()[11:19]
        self._event_log.append(EventLogEntry(ts=ts, role=role, message=message, level=level))
        if len(self._event_log) > 50:
            self._event_log.pop(0)

    def add_hitl(
        self,
        *,
        id: str,
        role: str,
        message: str,
        risk: str = "high",
        expires_at: str = "",
        options: list[str] | None = None,
    ) -> None:
        self._hitl_queue.append(HITLQueueItem(
            id=id, role=role, message=message, risk=risk,
            expires_at=expires_at, options=options or ["approve", "reject"],
        ))
        self.update_role(role, status=AgentStatus.WAITING)
        self.add_event(role, f"HITL requested: {message[:60]}", level="hitl")

    def resolve_hitl(self, id: str) -> None:
        self._hitl_queue = [h for h in self._hitl_queue if h.id != id]

    # ── Rendering ─────────────────────────────────────────────────────────────

    def print_snapshot(self) -> None:
        """One-shot Rich print — non-blocking, no live updates."""
        self._console.print(self._build_layout())

    def run(self, duration_s: float | None = None) -> None:
        """Launch live auto-refreshing TUI (blocks)."""
        try:
            with Live(
                self._build_layout(),
                console=self._console,
                refresh_per_second=int(1 / self.refresh_rate),
                screen=False,
            ) as live:
                import time
                start = time.time()
                while True:
                    time.sleep(self.refresh_rate)
                    live.update(self._build_layout())
                    if duration_s and (time.time() - start) > duration_s:
                        break
        except KeyboardInterrupt:
            pass

    async def run_async(self, duration_s: float | None = None) -> None:
        """Async version of run() — yields control between refreshes."""
        try:
            with Live(
                self._build_layout(),
                console=self._console,
                refresh_per_second=int(1 / self.refresh_rate),
                screen=False,
            ) as live:
                elapsed = 0.0
                while True:
                    await asyncio.sleep(self.refresh_rate)
                    live.update(self._build_layout())
                    elapsed += self.refresh_rate
                    if duration_s and elapsed >= duration_s:
                        break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    # ── Layout builders ───────────────────────────────────────────────────────

    def _build_layout(self) -> Panel:
        now = datetime.now(timezone.utc).isoformat()[11:19]

        lines: list[Any] = []

        # ── Header ────────────────────────────────────────────────────────────
        header = Text()
        header.append(f"Workforce: ", style="dim")
        header.append(self.workforce_name, style="bold cyan")
        header.append(f"   Backend: {self.backend}", style="dim")
        header.append(f"   {now} UTC", style="dim")
        lines.append(header)
        lines.append(Text("─" * 72, style="dim"))

        # ── Cost summary ──────────────────────────────────────────────────────
        cost_line = Text()
        cost_line.append("Total cost: ", style="dim")
        cost_line.append(f"${self._total_cost:.4f}", style="bold green")
        if self.budget_daily:
            pct = min(self._total_cost / self.budget_daily * 100, 100)
            cost_line.append(f"  /  ${self.budget_daily:.2f} daily  ", style="dim")
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            colour = "green" if pct < 60 else "yellow" if pct < 85 else "red"
            cost_line.append(f"[{bar}]", style=colour)
        lines.append(cost_line)
        lines.append(Text(""))

        # ── Roles table ───────────────────────────────────────────────────────
        if self._roles:
            table = Table(box=None, show_header=True, header_style="bold dim", padding=(0, 1))
            table.add_column("ROLE",       style="bold",   width=22)
            table.add_column("STATUS",     width=10)
            table.add_column("CONFIDENCE", width=12)
            table.add_column("COST",       width=8)
            table.add_column("SLA",        width=8)
            table.add_column("LAST EVENT", min_width=20)

            _status_icons = {
                AgentStatus.IDLE:     "[dim]○ idle[/dim]",
                AgentStatus.RUNNING:  "[green]● running[/green]",
                AgentStatus.WAITING:  "[yellow]⏸ waiting[/yellow]",
                AgentStatus.COMPLETE: "[cyan]✓ done[/cyan]",
                AgentStatus.ERROR:    "[red]✗ error[/red]",
            }

            for role in self._roles.values():
                conf_str = (
                    f"[{'green' if (role.confidence or 0) >= 0.80 else 'yellow'}]{role.confidence:.0%}[/]"
                    if role.confidence is not None else "[dim]—[/dim]"
                )
                sla_str = (
                    "[green]✓[/green]" if role.sla_ok else
                    f"[red]⚠ {role.sla_elapsed_s:.0f}s[/red]"
                )
                last = f"[dim]{role.last_event_ts}[/dim] {role.last_event[:28]}" if role.last_event else "[dim]—[/dim]"

                table.add_row(
                    role.name,
                    _status_icons.get(role.status, role.status),
                    conf_str,
                    f"${role.cost_usd:.4f}",
                    sla_str,
                    last,
                )
            lines.append(table)
        else:
            lines.append(Text("[dim]No active roles.[/dim]"))

        # ── HITL queue ────────────────────────────────────────────────────────
        if self._hitl_queue:
            lines.append(Text(""))
            lines.append(Text(f"HITL QUEUE  ({len(self._hitl_queue)} pending)", style="bold yellow"))
            for item in self._hitl_queue[:3]:
                hitl_line = Text()
                hitl_line.append(f"  [{item.id[:8]}] ", style="dim")
                hitl_line.append(f"{item.message[:55]}", style="white")
                hitl_line.append(f"  risk={item.risk}", style="yellow")
                hitl_line.append(f"  expires {item.expires_at[11:19]}", style="dim")
                lines.append(hitl_line)
                options_str = " / ".join(item.options)
                lines.append(Text(f"            → {options_str}", style="dim"))

        # ── Event log ─────────────────────────────────────────────────────────
        if self._event_log:
            lines.append(Text(""))
            lines.append(Text("RECENT EVENTS", style="bold dim"))
            _level_styles = {
                "info":    "dim",
                "warning": "yellow",
                "error":   "red",
                "hitl":    "bold yellow",
            }
            for entry in reversed(self._event_log[-6:]):
                style = _level_styles.get(entry.level, "dim")
                ev_line = Text()
                ev_line.append(f"  {entry.ts}  ", style="dim")
                ev_line.append(f"{entry.role:18}", style="cyan")
                ev_line.append(entry.message[:50], style=style)
                lines.append(ev_line)

        from rich.console import Group
        return Panel(
            Group(*lines),
            title="[bold cyan]WIRE[/bold cyan] Workforce Dashboard",
            border_style="cyan",
            padding=(0, 1),
        )
