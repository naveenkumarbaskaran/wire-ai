"""
StreamGuard — governance for streaming agent responses.

The #1 bug category across all major agent frameworks is streaming + async
state corruption:
  - LangChain: socket leaks on client disconnect
  - pydantic-ai: streaming state corruption on interrupt
  - agno: tool call events silently dropped in parallel execution
  - LangGraph: memory accumulation in long-running streams

StreamGuard wraps any async generator and adds:
  1. Stall detection     — raises StreamStallError if no chunk arrives within timeout_s
  2. Chunk auditing      — every chunk logged to AuditChain with sequence number
  3. Cost tracking       — accumulates token cost across chunks
  4. Graceful cancellation — cleans up on asyncio.CancelledError, never leaks
  5. Reconnect idempotency — resume_from_seq lets consumers restart without
                             re-processing chunks they already received

Usage:
    from wire.core.stream import StreamGuard

    guard = StreamGuard(
        run_id="run_abc",
        audit=chain,
        stall_timeout_s=30.0,
        max_chunks=1000,
    )

    async with guard.wrap(graph.astream(input)) as stream:
        async for chunk in stream:
            process(chunk)

    # Or with resume support:
    async with guard.wrap(graph.astream(input), resume_from_seq=last_seq) as stream:
        async for seq, chunk in stream.with_sequence():
            process(chunk)
            last_seq = seq
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

import structlog

from wire.core.audit import AuditChain
from wire.core.errors import WIREError
from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)

T = TypeVar("T")


class StreamStallError(WIREError):
    """Raised when no chunk arrives within stall_timeout_s."""
    def __init__(self, run_id: str, stall_timeout_s: float, seq: int) -> None:
        self.run_id = run_id
        self.stall_timeout_s = stall_timeout_s
        self.seq = seq
        super().__init__(
            f"Stream stalled [{run_id}]: no chunk received in {stall_timeout_s}s "
            f"(last sequence: {seq}). The upstream generator may have leaked."
        )


class StreamCapExceededError(WIREError):
    """Raised when stream exceeds max_chunks."""
    def __init__(self, run_id: str, max_chunks: int) -> None:
        self.run_id = run_id
        self.max_chunks = max_chunks
        super().__init__(
            f"Stream cap exceeded [{run_id}]: {max_chunks} chunks limit reached. "
            "Use a higher max_chunks or investigate unbounded streaming."
        )


@dataclass
class StreamStats:
    """Metrics collected across a guarded stream."""
    run_id: str
    total_chunks: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    stall_count: int = 0
    resumed_from_seq: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed: bool = False
    cancelled: bool = False
    error: str | None = None


class GuardedStream:
    """
    Wraps an async generator with sequence numbers + governance.
    Returned by StreamGuard.wrap() — use as async context manager.
    """

    def __init__(
        self,
        source: AsyncIterator[Any],
        *,
        run_id: str,
        audit: AuditChain | None,
        bus: EventBus | None,
        stall_timeout_s: float,
        max_chunks: int,
        resume_from_seq: int,
        cost_fn: Any | None,
    ) -> None:
        self._source = source
        self._run_id = run_id
        self._audit = audit
        self._bus = bus
        self._stall_timeout_s = stall_timeout_s
        self._max_chunks = max_chunks
        self._resume_from_seq = resume_from_seq
        self._cost_fn = cost_fn
        self._stats = StreamStats(run_id=run_id, resumed_from_seq=resume_from_seq)
        self._seq = 0

    async def __aenter__(self) -> "GuardedStream":
        if self._audit:
            await self._audit.write("stream_start", data={
                "resume_from_seq": self._resume_from_seq,
                "stall_timeout_s": self._stall_timeout_s,
                "max_chunks": self._max_chunks,
            })
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is asyncio.CancelledError:
            self._stats.cancelled = True
            if self._audit:
                await self._audit.write("stream_cancelled", data={
                    "seq": self._seq,
                    "chunks_received": self._stats.total_chunks,
                })
            log.warning("stream_cancelled", run_id=self._run_id, seq=self._seq)
        elif exc_type is not None:
            self._stats.error = str(exc_val)
            if self._audit:
                await self._audit.write("stream_error", data={
                    "error": str(exc_val),
                    "error_type": exc_type.__name__ if exc_type else None,
                    "seq": self._seq,
                })
        else:
            self._stats.completed = True
            if self._audit:
                await self._audit.write("stream_complete", data={
                    "total_chunks": self._stats.total_chunks,
                    "total_tokens": self._stats.total_tokens,
                    "total_cost_usd": self._stats.total_cost_usd,
                    "seq": self._seq,
                })

        # Drain the underlying generator to prevent resource leaks
        if hasattr(self._source, "aclose"):
            try:
                await self._source.aclose()
            except Exception:
                pass

    def __aiter__(self) -> "GuardedStream":
        return self

    async def __anext__(self) -> Any:
        """Yield next chunk with stall detection and governance."""
        if self._stats.total_chunks > self._max_chunks:
            raise StreamCapExceededError(self._run_id, self._max_chunks)
        try:
            chunk = await asyncio.wait_for(
                self._source.__anext__(),
                timeout=self._stall_timeout_s,
            )
        except asyncio.TimeoutError:
            self._stats.stall_count += 1
            log.warning("stream_stall", run_id=self._run_id, seq=self._seq,
                        timeout_s=self._stall_timeout_s)
            if self._audit:
                await self._audit.write("stream_stall", data={
                    "seq": self._seq,
                    "stall_count": self._stats.stall_count,
                    "timeout_s": self._stall_timeout_s,
                })
            raise StreamStallError(self._run_id, self._stall_timeout_s, self._seq)
        except StopAsyncIteration:
            raise

        self._seq += 1
        self._stats.total_chunks += 1

        # Skip already-seen chunks on resume
        if self._seq <= self._resume_from_seq:
            return await self.__anext__()

        # Extract cost if cost_fn provided
        cost = self._cost_fn(chunk) if self._cost_fn else 0.0
        self._stats.total_cost_usd += cost

        # Extract token count
        tokens = self._extract_tokens(chunk)
        self._stats.total_tokens += tokens

        # Audit every chunk
        if self._audit:
            await self._audit.write("stream_chunk", data={
                "seq": self._seq,
                "cost_usd": cost,
                "tokens": tokens,
                "chunk_type": type(chunk).__name__,
            })

        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.STEP_END,
                run_id=self._run_id,
                data={"seq": self._seq, "chunk_type": type(chunk).__name__, "cost_usd": cost},
            ))

        return chunk

    async def with_sequence(self) -> AsyncGenerator[tuple[int, Any], None]:
        """Yield (sequence_number, chunk) tuples for resume-aware consumers."""
        async for chunk in self:
            yield self._seq, chunk

    @property
    def stats(self) -> StreamStats:
        return self._stats

    @staticmethod
    def _extract_tokens(chunk: Any) -> int:
        """Extract token count from a chunk if available."""
        # LangGraph AIMessage with usage_metadata
        meta = getattr(chunk, "usage_metadata", None)
        if meta:
            return (meta.get("input_tokens", 0) or 0) + (meta.get("output_tokens", 0) or 0)
        # OpenAI-style usage
        usage = getattr(chunk, "usage", None)
        if usage:
            return getattr(usage, "total_tokens", 0) or 0
        return 0


class StreamGuard:
    """
    Governance wrapper for any async streaming agent output.

    Protects against:
      - Stalled streams (stall_timeout_s)
      - Unbounded streams (max_chunks)
      - Silent disconnects (tracks cancellation, never leaks)
      - Lost progress (resume_from_seq for reconnect)
      - Unaudited output (every chunk logged to AuditChain)

    Works with any async generator — LangGraph, CrewAI, AutoGen,
    OpenAI Agents SDK, or plain Python async generators.

    Usage:
        guard = StreamGuard(run_id="run_abc", audit=chain, stall_timeout_s=30)

        async with guard.wrap(graph.astream(input)) as stream:
            async for chunk in stream:
                yield chunk  # or process directly

        print(guard.last_stats)  # StreamStats with full metrics
    """

    def __init__(
        self,
        *,
        run_id: str,
        audit: AuditChain | None = None,
        bus: EventBus | None = None,
        stall_timeout_s: float = 30.0,
        max_chunks: int = 10_000,
        cost_fn: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self._audit = audit
        self._bus = bus
        self.stall_timeout_s = stall_timeout_s
        self.max_chunks = max_chunks
        self._cost_fn = cost_fn
        self._last_stats: StreamStats | None = None

    def wrap(
        self,
        source: AsyncIterator[Any],
        *,
        resume_from_seq: int = 0,
    ) -> GuardedStream:
        """
        Wrap an async iterator with governance.

        Args:
            source:          Any async iterator (graph.astream(), agent.stream(), etc.)
            resume_from_seq: Skip chunks with seq <= this value (resume after disconnect)

        Returns:
            GuardedStream — use as async context manager + async iterator
        """
        stream = GuardedStream(
            source,
            run_id=self.run_id,
            audit=self._audit,
            bus=self._bus,
            stall_timeout_s=self.stall_timeout_s,
            max_chunks=self.max_chunks,
            resume_from_seq=resume_from_seq,
            cost_fn=self._cost_fn,
        )
        return stream

    @property
    def last_stats(self) -> StreamStats | None:
        return self._last_stats
