"""Tests for SLATracker — response time, cost, confidence, retry breach."""

from __future__ import annotations

import asyncio

import pytest

from wire.core.sla import SLABreachError, SLATracker


class TestSLAResponseTime:
    @pytest.mark.asyncio
    async def test_under_response_limit_does_not_raise(self) -> None:
        tracker = SLATracker(role="agent", response_seconds=10.0)
        async with tracker.measure("r1"):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_over_response_limit_raises(self) -> None:
        tracker = SLATracker(role="agent", response_seconds=0.001)
        with pytest.raises(SLABreachError) as exc_info:
            async with tracker.measure("r1"):
                await asyncio.sleep(0.05)
        assert exc_info.value.dimension == "response_seconds"
        assert exc_info.value.role == "agent"

    @pytest.mark.asyncio
    async def test_no_response_limit_never_raises_on_time(self) -> None:
        tracker = SLATracker(role="agent")
        async with tracker.measure("r1"):
            await asyncio.sleep(0.01)


class TestSLACost:
    @pytest.mark.asyncio
    async def test_under_cost_limit_does_not_raise(self) -> None:
        tracker = SLATracker(role="agent", max_cost_usd=1.0)
        async with tracker.measure("r1") as t:
            t.record_cost(0.50)

    @pytest.mark.asyncio
    async def test_over_cost_limit_raises(self) -> None:
        tracker = SLATracker(role="agent", max_cost_usd=0.10)
        with pytest.raises(SLABreachError) as exc_info:
            async with tracker.measure("r1") as t:
                t.record_cost(0.20)
        assert exc_info.value.dimension == "max_cost_usd"

    @pytest.mark.asyncio
    async def test_accumulated_cost_checked(self) -> None:
        tracker = SLATracker(role="agent", max_cost_usd=0.10)
        with pytest.raises(SLABreachError):
            async with tracker.measure("r1") as t:
                t.record_cost(0.06)
                t.record_cost(0.06)  # total 0.12 > 0.10


class TestSLAConfidence:
    @pytest.mark.asyncio
    async def test_above_min_confidence_does_not_raise(self) -> None:
        tracker = SLATracker(role="agent", min_confidence=0.80)
        async with tracker.measure("r1") as t:
            t.record_confidence(0.95)

    @pytest.mark.asyncio
    async def test_below_min_confidence_raises(self) -> None:
        tracker = SLATracker(role="agent", min_confidence=0.80)
        with pytest.raises(SLABreachError) as exc_info:
            async with tracker.measure("r1") as t:
                t.record_confidence(0.60)
        assert exc_info.value.dimension == "min_confidence"

    @pytest.mark.asyncio
    async def test_no_confidence_recorded_skips_check(self) -> None:
        tracker = SLATracker(role="agent", min_confidence=0.80)
        async with tracker.measure("r1"):
            pass  # no confidence recorded — should not raise


class TestSLARetries:
    @pytest.mark.asyncio
    async def test_under_retry_limit_does_not_raise(self) -> None:
        tracker = SLATracker(role="agent", max_retries=3)
        async with tracker.measure("r1") as t:
            t.record_retry()
            t.record_retry()

    @pytest.mark.asyncio
    async def test_over_retry_limit_raises(self) -> None:
        tracker = SLATracker(role="agent", max_retries=2)
        with pytest.raises(SLABreachError) as exc_info:
            async with tracker.measure("r1") as t:
                for _ in range(3):
                    t.record_retry()
        assert exc_info.value.dimension == "max_retries"


class TestSLAHistory:
    @pytest.mark.asyncio
    async def test_history_records_measurements(self) -> None:
        tracker = SLATracker(role="agent", response_seconds=10.0)
        for _ in range(3):
            async with tracker.measure("r1"):
                pass
        assert len(tracker.history) == 3

    @pytest.mark.asyncio
    async def test_breach_rate_calculated(self) -> None:
        tracker = SLATracker(role="agent", max_cost_usd=0.10, raise_on_breach=False)
        async with tracker.measure("r1") as t:
            t.record_cost(0.05)  # ok
        async with tracker.measure("r1") as t:
            t.record_cost(0.20)  # breach
        assert tracker.breach_rate == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_no_raise_mode_records_without_raising(self) -> None:
        tracker = SLATracker(role="agent", max_cost_usd=0.01, raise_on_breach=False)
        async with tracker.measure("r1") as t:
            t.record_cost(1.0)  # breach, but no raise
        assert tracker.history[0].breached is True
        assert tracker.history[0].breach_dimension == "max_cost_usd"

    @pytest.mark.asyncio
    async def test_breach_error_message_is_informative(self) -> None:
        tracker = SLATracker(role="cost_monitor", max_cost_usd=0.10)
        with pytest.raises(SLABreachError) as exc_info:
            async with tracker.measure("r1") as t:
                t.record_cost(0.50)
        assert "cost_monitor" in str(exc_info.value)
        assert "max_cost_usd" in str(exc_info.value)
