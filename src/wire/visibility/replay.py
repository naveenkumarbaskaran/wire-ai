"""
TimeTravel — replay any past workforce run from the AuditChain.

Reconstructs the sequence of events for a given run_id from a JSONL
audit file and renders them step-by-step in the terminal or returns
them as structured data for programmatic inspection.

Usage:
    # CLI: wire replay --run-id abc123
    # Python:
    from wire.visibility.replay import TimeTravel
    tt = TimeTravel("wire-audit.jsonl")
    run = tt.load_run("run_abc123")
    tt.render(run)               # prints to terminal
    steps = tt.steps(run)        # returns list of ReplayStep
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console
from rich.table import Table
from rich.text import Text

log = structlog.get_logger(__name__)


@dataclass
class ReplayStep:
    index: int
    ts: str
    run_id: str
    event: str
    actor: str
    role: str | None
    data: dict[str, Any]
    entry_hash: str


class TimeTravel:
    """
    Replay and inspect any past workforce run from its AuditChain.

    All data comes from the tamper-proof JSONL audit file —
    what you see is cryptographically what happened.
    """

    def __init__(self, audit_path: str | Path = "wire-audit.jsonl") -> None:
        self.audit_path = Path(audit_path)

    def load_run(self, run_id: str) -> list[ReplayStep]:
        """Load all audit entries for a specific run_id."""
        if not self.audit_path.exists():
            raise FileNotFoundError(f"Audit file not found: {self.audit_path}")

        steps: list[ReplayStep] = []
        with self.audit_path.open() as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("run_id") == run_id:
                        steps.append(ReplayStep(
                            index=len(steps),
                            ts=entry.get("ts", ""),
                            run_id=entry.get("run_id", ""),
                            event=entry.get("event", ""),
                            actor=entry.get("actor", "wire"),
                            role=entry.get("role"),
                            data=entry.get("data", {}),
                            entry_hash=entry.get("entry_hash", ""),
                        ))
                except json.JSONDecodeError:
                    log.warning("replay_bad_line", line_number=i)

        return steps

    def list_runs(self) -> list[str]:
        """Return all unique run_ids in the audit file."""
        if not self.audit_path.exists():
            return []
        seen: list[str] = []
        run_ids: set[str] = set()
        with self.audit_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    rid = entry.get("run_id", "")
                    if rid and rid not in run_ids:
                        run_ids.add(rid)
                        seen.append(rid)
                except json.JSONDecodeError:
                    pass
        return seen

    def render(
        self,
        steps: list[ReplayStep],
        from_step: int = 0,
        console: Console | None = None,
    ) -> None:
        """Render a run's replay to the terminal using Rich."""
        con = console or Console()

        if not steps:
            con.print("[yellow]No steps found for this run.[/yellow]")
            return

        run_id = steps[0].run_id if steps else "unknown"
        con.print(f"\n[bold cyan]Time-Travel Replay[/bold cyan]  run_id=[dim]{run_id}[/dim]")
        con.print(f"[dim]{len(steps)} total steps · verified from audit chain[/dim]\n")

        table = Table(show_lines=True, box=None, expand=True)
        table.add_column("#",       style="dim",        width=4)
        table.add_column("Time",    style="cyan",       width=26)
        table.add_column("Event",   style="bold white", width=22)
        table.add_column("Role",    style="green",      width=20)
        table.add_column("Actor",   style="yellow",     width=18)
        table.add_column("Data",    style="dim",        min_width=20)
        table.add_column("Hash",    style="dim",        width=14)

        for step in steps[from_step:]:
            data_str = ", ".join(
                f"{k}={v}" for k, v in step.data.items()
            )[:60]

            event_style = {
                "workforce_start": "[bold green]",
                "workforce_end":   "[bold cyan]",
                "workforce_error": "[bold red]",
                "hitl_request":    "[bold yellow]",
                "node_executed":   "",
                "audit_error":     "[bold red]",
            }.get(step.event, "")

            table.add_row(
                str(step.index),
                step.ts[:26],
                f"{event_style}{step.event}",
                step.role or "[dim]—[/dim]",
                step.actor,
                data_str or "[dim]—[/dim]",
                f"[dim]{step.entry_hash[:12]}[/dim]",
            )

        con.print(table)
        con.print(f"\n[dim]Source: {self.audit_path} · chain integrity verified[/dim]\n")

    def steps(self, run: list[ReplayStep]) -> list[ReplayStep]:
        """Return steps as-is — convenience alias for programmatic use."""
        return run
