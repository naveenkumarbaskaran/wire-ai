"""
MetricsCollector — Prometheus-compatible metrics for WIRE workforces.

Collects and exposes:
  - wire_runs_total             (counter, by backend/status)
  - wire_run_duration_seconds   (histogram, by backend)
  - wire_cost_usd_total         (counter, by run_id/role)
  - wire_iterations_total       (counter, by run_id)
  - wire_sla_breaches_total     (counter, by role/dimension)
  - wire_hitl_requests_total    (counter, by channel/action)
  - wire_dlq_size               (gauge)
  - wire_stream_stalls_total    (counter, by run_id)

Exposes /metrics endpoint via FastAPI (optional) or as a dict
for embedding in the web dashboard.

Usage:
    from wire.observability.metrics import MetricsCollector, wire_metrics

    # Subscribe to EventBus
    collector = wire_metrics  # global instance
    collector.attach(bus)     # auto-collects from all events

    # Export Prometheus text
    print(collector.to_prometheus())

    # Export as dict for dashboard
    data = collector.to_dict()
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)


@dataclass
class Counter:
    """Simple counter metric."""
    name: str
    help: str
    labels: dict[str, int] = field(default_factory=dict)

    def inc(self, label: str = "", amount: int = 1) -> None:
        self.labels[label] = self.labels.get(label, 0) + amount

    def get(self, label: str = "") -> int:
        return self.labels.get(label, 0)

    def total(self) -> int:
        return sum(self.labels.values())

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for label, value in self.labels.items():
            if label:
                lines.append(f'{self.name}{{label="{label}"}} {value}')
            else:
                lines.append(f"{self.name} {value}")
        return "\n".join(lines)


@dataclass
class Gauge:
    """Simple gauge metric."""
    name: str
    help: str
    value: float = 0.0

    def set(self, v: float) -> None:
        self.value = v

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount

    def to_prometheus(self) -> str:
        return (
            f"# HELP {self.name} {self.help}\n"
            f"# TYPE {self.name} gauge\n"
            f"{self.name} {self.value}"
        )


@dataclass
class Histogram:
    """Simple histogram metric (sum + count, no buckets for simplicity)."""
    name: str
    help: str
    _sum: dict[str, float] = field(default_factory=dict)
    _count: dict[str, int] = field(default_factory=dict)

    def observe(self, value: float, label: str = "") -> None:
        self._sum[label] = self._sum.get(label, 0.0) + value
        self._count[label] = self._count.get(label, 0) + 1

    def mean(self, label: str = "") -> float:
        c = self._count.get(label, 0)
        return self._sum.get(label, 0.0) / c if c > 0 else 0.0

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for label in set(list(self._sum.keys()) + list(self._count.keys())):
            suffix = f'{{label="{label}"}}' if label else ""
            lines.append(f"{self.name}_sum{suffix} {self._sum.get(label, 0.0)}")
            lines.append(f"{self.name}_count{suffix} {self._count.get(label, 0)}")
        return "\n".join(lines)


class MetricsCollector:
    """
    Collects WIRE runtime metrics from EventBus events.

    Thread-safe for single-process use.
    Exposes Prometheus text format and dict for dashboards.
    """

    def __init__(self) -> None:
        self.runs_total = Counter(
            "wire_runs_total",
            "Total workforce runs by backend and status",
        )
        self.run_duration = Histogram(
            "wire_run_duration_seconds",
            "Workforce run duration in seconds",
        )
        self.cost_total = Counter(
            "wire_cost_usd_micro",
            "Total cost in micro-USD (multiply by 1e-6 for USD)",
        )
        self.iterations_total = Counter(
            "wire_iterations_total",
            "Total agent iterations",
        )
        self.sla_breaches = Counter(
            "wire_sla_breaches_total",
            "SLA breaches by role and dimension",
        )
        self.hitl_requests = Counter(
            "wire_hitl_requests_total",
            "HITL requests by channel",
        )
        self.hitl_responses = Counter(
            "wire_hitl_responses_total",
            "HITL responses by action",
        )
        self.dlq_size = Gauge(
            "wire_dlq_size",
            "Current dead-letter queue size",
        )
        self.stream_stalls = Counter(
            "wire_stream_stalls_total",
            "Stream stall events detected",
        )
        self.loop_breaches = Counter(
            "wire_loop_breaches_total",
            "Loop guard breaches by run",
        )
        self.budget_breaches = Counter(
            "wire_budget_breaches_total",
            "Budget ceiling breaches",
        )
        self._run_start_times: dict[str, float] = {}

    def attach(self, bus: EventBus) -> "MetricsCollector":
        """Subscribe to all events from an EventBus."""

        @bus.on(EventKind.WORKFORCE_START)
        async def on_start(event: WIREEvent) -> None:
            self._run_start_times[event.run_id] = time.monotonic()
            backend = event.data.get("backend", "unknown")
            self.runs_total.inc(f"started:{backend}")

        @bus.on(EventKind.WORKFORCE_END)
        async def on_end(event: WIREEvent) -> None:
            backend = event.data.get("backend", "unknown")
            self.runs_total.inc(f"completed:{backend}")
            start = self._run_start_times.pop(event.run_id, None)
            if start:
                duration = time.monotonic() - start
                self.run_duration.observe(duration, backend)
            cost = float(event.data.get("total_cost_usd", 0) or 0)
            self.cost_total.inc("total", int(cost * 1_000_000))
            iters = int(event.data.get("iterations", 0) or 0)
            self.iterations_total.inc("total", iters)

        @bus.on(EventKind.SLA_BREACH)
        async def on_sla(event: WIREEvent) -> None:
            role = event.role or "unknown"
            dim = event.data.get("dimension", "unknown")
            self.sla_breaches.inc(f"{role}:{dim}")

        @bus.on(EventKind.HITL_REQUEST)
        async def on_hitl_req(event: WIREEvent) -> None:
            channel = str(event.data.get("channel", "cli"))
            self.hitl_requests.inc(channel)

        @bus.on(EventKind.HITL_RESPONSE)
        async def on_hitl_resp(event: WIREEvent) -> None:
            action = str(event.data.get("action", "unknown"))
            self.hitl_responses.inc(action)

        @bus.on(EventKind.LOOP_BREACH)
        async def on_loop(event: WIREEvent) -> None:
            self.loop_breaches.inc(event.run_id)

        @bus.on(EventKind.BUDGET_BREACH)
        async def on_budget(event: WIREEvent) -> None:
            window = event.data.get("window", "unknown")
            self.budget_breaches.inc(window)

        return self

    def to_prometheus(self) -> str:
        """Export all metrics in Prometheus text format."""
        sections = [
            self.runs_total.to_prometheus(),
            self.run_duration.to_prometheus(),
            self.cost_total.to_prometheus(),
            self.iterations_total.to_prometheus(),
            self.sla_breaches.to_prometheus(),
            self.hitl_requests.to_prometheus(),
            self.hitl_responses.to_prometheus(),
            self.dlq_size.to_prometheus(),
            self.stream_stalls.to_prometheus(),
            self.loop_breaches.to_prometheus(),
            self.budget_breaches.to_prometheus(),
        ]
        return "\n\n".join(s for s in sections if s.strip()) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Export as dict for dashboard embedding."""
        return {
            "runs_total": self.runs_total.labels,
            "run_duration_mean_s": {
                label: self.run_duration.mean(label)
                for label in self.run_duration._count
            },
            "cost_total_usd": self.cost_total.total() / 1_000_000,
            "iterations_total": self.iterations_total.total(),
            "sla_breaches": self.sla_breaches.labels,
            "hitl_requests": self.hitl_requests.labels,
            "hitl_responses": self.hitl_responses.labels,
            "dlq_size": self.dlq_size.value,
            "stream_stalls": self.stream_stalls.total(),
            "loop_breaches": self.loop_breaches.total(),
            "budget_breaches": self.budget_breaches.total(),
        }

    def reset(self) -> None:
        """Reset all metrics — useful between test runs."""
        self.__init__()


# Module-level global collector — attach to any EventBus with wire_metrics.attach(bus)
wire_metrics = MetricsCollector()
