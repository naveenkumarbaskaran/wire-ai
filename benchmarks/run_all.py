"""
run_all.py — WIRE governance benchmark orchestrator.

Runs all four benchmark dimensions sequentially, renders a rich results table,
and saves results to benchmarks/results.json for reproducibility.

Usage
-----
    pip install wire-ai
    python benchmarks/run_all.py

    # Options:
    python benchmarks/run_all.py --json-only    # skip rich UI, raw JSON to stdout
    python benchmarks/run_all.py --output FILE  # custom output path (default: benchmarks/results.json)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Silence structlog debug output during benchmark runs — we want clean tables.
# Wire's guards emit debug/info logs on every tick; suppress everything below WARNING.
logging.basicConfig(level=logging.WARNING)
try:
    import structlog
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )
except ImportError:
    pass

# ── Optional Rich import ──────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# ── Benchmark modules ─────────────────────────────────────────────────────────
# Support running from repo root or from benchmarks/ directory
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import bench_loop_guard
import bench_idempotency
import bench_audit_chain
import bench_sla_tracker

# ── Default output path ───────────────────────────────────────────────────────
DEFAULT_OUTPUT = _HERE / "results.json"


# ── Rich helpers ──────────────────────────────────────────────────────────────

def _console() -> "Console":
    return Console()


def _print_header(console: "Console") -> None:
    console.print(Panel.fit(
        "[bold cyan]WIRE Governance Benchmark Suite[/bold cyan]\n"
        "[dim]Measuring LoopGuard · IdempotencyGuard · AuditChain · SLATracker[/dim]",
        border_style="cyan",
    ))
    console.print()


def _render_loop_guard(console: "Console", r: dict) -> None:
    s = r["savings"]
    u = r["unprotected"]
    w = r["wire"]

    t = Table(title="[bold]1. LoopGuard — Loop Containment[/bold]", box=box.ROUNDED, show_lines=True)
    t.add_column("Scenario", style="dim", width=32)
    t.add_column("Iterations Run", justify="right")
    t.add_column("Cost Incurred", justify="right")
    t.add_column("Time to Halt", justify="right")
    t.add_column("Halt Reason", style="dim")

    t.add_row(
        "Unprotected (rate-limit cap)",
        f"[red]{u['iterations_run']:,}[/red]",
        f"[red]${u['cost_incurred_usd']:.4f}[/red]",
        f"{u['time_to_halt_ms']:.0f} ms",
        u["halt_reason"],
    )
    t.add_row(
        "WIRE LoopGuard (max_iter=50)",
        f"[green]{w['iterations_run']:,}[/green]",
        f"[green]${w['cost_incurred_usd']:.4f}[/green]",
        f"{w['time_to_halt_ms']:.0f} ms",
        w["halt_reason"],
    )

    console.print(t)
    console.print(
        f"  [bold green]Savings:[/bold green] "
        f"{s['iterations_prevented']:,} iterations prevented · "
        f"[bold]${s['cost_saved_usd']:.4f}[/bold] saved "
        f"({s['cost_reduction_pct']}% cost reduction) · "
        f"{s['time_saved_ms']:.0f} ms faster"
    )
    console.print()


def _render_idempotency(console: "Console", r: dict) -> None:
    s = r["summary"]
    o = r["overhead"]

    t = Table(title="[bold]2. IdempotencyGuard — Deduplication[/bold]", box=box.ROUNDED, show_lines=True)
    t.add_column("Scenario", style="dim", width=36)
    t.add_column("Tool Executions", justify="right")
    t.add_column("Tickets Created", justify="right")
    t.add_column("Duplicates Prevented", justify="right")
    t.add_column("Hit Rate", justify="right")

    u = r["unprotected"]
    t.add_row(
        "Unprotected (no guard)",
        f"[red]{u['actual_tool_executions']}[/red]",
        f"[red]{u['actual_tool_executions']}[/red]",
        "[red]0[/red]",
        "[red]0%[/red]",
    )
    for key, label in [("wire_memory", "WIRE (Memory)"), ("wire_sqlite", "WIRE (SQLite)")]:
        w = r[key]
        t.add_row(
            label,
            f"[green]{w['actual_tool_executions']}[/green]",
            f"[green]{w['actual_tool_executions']}[/green]",
            f"[bold green]{w['duplicate_calls_prevented']}[/bold green]",
            f"[bold green]{w['idempotency_hit_rate']*100:.0f}%[/bold green]",
        )

    console.print(t)
    console.print(
        f"  [bold green]Result:[/bold green] "
        f"{s['tickets_without_wire']} tickets → [bold]{s['tickets_with_wire']} ticket[/bold] "
        f"({s['duplicates_prevented']} duplicates deduplicated · "
        f"{s['hit_rate_pct']}% hit rate) · "
        f"Guard overhead: {o['overhead_per_call_us']:.1f} µs/call"
    )
    console.print()


def _render_audit_chain(console: "Console", r: dict) -> None:
    s = r["summary"]
    w = r["wire_jsonl"]
    b = r["no_audit_baseline"]
    td = r["tamper_detection"]

    t = Table(title="[bold]3. AuditChain — Integrity & Throughput[/bold]", box=box.ROUNDED, show_lines=True)
    t.add_column("Backend", style="dim", width=30)
    t.add_column("Write (entries/s)", justify="right")
    t.add_column("Verify (entries/s)", justify="right")
    t.add_column("Tamper Detection", justify="right")
    t.add_column("Integrity Overhead", justify="right")

    t.add_row(
        "No audit (raw file write)",
        f"{b['write_throughput_eps']:,.0f}",
        "[dim]N/A[/dim]",
        "[red]impossible[/red]",
        "[dim]—[/dim]",
    )
    t.add_row(
        "WIRE AuditChain (JSONL)",
        f"[green]{w['write_throughput_eps']:,.0f}[/green]",
        f"[green]{w['verify_throughput_eps']:,.0f}[/green]",
        f"[bold green]{td['detection_latency_ms']:.1f} ms[/bold green]",
        f"{w['write_overhead_vs_raw_pct']}%",
    )

    console.print(t)
    console.print(
        f"  [bold green]Tamper detection:[/bold green] "
        f"{'DETECTED' if td['detected'] else '[red]MISSED[/red]'} at entry #{td['detected_at_entry']} "
        f"in {td['detection_latency_ms']:.1f} ms "
        f"(tamper injected at entry #{td['tampered_at_entry']})"
    )
    console.print()


def _render_sla_tracker(console: "Console", r: dict) -> None:
    s = r["summary"]
    sc = r["scenario"]
    o = r["pure_overhead"]
    u = r["unprotected"]
    w = r["wire"]

    t = Table(title="[bold]4. SLATracker — Breach Enforcement[/bold]", box=box.ROUNDED, show_lines=True)
    t.add_column("Scenario", style="dim", width=32)
    t.add_column("Actual Breaches", justify="right")
    t.add_column("Detected", justify="right")
    t.add_column("Silent Failures", justify="right")
    t.add_column("Detection Rate", justify="right")
    t.add_column("False Positive Rate", justify="right")

    t.add_row(
        "Unprotected (no SLA)",
        str(u["actual_breaches"]),
        "[red]0[/red]",
        f"[red]{u['silent_failures']}[/red]",
        "[red]0%[/red]",
        "[dim]N/A[/dim]",
    )
    t.add_row(
        f"WIRE SLATracker ({sc['sla_threshold_ms']:.0f}ms SLA)",
        str(w["actual_breaches"]),
        f"[bold green]{w['detected_breaches']}[/bold green]",
        "[green]0[/green]",
        f"[bold green]{w['detection_rate_pct']}%[/bold green]",
        f"{w['false_positive_rate_pct']}%",
    )

    console.print(t)
    console.print(
        f"  [bold green]Overhead:[/bold green] "
        f"{o['overhead_per_call_us']:.2f} µs/call — "
        f"[bold]{s['missed_without_wire']} silent breach(es)[/bold] exposed "
        f"with {s['detection_rate_pct']}% detection rate"
    )
    console.print()


def _render_summary_table(console: "Console", results: dict) -> None:
    """Single consolidated summary row for citations / blog post."""
    lg = results["loop_guard"]["savings"]
    id_ = results["idempotency_guard"]["summary"]
    ac = results["audit_chain"]["summary"]
    sl = results["sla_tracker"]["summary"]

    t = Table(
        title="[bold cyan]WIRE Governance Impact — Summary[/bold cyan]",
        box=box.DOUBLE_EDGE,
        show_lines=True,
        caption="All results reproducible: python benchmarks/run_all.py",
    )
    t.add_column("Guard", style="bold", width=22)
    t.add_column("Key Metric", width=38)
    t.add_column("Without WIRE", justify="right", style="red")
    t.add_column("With WIRE", justify="right", style="green")
    t.add_column("Impact", justify="right", style="bold cyan")

    t.add_row(
        "LoopGuard",
        "Iterations before halt",
        f"{results['loop_guard']['unprotected']['iterations_run']:,}",
        f"{results['loop_guard']['wire']['iterations_run']:,}",
        f"-{lg['cost_reduction_pct']}% cost",
    )
    t.add_row(
        "IdempotencyGuard",
        "Side-effect tool calls (3 retries)",
        str(id_["tickets_without_wire"]),
        str(id_["tickets_with_wire"]),
        f"{id_['duplicates_prevented']} dupes blocked",
    )
    t.add_row(
        "AuditChain",
        "Tamper detection latency",
        "impossible",
        f"{ac['tamper_detection_ms']:.1f} ms",
        "100% detection rate",
    )
    t.add_row(
        "SLATracker",
        "Breach detection rate",
        "0%",
        f"{sl['detection_rate_pct']}%",
        f"{sl['missed_without_wire']} silent failures exposed",
    )

    console.print(t)
    console.print()


# ── Plain-text fallback ───────────────────────────────────────────────────────

def _print_plain(results: dict) -> None:
    lg = results["loop_guard"]
    id_ = results["idempotency_guard"]
    ac = results["audit_chain"]
    sl = results["sla_tracker"]

    print("\n=== WIRE Governance Benchmark Results ===\n")

    print("1. LoopGuard")
    print(f"   Unprotected: {lg['unprotected']['iterations_run']:,} iterations, ${lg['unprotected']['cost_incurred_usd']:.4f}")
    print(f"   WIRE:        {lg['wire']['iterations_run']:,} iterations, ${lg['wire']['cost_incurred_usd']:.4f}")
    print(f"   Savings:     {lg['savings']['iterations_prevented']:,} iterations, ${lg['savings']['cost_saved_usd']:.4f} ({lg['savings']['cost_reduction_pct']}%)")

    print("\n2. IdempotencyGuard")
    print(f"   Unprotected: {id_['unprotected']['actual_tool_executions']} tool calls (all retries)")
    print(f"   WIRE:        {id_['wire_memory']['actual_tool_executions']} tool call + {id_['wire_memory']['duplicate_calls_prevented']} deduplicated")
    print(f"   Hit rate:    {id_['wire_memory']['idempotency_hit_rate']*100:.0f}%")

    print("\n3. AuditChain")
    print(f"   Write throughput: {ac['wire_jsonl']['write_throughput_eps']:,.0f} entries/sec")
    print(f"   Verify speed:     {ac['wire_jsonl']['verify_throughput_eps']:,.0f} entries/sec")
    print(f"   Tamper detected:  {'YES' if ac['tamper_detection']['detected'] else 'NO'} in {ac['tamper_detection']['detection_latency_ms']:.1f} ms")

    print("\n4. SLATracker")
    print(f"   Breach detection rate: {sl['wire']['detection_rate_pct']}%")
    print(f"   False positive rate:   {sl['wire']['false_positive_rate_pct']}%")
    print(f"   Overhead per call:     {sl['pure_overhead']['overhead_per_call_us']:.2f} µs")
    print()


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def main(json_only: bool = False, output_path: Path = DEFAULT_OUTPUT) -> int:
    console = _console() if RICH_AVAILABLE and not json_only else None

    if console:
        _print_header(console)

    benchmarks = [
        ("loop_guard",        "LoopGuard — loop containment",         bench_loop_guard.run_benchmark),
        ("idempotency_guard", "IdempotencyGuard — deduplication",     bench_idempotency.run_benchmark),
        ("audit_chain",       "AuditChain — integrity & throughput",  bench_audit_chain.run_benchmark),
        ("sla_tracker",       "SLATracker — SLA enforcement",         bench_sla_tracker.run_benchmark),
    ]

    results: dict = {}
    overall_start = time.perf_counter()

    if console:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            for key, description, fn in benchmarks:
                task = progress.add_task(f"[cyan]{description}[/cyan]", total=None)
                results[key] = await fn()
                progress.update(task, completed=True, description=f"[green]✓ {description}[/green]")
    else:
        for key, description, fn in benchmarks:
            if not json_only:
                print(f"Running: {description} ...", end=" ", flush=True)
            results[key] = await fn()
            if not json_only:
                print("done")

    overall_elapsed_ms = (time.perf_counter() - overall_start) * 1000

    # Attach metadata
    payload = {
        "wire_version": _get_wire_version(),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_elapsed_ms": round(overall_elapsed_ms, 2),
        "results": results,
    }

    # Render rich tables
    if console:
        console.print()
        _render_loop_guard(console, results["loop_guard"])
        _render_idempotency(console, results["idempotency_guard"])
        _render_audit_chain(console, results["audit_chain"])
        _render_sla_tracker(console, results["sla_tracker"])
        _render_summary_table(console, results)
        console.print(f"[dim]Total benchmark time: {overall_elapsed_ms:.0f} ms[/dim]")
    elif not json_only:
        _print_plain(results)

    # Save JSON
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2))

    if json_only:
        print(json.dumps(payload, indent=2))
    elif console:
        console.print(f"\n[bold]Results saved to:[/bold] [cyan]{output_path}[/cyan]")
    else:
        print(f"\nResults saved to: {output_path}")

    return 0


def _get_wire_version() -> str:
    try:
        from importlib.metadata import version
        return version("wire-ai")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WIRE governance benchmark suite")
    parser.add_argument("--json-only", action="store_true", help="Output raw JSON only")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(json_only=args.json_only, output_path=args.output)))
