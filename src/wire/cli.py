"""
wire CLI — wire status, wire audit verify, wire replay, wire dashboard
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="wire",
    help="WIRE — Workforce Intelligence & Reasoning Engine",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """Show WIRE version."""
    import wire
    console.print(f"[bold cyan]wire-ai[/bold cyan] v{wire.__version__}")


@app.command()
def audit(
    path: Path = typer.Argument(Path("wire-audit.jsonl"), help="Path to audit chain file"),
) -> None:
    """Verify audit chain integrity."""
    from wire.core.audit import AuditChain
    from wire.core.errors import AuditChainError

    if not path.exists():
        console.print(f"[red]✗[/red] Audit file not found: {path}")
        raise typer.Exit(1)

    try:
        count = AuditChain.verify(path)
        console.print(f"[green]✓[/green] Chain intact — [bold]{count}[/bold] entries verified · {path}")
    except AuditChainError as e:
        console.print(f"[red]✗ TAMPERED[/red] Entry {e.entry_index}")
        console.print(f"  Expected: {e.expected_hash}")
        console.print(f"  Got:      {e.actual_hash}")
        raise typer.Exit(1)


@app.command()
def replay(
    path: Path = typer.Argument(Path("wire-audit.jsonl"), help="Path to audit chain file"),
    run_id: str = typer.Option(..., "--run-id", "-r", help="Run ID to replay"),
    from_step: int = typer.Option(0, "--from", "-f", help="Start from step N"),
) -> None:
    """Replay a past workforce run from the audit chain."""
    import json

    if not path.exists():
        console.print(f"[red]✗[/red] Audit file not found: {path}")
        raise typer.Exit(1)

    entries = []
    with path.open() as f:
        for line in f:
            e = json.loads(line)
            if e.get("run_id") == run_id:
                entries.append(e)

    if not entries:
        console.print(f"[yellow]No entries found for run_id=[/yellow] {run_id}")
        raise typer.Exit(1)

    table = Table(title=f"Replay: {run_id}", show_lines=True)
    table.add_column("#", style="dim", width=5)
    table.add_column("Timestamp", style="cyan", width=26)
    table.add_column("Event", style="bold")
    table.add_column("Actor", style="green")
    table.add_column("Data", style="dim")

    for i, e in enumerate(entries[from_step:], start=from_step):
        table.add_row(
            str(i),
            e.get("ts", "")[:26],
            e.get("event", ""),
            e.get("actor", ""),
            str(e.get("data", {}))[:80],
        )

    console.print(table)
    console.print(f"\n[dim]{len(entries)} total entries for this run[/dim]")


@app.command()
def status() -> None:
    """Show WIRE installation status and available adapters."""
    import wire

    table = Table(title="WIRE Status", show_header=False)
    table.add_column("Key", style="bold cyan", width=20)
    table.add_column("Value")

    table.add_row("Version", wire.__version__)

    for backend, pkg in [
        ("langgraph", "langgraph"),
        ("crewai", "crewai"),
        ("autogen", "autogen_agentchat"),
        ("openai", "openai_agents"),
        ("foundry", "azure.ai.agents"),
    ]:
        try:
            __import__(pkg.replace("-", "_"))
            table.add_row(f"  {backend}", "[green]✓ installed[/green]")
        except ImportError:
            table.add_row(f"  {backend}", "[dim]not installed[/dim]")

    console.print(table)


@app.command()
def dashboard(
    port: int = typer.Option(8080, "--port", "-p", help="Port to serve the dashboard on"),
    audit_path: Path = typer.Option(
        Path("wire-audit.jsonl"), "--audit", "-a", help="Audit JSONL file to load events from"
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open browser"),
) -> None:
    """Start the web dashboard and open it in a browser."""
    import json

    from wire.visibility.dashboard import WorkforceDashboard
    from wire.visibility.web_dashboard import WebDashboard

    dash = WorkforceDashboard(workforce_name="wire-ai", audit_path=str(audit_path))

    # Load events from audit file into mock dashboard state
    if audit_path.exists():
        try:
            with audit_path.open() as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    role = entry.get("role") or entry.get("actor", "unknown")
                    event = entry.get("event", "")
                    data = entry.get("data") or {}
                    if role and role != "wire":
                        dash.update_role(
                            role,
                            status=data.get("status", "complete"),
                            cost_usd=float(data.get("cost_usd", 0.0)),
                            confidence=data.get("confidence"),
                        )
                    if event:
                        dash.add_event(role or "wire", f"{event}: {str(data)[:60]}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Warning: could not load audit file: {exc}[/yellow]")

    web = WebDashboard(dashboard=dash, port=port)
    url = web.url()
    console.print(f"[bold cyan]WIRE[/bold cyan] web dashboard starting at [bold]{url}[/bold]")

    async def _run() -> None:
        await web.start()
        if not no_browser:
            import webbrowser
            await asyncio.sleep(0.5)
            webbrowser.open(url)
        console.print("[dim]Press Ctrl+C to stop.[/dim]")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await web.stop()
            console.print("[dim]Dashboard stopped.[/dim]")

    asyncio.run(_run())
