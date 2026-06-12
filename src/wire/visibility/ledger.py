"""
CostLedger — real-time per-agent, per-role, per-task cost tracking.

Delegates token counting to tokmon-compatible interface.
Provides rolling windows, per-role breakdowns, and budget progress.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class LedgerEntry:
    run_id: str
    role: str
    tool: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CostLedger:
    """
    Real-time cost tracker — per-role, per-run, per-model.

    Usage:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="cost_monitor",
                      tokens_in=500, tokens_out=200, model="claude-haiku-4-5-20251001")
        ledger.total_usd          # → float
        ledger.by_role()          # → {"cost_monitor": 0.0023, ...}
        ledger.by_run("r1")       # → 0.0023
    """

    # Approximate pricing per 1M tokens (input/output) — updated for Claude family
    _PRICING: dict[str, tuple[float, float]] = {
        "claude-haiku-4-5-20251001":  (0.80,  4.00),
        "claude-sonnet-4-6":          (3.00, 15.00),
        "claude-opus-4-8":           (15.00, 75.00),
        "gpt-4o":                     (5.00, 15.00),
        "gpt-4o-mini":                (0.15,  0.60),
        "default":                    (3.00, 15.00),
    }

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._by_role: dict[str, float] = defaultdict(float)
        self._by_run: dict[str, float] = defaultdict(float)

    def record(
        self,
        *,
        run_id: str,
        role: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str = "default",
        tool: str | None = None,
        cost_usd: float | None = None,
    ) -> float:
        """Record a spend event. Returns cost for this call."""
        if cost_usd is None:
            pricing = self._PRICING.get(model, self._PRICING["default"])
            cost_usd = (tokens_in * pricing[0] + tokens_out * pricing[1]) / 1_000_000

        entry = LedgerEntry(
            run_id=run_id, role=role, tool=tool,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd, model=model,
        )
        self._entries.append(entry)
        self._by_role[role] += cost_usd
        self._by_run[run_id] += cost_usd
        return cost_usd

    @property
    def total_usd(self) -> float:
        return sum(e.cost_usd for e in self._entries)

    def by_role(self) -> dict[str, float]:
        return dict(self._by_role)

    def by_run(self, run_id: str) -> float:
        return self._by_run.get(run_id, 0.0)

    def entries(self, run_id: str | None = None) -> list[LedgerEntry]:
        if run_id:
            return [e for e in self._entries if e.run_id == run_id]
        return list(self._entries)

    def summary(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_usd, 6),
            "by_role": {k: round(v, 6) for k, v in self._by_role.items()},
            "entry_count": len(self._entries),
        }
