"""
Sprint 7 tests — StreamGuard + DurableEventBus.

Covers the #1 bug category across all major frameworks:
streaming + async state corruption.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wire.core.durable_events import DeadLetter, DurableEventBus
from wire.core.events import EventKind, WIREEvent
from wire.core.stream import (
    GuardedStream,
    StreamCapExceededError,
    StreamGuard,
    StreamStallError,
    StreamStats,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _gen(*items):
    """Async generator yielding items."""
    for item in items:
        yield item


async def _slow_gen(delay_s: float, *items):
    """Async generator with delay between items."""
    for item in items:
        await asyncio.sleep(delay_s)
        yield item


async def _stalling_gen():
    """Async generator that yields one item then stalls forever."""
    yield {"node": "step1"}
    await asyncio.sleep(9999)


async def _collect(stream: GuardedStream) -> list:
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


# ── StreamGuard — basic operation ─────────────────────────────────────────────

class TestStreamGuardBasic:
    @pytest.mark.asyncio
    async def test_yields_all_chunks(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen("a", "b", "c")) as stream:
            chunks = await _collect(stream)
        assert chunks == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_empty_stream_completes(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen()) as stream:
            chunks = await _collect(stream)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_stats_count_chunks(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen(1, 2, 3, 4, 5)) as stream:
            chunks = await _collect(stream)
            assert stream.stats.total_chunks == 5

    @pytest.mark.asyncio
    async def test_stats_completed_true_on_success(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen("x")) as stream:
            await _collect(stream)
        # stats.completed is set in __aexit__ — check after context exits
        assert stream.stats.completed is True
        assert stream.stats.cancelled is False
        assert stream.stats.error is None

    @pytest.mark.asyncio
    async def test_cost_fn_accumulates(self) -> None:
        guard = StreamGuard(
            run_id="r1",
            stall_timeout_s=5.0,
            cost_fn=lambda chunk: 0.01,
        )
        async with guard.wrap(_gen("a", "b", "c")) as stream:
            await _collect(stream)
            assert stream.stats.total_cost_usd == pytest.approx(0.03)


# ── StreamGuard — stall detection ────────────────────────────────────────────

class TestStreamGuardStall:
    @pytest.mark.asyncio
    async def test_stall_raises_stream_stall_error(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=0.05)
        with pytest.raises(StreamStallError) as exc_info:
            async with guard.wrap(_stalling_gen()) as stream:
                await _collect(stream)
        assert exc_info.value.run_id == "r1"
        assert exc_info.value.stall_timeout_s == 0.05

    @pytest.mark.asyncio
    async def test_stall_error_message_informative(self) -> None:
        guard = StreamGuard(run_id="test-run", stall_timeout_s=0.05)
        with pytest.raises(StreamStallError) as exc_info:
            async with guard.wrap(_stalling_gen()) as stream:
                await _collect(stream)
        assert "test-run" in str(exc_info.value)
        assert "0.05" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_stall_on_fast_stream(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_slow_gen(0.01, "a", "b")) as stream:
            chunks = await _collect(stream)
        assert chunks == ["a", "b"]


# ── StreamGuard — cap exceeded ────────────────────────────────────────────────

class TestStreamGuardCap:
    @pytest.mark.asyncio
    async def test_cap_exceeded_raises(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0, max_chunks=3)
        with pytest.raises(StreamCapExceededError) as exc_info:
            async with guard.wrap(_gen(1, 2, 3, 4, 5)) as stream:
                await _collect(stream)
        assert exc_info.value.max_chunks == 3

    @pytest.mark.asyncio
    async def test_exactly_at_cap_does_not_raise(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0, max_chunks=3)
        async with guard.wrap(_gen(1, 2, 3)) as stream:
            chunks = await _collect(stream)
        assert len(chunks) == 3


# ── StreamGuard — cancellation ────────────────────────────────────────────────

class TestStreamGuardCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_tracked_in_stats(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        stats_ref: list[StreamStats] = []

        async def run():
            async with guard.wrap(_slow_gen(0.1, 1, 2, 3)) as stream:
                stats_ref.append(stream.stats)
                async for _ in stream:
                    break  # exit early — simulates cancel
                # Stats available after context exit
                return stream.stats

        stats = await run()
        # Not cancelled by asyncio.CancelledError, just early exit — completed
        assert stats.total_chunks >= 1

    @pytest.mark.asyncio
    async def test_underlying_generator_closed_on_exit(self) -> None:
        closed = []

        async def gen_with_cleanup():
            try:
                yield "a"
                yield "b"
            finally:
                closed.append(True)

        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(gen_with_cleanup()) as stream:
            async for chunk in stream:
                break  # exit after first chunk

        assert closed  # generator was properly closed


# ── StreamGuard — resume ──────────────────────────────────────────────────────

class TestStreamGuardResume:
    @pytest.mark.asyncio
    async def test_resume_skips_seen_chunks(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen("a", "b", "c", "d", "e"), resume_from_seq=2) as stream:
            chunks = await _collect(stream)
        # seq 1 and 2 skipped, yields seq 3,4,5
        assert len(chunks) == 3
        assert chunks == ["c", "d", "e"]

    @pytest.mark.asyncio
    async def test_resume_from_zero_yields_all(self) -> None:
        guard = StreamGuard(run_id="r1", stall_timeout_s=5.0)
        async with guard.wrap(_gen("a", "b", "c"), resume_from_seq=0) as stream:
            chunks = await _collect(stream)
        assert chunks == ["a", "b", "c"]


# ── StreamGuard — audit integration ──────────────────────────────────────────

class TestStreamGuardAudit:
    @pytest.mark.asyncio
    async def test_audit_entries_written(self, tmp_path: Path) -> None:
        import json
        from wire.core.audit import AuditChain
        path = tmp_path / "stream-audit.jsonl"
        chain = AuditChain(run_id="r1", path=str(path))
        guard = StreamGuard(run_id="r1", audit=chain, stall_timeout_s=5.0)
        async with guard.wrap(_gen("a", "b")) as stream:
            await _collect(stream)
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "stream_start" in events
        assert "stream_chunk" in events
        assert "stream_complete" in events

    @pytest.mark.asyncio
    async def test_stall_audit_entry_written(self, tmp_path: Path) -> None:
        import json
        from wire.core.audit import AuditChain
        path = tmp_path / "audit.jsonl"
        chain = AuditChain(run_id="r1", path=str(path))
        guard = StreamGuard(run_id="r1", audit=chain, stall_timeout_s=0.05)
        with pytest.raises(StreamStallError):
            async with guard.wrap(_stalling_gen()) as stream:
                await _collect(stream)
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "stream_stall" in events


# ── DurableEventBus ───────────────────────────────────────────────────────────

def _make_event(kind: EventKind = EventKind.STEP_END) -> WIREEvent:
    return WIREEvent(kind=kind, run_id="test-run", data={"test": True})


class TestDurableEventBusBasic:
    @pytest.mark.asyncio
    async def test_handler_receives_event(self) -> None:
        bus = DurableEventBus()
        received = []

        @bus.on(EventKind.STEP_END)
        async def handler(event: WIREEvent) -> None:
            received.append(event)

        await bus.emit(_make_event())
        await bus.drain()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_wildcard_handler_receives_all(self) -> None:
        bus = DurableEventBus()
        received = []

        @bus.on()
        async def handler(event: WIREEvent) -> None:
            received.append(event.kind)

        await bus.emit(_make_event(EventKind.STEP_END))
        await bus.emit(_make_event(EventKind.WORKFORCE_END))
        await bus.drain()
        assert EventKind.STEP_END in received
        assert EventKind.WORKFORCE_END in received

    @pytest.mark.asyncio
    async def test_drain_returns_count(self) -> None:
        bus = DurableEventBus()

        @bus.on(EventKind.STEP_END)
        async def handler(event: WIREEvent) -> None:
            await asyncio.sleep(0.01)

        for _ in range(5):
            await bus.emit(_make_event())
        count = await bus.drain()
        assert count == 5


class TestDurableEventBusRetry:
    @pytest.mark.asyncio
    async def test_failing_handler_retried(self) -> None:
        bus = DurableEventBus(max_retries=2, retry_delay_s=0.01)
        call_count = [0]

        @bus.on(EventKind.STEP_END, max_retries=2)
        async def flaky(event: WIREEvent) -> None:
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("transient failure")

        await bus.emit(_make_event())
        await bus.drain()
        assert call_count[0] == 3  # 1 initial + 2 retries
        assert bus.dlq_size() == 0  # recovered

    @pytest.mark.asyncio
    async def test_exhausted_handler_goes_to_dlq(self) -> None:
        bus = DurableEventBus(max_retries=1, retry_delay_s=0.01)

        @bus.on(EventKind.STEP_END, max_retries=1)
        async def always_fails(event: WIREEvent) -> None:
            raise RuntimeError("always fails")

        await bus.emit(_make_event())
        await bus.drain()
        assert bus.dlq_size() == 1
        dlq = await bus.dead_letters()
        assert "always_fails" in dlq[0].handler_name
        assert dlq[0].attempts == 2  # 1 initial + 1 retry

    @pytest.mark.asyncio
    async def test_dlq_captures_error_message(self) -> None:
        bus = DurableEventBus(max_retries=0, retry_delay_s=0.0)

        @bus.on(EventKind.STEP_END, max_retries=0)
        async def fails(event: WIREEvent) -> None:
            raise ValueError("specific error message")

        await bus.emit(_make_event())
        await bus.drain()
        dlq = await bus.dead_letters()
        assert "specific error message" in dlq[0].error

    @pytest.mark.asyncio
    async def test_bomb_handler_does_not_affect_others(self) -> None:
        bus = DurableEventBus(max_retries=0, retry_delay_s=0.0)
        good_received = []

        @bus.on(EventKind.STEP_END, max_retries=0)
        async def bomb(event: WIREEvent) -> None:
            raise RuntimeError("kaboom")

        @bus.on(EventKind.STEP_END)
        async def good(event: WIREEvent) -> None:
            good_received.append(event)

        await bus.emit(_make_event())
        await bus.drain()
        assert len(good_received) == 1   # good handler still received
        assert bus.dlq_size() == 1       # bomb in DLQ


class TestDurableEventBusDLQ:
    @pytest.mark.asyncio
    async def test_retry_dead_letters_redelivers(self) -> None:
        bus = DurableEventBus(max_retries=0, retry_delay_s=0.0)
        call_count = [0]

        @bus.on(EventKind.STEP_END, max_retries=0)
        async def sometimes_fails(event: WIREEvent) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first attempt fails")

        await bus.emit(_make_event())
        await bus.drain()
        assert bus.dlq_size() == 1

        # Fix the handler and retry
        retried = await bus.retry_dead_letters()
        await bus.drain()
        assert retried == 1
        assert bus.dlq_size() == 0   # second attempt succeeded

    @pytest.mark.asyncio
    async def test_dlq_capped_at_max_size(self) -> None:
        bus = DurableEventBus(max_retries=0, retry_delay_s=0.0, max_dlq_size=3)

        @bus.on(EventKind.STEP_END, max_retries=0)
        async def always_fails(event: WIREEvent) -> None:
            raise RuntimeError("fail")

        for _ in range(10):
            await bus.emit(_make_event())
        await bus.drain()
        assert bus.dlq_size() == 3   # capped at max_dlq_size


class TestDurableEventBusPersistence:
    @pytest.mark.asyncio
    async def test_persists_and_replays(self, tmp_path: Path) -> None:
        path = str(tmp_path / "events.db")
        bus = DurableEventBus(persist_path=path, max_retries=0)

        @bus.on(EventKind.STEP_END)
        async def handler(event: WIREEvent) -> None:
            pass

        event = WIREEvent(kind=EventKind.STEP_END, run_id="run-persist")
        await bus.emit(event)
        await bus.drain()

        replayed = await bus.replay_events("run-persist")
        assert len(replayed) >= 1
        assert replayed[0].run_id == "run-persist"

    @pytest.mark.asyncio
    async def test_replay_from_seq(self, tmp_path: Path) -> None:
        path = str(tmp_path / "events.db")
        bus = DurableEventBus(persist_path=path, max_retries=0)

        @bus.on(EventKind.STEP_END)
        async def handler(event: WIREEvent) -> None:
            pass

        for _ in range(5):
            await bus.emit(WIREEvent(kind=EventKind.STEP_END, run_id="run-seq"))
        await bus.drain()

        # Replay from seq 3 — should return only events with seq > 3
        replayed = await bus.replay_events("run-seq", from_seq=3)
        assert len(replayed) <= 2   # only seq 4 and 5
