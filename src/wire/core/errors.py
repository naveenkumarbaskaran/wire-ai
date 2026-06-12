"""
WIRE unified error handling.

All WIRE errors follow a consistent shape:
  - error_code: machine-readable string (LOOP_BREACH, BUDGET_EXCEEDED, etc.)
  - message: human-readable explanation
  - details: structured context (what happened, what the limits were)
  - suggestion: what to do about it
  - docs_url: link to relevant documentation section

Usage:
    from wire.core.errors import WIREError, format_error

    try:
        await governed.ainvoke(...)
    except WIREError as e:
        print(e.user_message())   # clean, actionable message
        print(e.to_dict())        # machine-readable for logging/monitoring
"""

from __future__ import annotations

from typing import Any

DOCS_BASE = "https://naveenkumarbaskaran.github.io/wire-ai"


class WIREError(Exception):
    """Base class for all WIRE errors. Always includes an actionable message."""

    error_code: str = "WIRE_ERROR"
    docs_path: str = "#getting-started"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def user_message(self) -> str:
        """Clean, actionable message suitable for displaying to users."""
        lines = [f"[{self.error_code}] {self}"]
        if self.details:
            for k, v in self.details.items():
                lines.append(f"  {k}: {v}")
        lines.append(f"  → Docs: {DOCS_BASE}/{self.docs_path}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": str(self),
            "details": self.details,
            "docs_url": f"{DOCS_BASE}/{self.docs_path}",
        }

    def __str__(self) -> str:
        return super().__str__()


class LoopBreachError(WIREError):
    """Raised when LoopGuard detects a runaway agent loop."""

    error_code = "LOOP_BREACH"
    docs_path = "#core-primitives"

    def __init__(self, iterations: int, limit: int, cost_usd: float) -> None:
        self.iterations = iterations
        self.limit = limit
        self.cost_usd = cost_usd
        super().__init__(
            f"Agent loop exceeded limit: {iterations} iterations ran, limit was {limit}. "
            f"${cost_usd:.4f} spent before halt.",
            details={
                "iterations_run": iterations,
                "max_iterations": limit,
                "cost_usd": cost_usd,
                "suggestion": f"Increase max_iterations (current: {limit}) or fix the agent's "
                              "termination condition. Use wire.deploy(..., max_iterations=N).",
            },
        )


class BudgetBreachError(WIREError):
    """Raised when a Budget ceiling is exceeded."""

    error_code = "BUDGET_EXCEEDED"
    docs_path = "#core-primitives"

    def __init__(self, spent: float, limit: float, window: str) -> None:
        self.spent = spent
        self.limit = limit
        self.window = window
        super().__init__(
            f"Budget ceiling exceeded [{window}]: ${spent:.4f} spent, limit ${limit:.4f}.",
            details={
                "spent_usd": spent,
                "limit_usd": limit,
                "window": window,
                "suggestion": f"Raise the {window} budget: wire.deploy(..., "
                              f"{'max_cost_usd' if window == 'total' else window + '_budget_usd'}={limit * 2:.2f}), "
                              "or optimise token usage.",
            },
        )


class AuditChainError(WIREError):
    """Raised when AuditChain integrity verification fails."""

    error_code = "AUDIT_TAMPERED"
    docs_path = "#core-primitives"

    def __init__(self, entry_index: int, expected_hash: str, actual_hash: str) -> None:
        self.entry_index = entry_index
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        super().__init__(
            f"Audit chain integrity failure at entry {entry_index}. "
            "The audit log may have been tampered with.",
            details={
                "entry_index": entry_index,
                "expected_hash": expected_hash[:16] + "…",
                "actual_hash": actual_hash[:16] + "…",
                "suggestion": "Do not modify wire-audit.jsonl manually. "
                              "Restore from a backup or start a fresh audit chain.",
            },
        )


class AdapterNotFoundError(WIREError):
    """Raised when the requested backend adapter is not installed."""

    error_code = "ADAPTER_NOT_INSTALLED"
    docs_path = "#frameworks"

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"Backend '{backend}' adapter not installed.",
            details={
                "backend": backend,
                "suggestion": f"Run: pip install wire-ai[{backend}]",
                "available_backends": "langgraph, crewai, autogen, openai, foundry",
            },
        )
