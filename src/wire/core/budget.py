"""
Budget — hard cost ceiling enforced across hourly and daily windows.

Tracks rolling spend per run. Raises BudgetBreachError immediately on ceiling breach.
Designed to be called from adapters after every LLM response where token cost is known.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel

from wire.core.errors import BudgetBreachError
from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)


class _Spend(BaseModel):
    ts: datetime
    amount_usd: float


class Budget:
    """
    Rolling budget tracker with per-window hard ceilings.

    Usage:
        budget = Budget(hourly=0.50, daily=5.00)
        budget.charge(run_id="abc", amount_usd=0.002)  # raises on breach
    """

    def __init__(
        self,
        *,
        max_usd: float | None = None,
        hourly: float | None = None,
        daily: float | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.max_usd = max_usd       # lifetime ceiling for this Budget instance
        self.hourly = hourly
        self.daily = daily
        self._bus = bus
        self._total: float = 0.0
        self._history: deque[_Spend] = deque()

    # ── Public API ────────────────────────────────────────────────────────────

    def charge(self, run_id: str, amount_usd: float) -> float:
        """
        Record a spend event. Returns total spend so far.
        Raises BudgetBreachError immediately if any ceiling is exceeded.
        """
        now = datetime.now(timezone.utc)
        self._history.append(_Spend(ts=now, amount_usd=amount_usd))
        self._total += amount_usd
        self._prune(now)

        if self.max_usd is not None and self._total > self.max_usd:
            self._breach(run_id, self._total, self.max_usd, "total")

        if self.hourly is not None:
            hourly_spend = self._window_spend(now, hours=1)
            if hourly_spend > self.hourly:
                self._breach(run_id, hourly_spend, self.hourly, "hourly")

        if self.daily is not None:
            daily_spend = self._window_spend(now, hours=24)
            if daily_spend > self.daily:
                self._breach(run_id, daily_spend, self.daily, "daily")

        log.debug("budget_charge", run_id=run_id, amount=amount_usd, total=self._total)
        return self._total

    @property
    def total_usd(self) -> float:
        return self._total

    # ── Internals ─────────────────────────────────────────────────────────────

    def _window_spend(self, now: datetime, *, hours: int) -> float:
        from datetime import timedelta
        cutoff = now - timedelta(hours=hours)
        return sum(s.amount_usd for s in self._history if s.ts >= cutoff)

    def _prune(self, now: datetime) -> None:
        # Retain at least max(hourly*2, daily+1h, 25h) to cover all windows
        from datetime import timedelta
        retention_h = max(25, (self.daily or 0) and 25)
        cutoff = now - timedelta(hours=retention_h)
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()

    def _breach(self, run_id: str, spent: float, limit: float, window: str) -> None:
        event = WIREEvent(
            kind=EventKind.BUDGET_BREACH,
            run_id=run_id,
            data={"spent": spent, "limit": limit, "window": window},
        )
        if self._bus:
            # Schedule event emission on the running loop if available.
            # Never block — breach error must always raise immediately.
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._bus.emit(event))
            except RuntimeError:
                pass  # No running loop — skip emission, still raise

        log.error("budget_breach", run_id=run_id, spent=spent, limit=limit, window=window)
        raise BudgetBreachError(spent, limit, window)
