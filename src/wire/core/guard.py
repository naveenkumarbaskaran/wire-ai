"""
LoopGuard — prevents runaway agent loops.

Tracks iteration count and cumulative cost per run.
Raises LoopBreachError before the loop can exhaust API quota or budget.
Emits a LOOP_BREACH event to the EventBus for downstream handlers (alerting, logging).
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import LoopBreachError
from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)


class LoopGuardState(BaseModel):
    iterations: int = 0
    cost_usd: float = 0.0


class LoopGuard:
    """
    Stateful per-run loop guard.

    Usage (framework-agnostic):
        guard = LoopGuard(run_id="abc", max_iterations=50, max_cost_usd=1.00)
        for step in agent_steps:
            guard.tick(cost_usd=step.cost)   # raises LoopBreachError if limits hit
    """

    def __init__(
        self,
        run_id: str,
        max_iterations: int = 50,
        max_cost_usd: float | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.run_id = run_id
        self.max_iterations = max_iterations
        self.max_cost_usd = max_cost_usd
        self._bus = bus
        self._state = LoopGuardState()

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self, cost_usd: float = 0.0) -> LoopGuardState:
        """
        Record one iteration. Call once per agent step / graph node execution.
        Raises LoopBreachError immediately when either limit is exceeded.
        """
        self._state.iterations += 1
        self._state.cost_usd += cost_usd

        if self._state.iterations > self.max_iterations:
            self._breach()

        if self.max_cost_usd is not None and self._state.cost_usd > self.max_cost_usd:
            self._breach()

        log.debug(
            "loop_tick",
            run_id=self.run_id,
            iteration=self._state.iterations,
            cost_usd=round(self._state.cost_usd, 6),
        )
        return LoopGuardState(**self._state.model_dump())

    @property
    def iterations(self) -> int:
        return self._state.iterations

    @property
    def cost_usd(self) -> float:
        return self._state.cost_usd

    # ── Internals ─────────────────────────────────────────────────────────────

    def _breach(self) -> None:
        event = WIREEvent(
            kind=EventKind.LOOP_BREACH,
            run_id=self.run_id,
            data={
                "iterations": self._state.iterations,
                "max_iterations": self.max_iterations,
                "cost_usd": self._state.cost_usd,
                "max_cost_usd": self.max_cost_usd,
            },
        )
        if self._bus:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._bus.emit(event))
            except RuntimeError:
                pass  # No running loop — skip emission, still raise

        log.error(
            "loop_breach",
            run_id=self.run_id,
            iterations=self._state.iterations,
            cost_usd=self._state.cost_usd,
        )
        raise LoopBreachError(
            self._state.iterations,
            self.max_iterations,
            self._state.cost_usd,
        )
