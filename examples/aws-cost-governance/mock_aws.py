"""
mock_aws.py — Synthetic AWS cost data for the WIRE governance demo.

Generates realistic daily cost breakdowns across 5 AWS services.
Seeds anomaly injection on the date string so runs are deterministic
per day (20% chance of a spike on any given date).
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta


# ── Service baseline ranges (min, max) in USD/day ───────────────────────────

_BASELINES: dict[str, tuple[float, float]] = {
    "EC2":         (200.0,  800.0),
    "RDS":         (100.0,  400.0),
    "Lambda":      ( 10.0,   50.0),
    "S3":          ( 20.0,   80.0),
    "CloudFront":  ( 15.0,   60.0),
}

# Budget thresholds
_DAILY_BUDGET_USD  = 1_500.0
_MONTHLY_BUDGET_USD = 40_000.0
_ALERT_THRESHOLD   = 0.80   # warn at 80% of daily budget


def _rng_for_date(date_str: str) -> random.Random:
    """Deterministic RNG seeded by date string so results are stable per day."""
    seed = int(hashlib.md5(date_str.encode()).hexdigest(), 16) % (2**31)
    return random.Random(seed)


def get_cost_data(date_str: str) -> dict:
    """
    Return daily cost breakdown for a given ISO date string (YYYY-MM-DD).

    20% chance of a cost spike (2-4x multiplier) on a random service,
    determined deterministically by the date — same date, same spike.

    Returns:
        {
            "date": "2025-01-15",
            "services": {"EC2": 312.50, "RDS": 187.20, ...},
            "total_usd": 634.70,
            "has_anomaly": True,
            "anomaly_service": "EC2",   # None if no anomaly
            "anomaly_multiplier": 2.8,  # None if no anomaly
        }
    """
    rng = _rng_for_date(date_str)

    # Build baseline costs
    services: dict[str, float] = {}
    for service, (lo, hi) in _BASELINES.items():
        services[service] = round(rng.uniform(lo, hi), 2)

    # Inject anomaly with 20% probability
    has_anomaly = rng.random() < 0.20
    anomaly_service = None
    anomaly_multiplier = None

    if has_anomaly:
        anomaly_service = rng.choice(list(_BASELINES.keys()))
        anomaly_multiplier = round(rng.uniform(2.0, 4.5), 2)
        services[anomaly_service] = round(
            services[anomaly_service] * anomaly_multiplier, 2
        )

    total = round(sum(services.values()), 2)

    return {
        "date": date_str,
        "services": services,
        "total_usd": total,
        "has_anomaly": has_anomaly,
        "anomaly_service": anomaly_service,
        "anomaly_multiplier": anomaly_multiplier,
    }


def get_budget_status() -> dict:
    """
    Return current budget utilisation.

    Uses today's cost data to simulate month-to-date spend
    by multiplying today's cost by a realistic number of elapsed days.

    Returns:
        {
            "daily_budget_usd": 1500.0,
            "daily_spend_usd": 634.70,
            "daily_pct": 42.3,
            "monthly_budget_usd": 40000.0,
            "monthly_spend_usd": 12987.40,
            "monthly_pct": 32.5,
            "status": "ok" | "warning" | "breach",
        }
    """
    today = date.today().isoformat()
    today_data = get_cost_data(today)
    daily_spend = today_data["total_usd"]

    # Simulate MTD by accumulating from the 1st of the month
    today_obj = date.today()
    days_elapsed = today_obj.day
    monthly_spend = 0.0
    for offset in range(days_elapsed):
        d = (today_obj - timedelta(days=offset)).isoformat()
        monthly_spend += get_cost_data(d)["total_usd"]
    monthly_spend = round(monthly_spend, 2)

    daily_pct  = round(daily_spend  / _DAILY_BUDGET_USD   * 100, 1)
    monthly_pct = round(monthly_spend / _MONTHLY_BUDGET_USD * 100, 1)

    if daily_pct >= 100 or monthly_pct >= 100:
        status = "breach"
    elif daily_pct >= _ALERT_THRESHOLD * 100 or monthly_pct >= _ALERT_THRESHOLD * 100:
        status = "warning"
    else:
        status = "ok"

    return {
        "daily_budget_usd":   _DAILY_BUDGET_USD,
        "daily_spend_usd":    daily_spend,
        "daily_pct":          daily_pct,
        "monthly_budget_usd": _MONTHLY_BUDGET_USD,
        "monthly_spend_usd":  monthly_spend,
        "monthly_pct":        monthly_pct,
        "status":             status,
    }
