"""
Sprint 9 tests — EventStore + MetricsCollector.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from wire.core.events import EventBus, EventKind, WIREEvent
from wire.observability.event_store import EventQuery, EventStore, RunSummary
from wire.observability.metrics import MetricsCollector


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ev(kind: EventKind = EventKind.STEP_END, run_id: str = "r1",
        role: str | None = None, data: dict | None = None) -> WIREEvent:
    return WIREEvent(kind=kind, run_id=run_id, role=role, data=data or {})


async def _emit_n(store: EventStore, n: int, run_id: str = "r1") -> None:
    for i in range(n):
        await store.emit(_ev(run_id=run_id, data={"i": i}))
    await store.drain()


# ── EventStore — basic persistence ───────────────────────────────────────────

class TestEventStorePersistence:
    @pytest.mark.asyncio
    async def test_emit_persists_event(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(run_id="r1"))
        await store.drain()
        events = await store.query(EventQuery(run_id="r1"))
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_query_filters_by_run_id(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 3, "run-a")
        await _emit_n(store, 2, "run-b")
        events_a = await store.query(EventQuery(run_id="run-a"))
        events_b = await store.query(EventQuery(run_id="run-b"))
        assert len(events_a) == 3
        assert len(events_b) == 2

    @pytest.mark.asyncio
    async def test_query_filters_by_kind(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(kind=EventKind.STEP_END, run_id="r1"))
        await store.emit(_ev(kind=EventKind.WORKFORCE_END, run_id="r1"))
        await store.drain()
        steps = await store.query(EventQuery(run_id="r1", kind=EventKind.STEP_END))
        ends = await store.query(EventQuery(run_id="r1", kind=EventKind.WORKFORCE_END))
        assert len(steps) == 1
        assert len(ends) == 1

    @pytest.mark.asyncio
    async def test_query_since_seq(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 10, "r1")
        all_ev = await store.query(EventQuery(run_id="r1"))
        partial = await store.query(EventQuery(run_id="r1", since_seq=5))
        assert len(partial) < len(all_ev)

    @pytest.mark.asyncio
    async def test_query_limit(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 20, "r1")
        limited = await store.query(EventQuery(run_id="r1", limit=5))
        assert len(limited) <= 5

    @pytest.mark.asyncio
    async def test_list_runs(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        for run in ["run-x", "run-y", "run-z"]:
            await store.emit(_ev(run_id=run))
        await store.drain()
        runs = await store.list_runs()
        for run in ["run-x", "run-y", "run-z"]:
            assert run in runs

    @pytest.mark.asyncio
    async def test_replay_generator(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 5, "r1")
        replayed = []
        async for event in store.replay("r1"):
            replayed.append(event)
        assert len(replayed) == 5

    @pytest.mark.asyncio
    async def test_replay_from_checkpoint(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 10, "r1")
        all_ev = await store.query(EventQuery(run_id="r1"))
        checkpoint = all_ev[4].data.get("_seq", 5)
        replayed = []
        async for ev in store.replay("r1", from_seq=checkpoint):
            replayed.append(ev)
        assert len(replayed) < 10


# ── EventStore — vector clocks ────────────────────────────────────────────────

class TestEventStoreVectorClocks:
    @pytest.mark.asyncio
    async def test_events_tagged_with_vclock(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(run_id="r1"))
        await store.drain()
        events = await store.query(EventQuery(run_id="r1"))
        assert events[0].data.get("_vclock", 0) >= 1

    @pytest.mark.asyncio
    async def test_vclock_increments_per_run(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        for _ in range(3):
            await store.emit(_ev(run_id="r1"))
        await store.drain()
        events = await store.query(EventQuery(run_id="r1"))
        clocks = [e.data.get("_vclock", 0) for e in events]
        assert clocks == sorted(clocks)
        assert len(set(clocks)) == 3  # all distinct

    @pytest.mark.asyncio
    async def test_vclock_independent_per_run(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(run_id="run-a"))
        await store.emit(_ev(run_id="run-b"))
        await store.emit(_ev(run_id="run-a"))
        await store.drain()
        a_events = await store.query(EventQuery(run_id="run-a"))
        b_events = await store.query(EventQuery(run_id="run-b"))
        a_clocks = [e.data.get("_vclock", 0) for e in a_events]
        # run-a clock: 1, 2 — independent of run-b
        assert a_clocks[0] < a_clocks[1]
        assert b_events[0].data.get("_vclock") == 1


# ── RunSummary analytics ──────────────────────────────────────────────────────

class TestRunSummary:
    @pytest.mark.asyncio
    async def test_run_summary_counts_events(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await _emit_n(store, 7, "r1")
        summary = await store.run_summary("r1")
        assert summary.total_events == 7

    @pytest.mark.asyncio
    async def test_run_summary_kind_breakdown(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(kind=EventKind.STEP_END, run_id="r1"))
        await store.emit(_ev(kind=EventKind.STEP_END, run_id="r1"))
        await store.emit(_ev(kind=EventKind.WORKFORCE_END, run_id="r1"))
        await store.drain()
        summary = await store.run_summary("r1")
        assert summary.total_events == 3

    @pytest.mark.asyncio
    async def test_run_summary_cost_accumulates(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        await store.emit(_ev(run_id="r1", data={"cost_usd": 0.01}))
        await store.emit(_ev(run_id="r1", data={"cost_usd": 0.02}))
        await store.drain()
        summary = await store.run_summary("r1")
        assert summary.total_cost_usd == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_run_summary_empty_run(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        summary = await store.run_summary("nonexistent-run")
        assert summary.total_events == 0
        assert summary.duration_s is None


# ── Cross-run stats ───────────────────────────────────────────────────────────

class TestCrossRunStats:
    @pytest.mark.asyncio
    async def test_cross_run_stats_structure(self, tmp_path: Path) -> None:
        store = EventStore(str(tmp_path / "e.db"))
        await store._ensure_schema()
        for run in ["r1", "r2", "r3"]:
            await _emit_n(store, 3, run)
        stats = await store.cross_run_stats()
        assert "total_runs" in stats
        assert "total_events" in stats
        assert "top_event_kinds" in stats
        assert stats["total_runs"] >= 3
        assert stats["total_events"] >= 9


# ── MetricsCollector ──────────────────────────────────────────────────────────

class TestMetricsCollector:
    def test_initial_state_all_zero(self) -> None:
        m = MetricsCollector()
        assert m.runs_total.total() == 0
        assert m.cost_total.total() == 0
        assert m.sla_breaches.total() == 0

    @pytest.mark.asyncio
    async def test_attach_collects_workforce_events(self) -> None:
        bus = EventBus()
        m = MetricsCollector()
        m.attach(bus)

        await bus.emit(_ev(kind=EventKind.WORKFORCE_START, run_id="r1",
                           data={"backend": "langgraph"}))
        await bus.emit(_ev(kind=EventKind.WORKFORCE_END, run_id="r1",
                           data={"backend": "langgraph", "total_cost_usd": 0.05, "iterations": 10}))
        assert m.runs_total.get("started:langgraph") == 1
        assert m.runs_total.get("completed:langgraph") == 1

    @pytest.mark.asyncio
    async def test_collects_sla_breaches(self) -> None:
        bus = EventBus()
        m = MetricsCollector()
        m.attach(bus)

        await bus.emit(_ev(kind=EventKind.SLA_BREACH, run_id="r1",
                           role="cost_monitor",
                           data={"dimension": "response_seconds"}))
        assert m.sla_breaches.get("cost_monitor:response_seconds") == 1

    @pytest.mark.asyncio
    async def test_collects_hitl_events(self) -> None:
        bus = EventBus()
        m = MetricsCollector()
        m.attach(bus)

        await bus.emit(_ev(kind=EventKind.HITL_REQUEST, run_id="r1",
                           data={"channel": "slack"}))
        await bus.emit(_ev(kind=EventKind.HITL_RESPONSE, run_id="r1",
                           data={"action": "approve"}))
        assert m.hitl_requests.get("slack") == 1
        assert m.hitl_responses.get("approve") == 1

    @pytest.mark.asyncio
    async def test_collects_loop_breaches(self) -> None:
        bus = EventBus()
        m = MetricsCollector()
        m.attach(bus)

        await bus.emit(_ev(kind=EventKind.LOOP_BREACH, run_id="run-bad"))
        assert m.loop_breaches.total() == 1

    def test_to_prometheus_output(self) -> None:
        m = MetricsCollector()
        m.runs_total.inc("started:langgraph", 5)
        m.sla_breaches.inc("monitor:response_seconds", 2)
        output = m.to_prometheus()
        assert "wire_runs_total" in output
        assert "wire_sla_breaches_total" in output
        assert "5" in output

    def test_to_dict_structure(self) -> None:
        m = MetricsCollector()
        m.runs_total.inc("started:test")
        d = m.to_dict()
        assert "runs_total" in d
        assert "cost_total_usd" in d
        assert "sla_breaches" in d
        assert "dlq_size" in d

    def test_reset_clears_all(self) -> None:
        m = MetricsCollector()
        m.runs_total.inc("x", 100)
        m.cost_total.inc("y", 9999)
        m.reset()
        assert m.runs_total.total() == 0
        assert m.cost_total.total() == 0

    @pytest.mark.asyncio
    async def test_multiple_runs_tracked_independently(self) -> None:
        bus = EventBus()
        m = MetricsCollector()
        m.attach(bus)

        for i in range(3):
            await bus.emit(_ev(kind=EventKind.WORKFORCE_START, run_id=f"r{i}",
                               data={"backend": "langgraph"}))
            await bus.emit(_ev(kind=EventKind.WORKFORCE_END, run_id=f"r{i}",
                               data={"backend": "langgraph", "total_cost_usd": 0.10,
                                     "iterations": 5}))

        assert m.runs_total.get("started:langgraph") == 3
        assert m.runs_total.get("completed:langgraph") == 3


# ── Top-level imports ─────────────────────────────────────────────────────────

class TestTopLevelImports:
    def test_event_store_importable(self) -> None:
        import wire
        assert hasattr(wire, "EventStore")
        assert callable(wire.EventStore)

    def test_metrics_collector_importable(self) -> None:
        import wire
        assert hasattr(wire, "MetricsCollector")
        assert hasattr(wire, "wire_metrics")

    def test_run_summary_importable(self) -> None:
        import wire
        assert hasattr(wire, "RunSummary")
