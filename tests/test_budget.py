"""Tests for Budget — total, hourly, daily windows, breach errors."""

from __future__ import annotations

import pytest

from wire.core.budget import Budget
from wire.core.errors import BudgetBreachError


class TestBudgetTotal:
    def test_under_total_does_not_raise(self) -> None:
        b = Budget(max_usd=1.0)
        b.charge("r1", 0.40)
        b.charge("r1", 0.40)

    def test_over_total_raises(self) -> None:
        b = Budget(max_usd=0.50)
        b.charge("r1", 0.30)
        with pytest.raises(BudgetBreachError) as exc_info:
            b.charge("r1", 0.30)
        assert exc_info.value.window == "total"
        assert exc_info.value.spent == pytest.approx(0.60)

    def test_total_usd_property(self) -> None:
        b = Budget(max_usd=10.0)
        b.charge("r1", 0.10)
        b.charge("r1", 0.05)
        assert b.total_usd == pytest.approx(0.15)

    def test_no_limit_never_raises(self) -> None:
        b = Budget()
        for _ in range(1000):
            b.charge("r1", 100.0)


class TestBudgetHourly:
    def test_under_hourly_does_not_raise(self) -> None:
        b = Budget(hourly=1.0)
        for _ in range(10):
            b.charge("r1", 0.09)  # 0.90 total < 1.0

    def test_over_hourly_raises(self) -> None:
        b = Budget(hourly=0.50)
        b.charge("r1", 0.30)
        with pytest.raises(BudgetBreachError) as exc_info:
            b.charge("r1", 0.30)
        assert exc_info.value.window == "hourly"

    def test_breach_message_is_informative(self) -> None:
        b = Budget(hourly=0.10)
        b.charge("r1", 0.05)
        with pytest.raises(BudgetBreachError) as exc_info:
            b.charge("r1", 0.10)
        assert "hourly" in str(exc_info.value)
        assert "BUDGET_EXCEEDED" in str(exc_info.value) or "budget" in str(exc_info.value).lower()


class TestBudgetDaily:
    def test_over_daily_raises(self) -> None:
        b = Budget(daily=1.0)
        b.charge("r1", 0.60)
        with pytest.raises(BudgetBreachError) as exc_info:
            b.charge("r1", 0.60)
        assert exc_info.value.window == "daily"


class TestBudgetCombined:
    def test_most_restrictive_window_fires_first(self) -> None:
        b = Budget(max_usd=10.0, hourly=0.50, daily=5.0)
        b.charge("r1", 0.30)
        with pytest.raises(BudgetBreachError) as exc_info:
            b.charge("r1", 0.30)  # hourly breaches at 0.60
        assert exc_info.value.window == "hourly"

    def test_zero_amount_charge_is_valid(self) -> None:
        b = Budget(max_usd=1.0, hourly=0.50)
        for _ in range(100):
            b.charge("r1", 0.0)
