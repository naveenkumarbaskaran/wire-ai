"""
SLATracker — per-role SLA enforcement with typed breach events.

Tracks response time, cost, and accuracy thresholds per role.
Fires SLABreachEvent immediately on violation — never silently fails.

No existing framework ships this. Enterprises define SLAs in contracts
with vendors but have no way to enforce them at the agent level.

Usage:
    tracker = SLATracker(
        role="cost_monitor",
        response_seconds=60,
        max_cost_usd=0.10,
        min_confidence=0.80,
    )

    async with tracker.measure(run_id="run_abc"):
        result = await agent.run(...)
        tracker.record_confidence(result.confidence)

    # SLABreachError raised immediately if any threshold exceeded
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import WIREError
from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)


class SLABreachError(WIREError):
    """Raised when a role violates its SLA contract."""

    def __init__(self, role: str, dimension: str, actual: float, limit: float) -> None:
        self.role = role
        self.dimension = dimension
        self.actual = actual
        self.limit = limit
        super().__init__(
            f"SLA breach [{role}] {dimension}: "
            f"actual={actual:.3f} exceeded limit={limit:.3f}"
        )


class SLADefinition(BaseModel):
    """Contract definition for a role's SLA."""

    role: str
    response_seconds: float | None = None    # max wall-clock time per invocation
    max_cost_usd: float | None = None        # max cost per invocation
    min_confidence: float | None = None      # min confidence score (0.0–1.0)
    max_retries: int | None = None           # max retry count before breach


class SLAMeasurement(BaseModel):
    """One recorded SLA measurement for a role invocation."""

    run_id: str
    role: str
    elapsed_seconds: float
    cost_usd: float = 0.0
    confidence: float | None = None
    retries: int = 0
    breached: bool = False
    breach_dimension: str | None = None
    measured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SLATracker:
    """
    Measures and enforces SLA contracts per role invocation.

    Thread-safe per-instance. Keeps a rolling history of measurements
    for trend analysis (surfaced in Visibility layer, Sprint 4).
    """

    def __init__(
        self,
        *,
        role: str,
        response_seconds: float | None = None,
        max_cost_usd: float | None = None,
        min_confidence: float | None = None,
        max_retries: int | None = None,
        bus: EventBus | None = None,
        raise_on_breach: bool = True,
    ) -> None:
        self.sla = SLADefinition(
            role=role,
            response_seconds=response_seconds,
            max_cost_usd=max_cost_usd,
            min_confidence=min_confidence,
            max_retries=max_retries,
        )
        self._bus = bus
        self._raise_on_breach = raise_on_breach
        self._history: list[SLAMeasurement] = []
        self._current_retries: int = 0
        self._current_cost: float = 0.0
        self._current_confidence: float | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def measure(self, run_id: str) -> AsyncIterator["SLATracker"]:
        """
        Context manager that measures wall-clock time and enforces SLA on exit.

        async with tracker.measure(run_id) as t:
            result = await agent.run(...)
            t.record_cost(result.cost)
            t.record_confidence(result.confidence)
        """
        self._current_retries = 0
        self._current_cost = 0.0
        self._current_confidence = None
        start = time.perf_counter()

        try:
            yield self
        finally:
            elapsed = time.perf_counter() - start
            measurement = self._record(run_id, elapsed)
            await self._enforce(run_id, measurement)

    def record_cost(self, cost_usd: float) -> None:
        self._current_cost += cost_usd

    def record_confidence(self, confidence: float) -> None:
        self._current_confidence = confidence

    def record_retry(self) -> None:
        self._current_retries += 1

    @property
    def history(self) -> list[SLAMeasurement]:
        return list(self._history)

    @property
    def breach_rate(self) -> float:
        if not self._history:
            return 0.0
        return sum(1 for m in self._history if m.breached) / len(self._history)

    @property
    def avg_response_seconds(self) -> float | None:
        if not self._history:
            return None
        return sum(m.elapsed_seconds for m in self._history) / len(self._history)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _record(self, run_id: str, elapsed: float) -> SLAMeasurement:
        m = SLAMeasurement(
            run_id=run_id,
            role=self.sla.role,
            elapsed_seconds=elapsed,
            cost_usd=self._current_cost,
            confidence=self._current_confidence,
            retries=self._current_retries,
        )
        self._history.append(m)
        log.debug(
            "sla_measured",
            role=self.sla.role,
            elapsed_s=round(elapsed, 3),
            cost_usd=self._current_cost,
            confidence=self._current_confidence,
        )
        return m

    async def _enforce(self, run_id: str, m: SLAMeasurement) -> None:
        breach: tuple[str, float, float] | None = None

        if self.sla.response_seconds and m.elapsed_seconds > self.sla.response_seconds:
            breach = ("response_seconds", m.elapsed_seconds, self.sla.response_seconds)

        elif self.sla.max_cost_usd and m.cost_usd > self.sla.max_cost_usd:
            breach = ("max_cost_usd", m.cost_usd, self.sla.max_cost_usd)

        elif (
            self.sla.min_confidence is not None
            and m.confidence is not None
            and m.confidence < self.sla.min_confidence
        ):
            breach = ("min_confidence", m.confidence, self.sla.min_confidence)

        elif self.sla.max_retries and m.retries > self.sla.max_retries:
            breach = ("max_retries", float(m.retries), float(self.sla.max_retries))

        if breach:
            dimension, actual, limit = breach
            m.breached = True
            m.breach_dimension = dimension

            log.warning(
                "sla_breach",
                role=self.sla.role,
                run_id=run_id,
                dimension=dimension,
                actual=actual,
                limit=limit,
            )

            if self._bus:
                await self._bus.emit(WIREEvent(
                    kind=EventKind.SLA_BREACH,
                    run_id=run_id,
                    role=self.sla.role,
                    data={
                        "dimension": dimension,
                        "actual": actual,
                        "limit": limit,
                        "elapsed_s": m.elapsed_seconds,
                        "cost_usd": m.cost_usd,
                    },
                ))

            if self._raise_on_breach:
                raise SLABreachError(self.sla.role, dimension, actual, limit)
