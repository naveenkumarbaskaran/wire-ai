"""Tests for LoopGuard — iteration limits, cost limits, event emission."""

from __future__ import annotations

import pytest

from wire.core.errors import LoopBreachError
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.guard import LoopGuard


def make_guard(max_iter: int = 10, max_cost: float | None = None, bus: EventBus | None = None) -> LoopGuard:
    return LoopGuard(run_id="test-run", max_iterations=max_iter, max_cost_usd=max_cost, bus=bus)


class TestLoopGuardIterations:
    def test_under_limit_does_not_raise(self) -> None:
        guard = make_guard(max_iter=5)
        for _ in range(5):
            guard.tick()

    def test_exactly_at_limit_does_not_raise(self) -> None:
        guard = make_guard(max_iter=3)
        for _ in range(3):
            guard.tick()

    def test_over_limit_raises(self) -> None:
        guard = make_guard(max_iter=3)
        for _ in range(3):
            guard.tick()
        with pytest.raises(LoopBreachError) as exc_info:
            guard.tick()
        assert exc_info.value.iterations == 4
        assert exc_info.value.limit == 3

    def test_breach_error_message_is_informative(self) -> None:
        guard = make_guard(max_iter=1)
        guard.tick()
        with pytest.raises(LoopBreachError) as exc_info:
            guard.tick()
        assert "LOOP_BREACH" in str(exc_info.value) or "limit" in str(exc_info.value).lower()
        assert "2" in str(exc_info.value) and "1" in str(exc_info.value)

    def test_iterations_counter_increments(self) -> None:
        guard = make_guard(max_iter=10)
        guard.tick()
        guard.tick()
        assert guard.iterations == 2


class TestLoopGuardCost:
    def test_under_cost_limit_does_not_raise(self) -> None:
        guard = make_guard(max_cost=1.0)
        guard.tick(cost_usd=0.30)
        guard.tick(cost_usd=0.30)
        guard.tick(cost_usd=0.30)  # 0.90 < 1.0

    def test_over_cost_limit_raises(self) -> None:
        guard = make_guard(max_cost=0.50)
        guard.tick(cost_usd=0.30)
        with pytest.raises(LoopBreachError) as exc_info:
            guard.tick(cost_usd=0.30)  # 0.60 > 0.50
        assert exc_info.value.cost_usd == pytest.approx(0.60)

    def test_cost_accumulates_correctly(self) -> None:
        guard = make_guard(max_cost=10.0)
        guard.tick(cost_usd=0.001)
        guard.tick(cost_usd=0.002)
        assert guard.cost_usd == pytest.approx(0.003)

    def test_no_cost_limit_never_cost_breaches(self) -> None:
        guard = make_guard(max_iter=1000, max_cost=None)
        for _ in range(100):
            guard.tick(cost_usd=999.0)  # no cost limit — should never raise

    def test_zero_cost_tick_is_valid(self) -> None:
        guard = make_guard(max_iter=1000, max_cost=0.50)
        for _ in range(50):
            guard.tick(cost_usd=0.0)


class TestLoopGuardState:
    def test_returns_snapshot_not_reference(self) -> None:
        guard = make_guard()
        state1 = guard.tick()
        state2 = guard.tick()
        assert state1.iterations == 1
        assert state2.iterations == 2
        assert state1 is not state2

    def test_independent_guards_do_not_share_state(self) -> None:
        g1 = make_guard(max_iter=10)
        g2 = make_guard(max_iter=10)
        for _ in range(5):
            g1.tick()
        assert g1.iterations == 5
        assert g2.iterations == 0
