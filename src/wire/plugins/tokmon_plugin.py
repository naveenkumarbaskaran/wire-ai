"""
Tokmon plugin for WIRE — token and cost tracking with CLI dashboard.

Tokmon (github.com/naveenkumarbaskaran/tokmon) is a lightweight LLM token
and cost tracker with decorators, session tracking, and a CLI dashboard.

This plugin feeds WIRE workforce events into a tokmon session so every
workforce run appears in the tokmon dashboard alongside any other LLM usage
in the same environment.

Falls back to WIRE's built-in CostLedger when tokmon is not installed,
preserving all cost data inside WIRE's own audit chain.

Install:
    pip install tokmon
"""

from __future__ import annotations

from typing import Any

import structlog

from wire.plugins import WIREPlugin
from wire.visibility.ledger import CostLedger

log = structlog.get_logger(__name__)


class TokmonPlugin(WIREPlugin):
    """
    Tokmon integration — token and cost tracking with CLI dashboard.

    Args:
        session_name: Label for this workforce run in the tokmon dashboard.
                      Default: "wire-session"
        budget_usd:   Optional hard budget ceiling passed to tokmon.
                      When set, tokmon will warn (or stop) at this threshold.
        dashboard:    If True, print a tokmon summary table at workforce end.
                      Default: False

    Graceful degradation:
        - tokmon installed  → records to a tokmon session; full dashboard.
        - tokmon missing    → delegates to WIRE's built-in CostLedger;
                              summary printed via structlog.
    """

    name = "tokmon"
    version = "1.0.0"

    def __init__(
        self,
        *,
        session_name: str = "wire-session",
        budget_usd: float | None = None,
        dashboard: bool = False,
    ) -> None:
        self._session_name = session_name
        self._budget_usd = budget_usd
        self._dashboard = dashboard
        # Fallback ledger — always maintained regardless of tokmon availability
        self._ledger = CostLedger()
        # tokmon session handle — set lazily in on_step_end if tokmon is available
        self._tokmon_session: Any = None

    # ── Availability check ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the tokmon package is importable."""
        try:
            import tokmon  # noqa: F401  # type: ignore[import-not-found]
            return True
        except ImportError:
            return False

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    async def on_step_start(self, run_id: str, role: str, iteration: int) -> None:
        """No-op for tokmon — cost is recorded on step end when token counts are known."""
        log.debug("tokmon.step_start", run_id=run_id, role=role, iteration=iteration)

    async def on_step_end(
        self,
        run_id: str,
        role: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Record token usage to the tokmon session (or CostLedger fallback)."""
        # Always record to fallback ledger
        self._ledger.record(
            run_id=run_id,
            role=role,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

        if self._try_tokmon_record(
            run_id=run_id,
            role=role,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        ):
            return

        log.info(
            "tokmon.step_end_fallback",
            run_id=run_id,
            role=role,
            cost_usd=round(cost_usd, 8),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def on_tool_call(
        self, run_id: str, tool: str, args: dict[str, Any]
    ) -> None:
        """Tokmon tracks tokens, not tool calls — log and continue."""
        log.debug("tokmon.tool_call", run_id=run_id, tool=tool)

    async def on_workforce_end(
        self, run_id: str, total_cost: float, iterations: int
    ) -> None:
        """Flush the tokmon session and optionally print the dashboard summary."""
        if self._try_tokmon_flush(run_id=run_id, total_cost=total_cost):
            return

        # Fallback: emit summary via structlog + optional rich print
        summary = self._ledger.summary()
        log.info(
            "tokmon.workforce_end_fallback",
            run_id=run_id,
            total_cost_usd=round(total_cost, 8),
            iterations=iterations,
            ledger_summary=summary,
        )
        if self._dashboard:
            self._print_fallback_summary(run_id=run_id, summary=summary, iterations=iterations)

    # ── Tokmon SDK helpers ────────────────────────────────────────────────────

    def _get_or_create_session(self) -> Any:
        """Lazy-initialise the tokmon session on first use."""
        if self._tokmon_session is not None:
            return self._tokmon_session
        try:
            import tokmon  # type: ignore[import-not-found]

            if hasattr(tokmon, "Session"):
                kwargs: dict[str, Any] = {"name": self._session_name}
                if self._budget_usd is not None:
                    kwargs["budget_usd"] = self._budget_usd
                self._tokmon_session = tokmon.Session(**kwargs)
            elif hasattr(tokmon, "create_session"):
                kwargs = {"name": self._session_name}
                if self._budget_usd is not None:
                    kwargs["budget_usd"] = self._budget_usd
                self._tokmon_session = tokmon.create_session(**kwargs)
        except (ImportError, Exception) as exc:  # noqa: BLE001
            log.debug("tokmon.session_init_failed", error=str(exc))
        return self._tokmon_session

    def _try_tokmon_record(self, **kwargs: Any) -> bool:
        session = self._get_or_create_session()
        if session is None:
            return False
        try:
            if hasattr(session, "record"):
                session.record(**kwargs)
                return True
            # Alternative API: add_usage / track
            for method in ("add_usage", "track"):
                if hasattr(session, method):
                    getattr(session, method)(**kwargs)
                    return True
        except Exception as exc:  # noqa: BLE001
            log.warning("tokmon.record_failed", error=str(exc))
        return False

    def _try_tokmon_flush(self, run_id: str, total_cost: float) -> bool:
        session = self._get_or_create_session()
        if session is None:
            return False
        try:
            for method in ("flush", "end", "close", "finish"):
                if hasattr(session, method):
                    getattr(session, method)()
                    if self._dashboard and hasattr(session, "print_summary"):
                        session.print_summary()
                    return True
        except Exception as exc:  # noqa: BLE001
            log.warning("tokmon.flush_failed", run_id=run_id, error=str(exc))
        return False

    # ── Fallback summary ──────────────────────────────────────────────────────

    def _print_fallback_summary(
        self,
        run_id: str,
        summary: dict[str, Any],
        iterations: int,
    ) -> None:
        """Print a basic cost summary via rich when tokmon is not available."""
        try:
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title=f"WIRE Cost Summary — {self._session_name}")
            table.add_column("Role", style="cyan")
            table.add_column("Cost (USD)", style="green", justify="right")

            for role, cost in summary.get("by_role", {}).items():
                table.add_row(role, f"${cost:.6f}")

            table.add_row(
                "[bold]TOTAL[/bold]",
                f"[bold]${summary.get('total_usd', 0.0):.6f}[/bold]",
            )
            console.print(table)
            console.print(
                f"  run_id={run_id}  iterations={iterations}  "
                f"entries={summary.get('entry_count', 0)}"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("tokmon.print_fallback_failed", error=str(exc))
