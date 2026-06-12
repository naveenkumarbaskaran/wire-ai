"""WIRE exception hierarchy."""

from __future__ import annotations


class WIREError(Exception):
    """Base class for all WIRE errors."""


class LoopBreachError(WIREError):
    """Raised when LoopGuard detects a runaway agent loop."""

    def __init__(self, iterations: int, limit: int, cost_usd: float) -> None:
        self.iterations = iterations
        self.limit = limit
        self.cost_usd = cost_usd
        super().__init__(
            f"Loop limit reached: {iterations}/{limit} iterations, "
            f"${cost_usd:.4f} spent. Halting to prevent runaway execution."
        )


class BudgetBreachError(WIREError):
    """Raised when a Budget ceiling is exceeded."""

    def __init__(self, spent: float, limit: float, window: str) -> None:
        self.spent = spent
        self.limit = limit
        self.window = window
        super().__init__(
            f"Budget breached [{window}]: ${spent:.4f} spent, limit ${limit:.4f}. "
            "Halting workforce to prevent unbounded cost."
        )


class AuditChainError(WIREError):
    """Raised when AuditChain integrity verification fails."""

    def __init__(self, entry_index: int, expected_hash: str, actual_hash: str) -> None:
        self.entry_index = entry_index
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        super().__init__(
            f"Audit chain integrity failure at entry {entry_index}. "
            f"Expected hash {expected_hash[:12]}… got {actual_hash[:12]}… "
            "Chain may have been tampered with."
        )


class AdapterNotFoundError(WIREError):
    """Raised when the requested backend adapter is not installed."""

    def __init__(self, backend: str) -> None:
        super().__init__(
            f"Backend '{backend}' adapter not installed. "
            f"Run: pip install wire-ai[{backend}]"
        )
