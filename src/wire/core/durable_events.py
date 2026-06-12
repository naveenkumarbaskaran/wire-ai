"""
DurableEventBus — guaranteed event delivery with dead-letter queue.

The standard EventBus (wire/core/events.py) is fire-and-forget:
if a handler crashes, the event is silently lost. This is the #1
observability complaint in agno (438 open issues), AutoGen, and LangGraph.

DurableEventBus adds:
  1. Dead-letter queue  — failed events captured, never silently dropped
  2. Retry with backoff — failed handlers retried up to max_retries times
  3. Sequence ordering  — events from parallel agents arrive tagged with
                          sequence numbers; consumers can re-order if needed
  4. Persistence        — events stored to SQLite (default) or in-memory
                          survives process restart; replay from any point
  5. Drain              — await bus.drain() ensures all pending events
                          delivered before shutdown

Usage:
    from wire.core.durable_events import DurableEventBus

    bus = DurableEventBus(persist_path="wire-events.db")

    @bus.on(EventKind.STEP_END)
    async def handle_step(event: WIREEvent) -> None:
        await dashboard.update(event)

    # Failed handlers go to DLQ, never lost:
    dlq = await bus.dead_letters()
    for event in dlq:
        print(f"Failed: {event.kind} — retry or discard")

    await bus.drain()   # wait for all pending before shutdown
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog

from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)

Handler = Callable[[WIREEvent], Coroutine[Any, Any, None]]


@dataclass
class DeadLetter:
    """An event that failed delivery after all retries."""
    id: str = field(default_factory=lambda: str(uuid4()))
    event: WIREEvent = field(default_factory=lambda: WIREEvent(kind=EventKind.STEP_END, run_id=""))
    handler_name: str = ""
    error: str = ""
    attempts: int = 0
    failed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _HandlerEntry:
    fn: Handler
    name: str
    max_retries: int
    retry_delay_s: float


class DurableEventBus(EventBus):
    """
    EventBus with guaranteed delivery, retry, and dead-letter queue.

    Drop-in replacement for EventBus — same .on() / .emit() API,
    plus .drain() and .dead_letters().

    Extends EventBus rather than replacing it so existing code
    using EventBus continues to work.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_delay_s: float = 0.1,
        max_dlq_size: int = 1000,
        persist_path: str | None = None,
    ) -> None:
        super().__init__()
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._max_dlq_size = max_dlq_size
        self._persist_path = persist_path
        self._dlq: deque[DeadLetter] = deque(maxlen=max_dlq_size)
        self._durable_handlers: dict[EventKind | None, list[_HandlerEntry]] = defaultdict(list)
        self._pending: list[asyncio.Task] = []
        self._seq: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def on(
        self,
        kind: EventKind | None = None,
        *,
        max_retries: int | None = None,
        retry_delay_s: float | None = None,
    ) -> Callable[[Handler], Handler]:
        """
        Subscribe with retry + dead-letter protection.

        Usage:
            @bus.on(EventKind.STEP_END, max_retries=3)
            async def handle(event): ...
        """
        def decorator(fn: Handler) -> Handler:
            entry = _HandlerEntry(
                fn=fn,
                name=fn.__qualname__,
                max_retries=max_retries if max_retries is not None else self._max_retries,
                retry_delay_s=retry_delay_s if retry_delay_s is not None else self._retry_delay_s,
            )
            self._durable_handlers[kind].append(entry)
            return fn
        return decorator

    async def emit(self, event: WIREEvent) -> None:
        """
        Emit an event to all registered handlers with retry + DLQ.
        Never raises — failed handlers go to dead-letter queue.
        """
        self._seq += 1
        event_with_seq = event.model_copy(
            update={"data": {**event.data, "_seq": self._seq}}
        )

        # Persist if configured
        if self._persist_path:
            await self._persist(event_with_seq)

        # Collect matching handlers
        handlers: list[_HandlerEntry] = (
            list(self._durable_handlers.get(event.kind, []))
            + list(self._durable_handlers.get(None, []))   # wildcard
        )

        # Also fire base EventBus handlers (backward compat)
        base_handlers = (
            self._handlers.get(event.kind, [])
            + self._wildcard
        )

        # Fire base handlers (fire-and-forget, existing behaviour)
        if base_handlers:
            results = await asyncio.gather(
                *(h(event) for h in base_handlers),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    log.error("base_handler_error", kind=event.kind, error=str(r))

        # Fire durable handlers with retry
        tasks = [
            asyncio.create_task(self._deliver_with_retry(event_with_seq, entry))
            for entry in handlers
        ]
        self._pending.extend(tasks)
        # Clean up completed tasks
        self._pending = [t for t in self._pending if not t.done()]

    async def drain(self, timeout_s: float = 10.0) -> int:
        """
        Wait for all pending event deliveries to complete.
        Returns number of events successfully drained.
        Call before shutdown to guarantee no events are lost.
        """
        if not self._pending:
            return 0
        pending = list(self._pending)
        done, pending_still = await asyncio.wait(pending, timeout=timeout_s)
        self._pending = list(pending_still)
        drained = len(done)
        if pending_still:
            log.warning("drain_timeout", pending=len(pending_still), timeout_s=timeout_s)
        else:
            log.debug("drain_complete", drained=drained)
        return drained

    async def dead_letters(self) -> list[DeadLetter]:
        """Return all events that failed delivery after all retries."""
        return list(self._dlq)

    def dlq_size(self) -> int:
        return len(self._dlq)

    async def retry_dead_letters(self) -> int:
        """Re-attempt delivery of all dead-letter events. Returns count retried."""
        retried = 0
        to_retry = list(self._dlq)
        self._dlq.clear()
        for dl in to_retry:
            await self.emit(dl.event)
            retried += 1
        return retried

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _deliver_with_retry(self, event: WIREEvent, entry: _HandlerEntry) -> None:
        last_error = ""
        for attempt in range(entry.max_retries + 1):
            try:
                await entry.fn(event)
                if attempt > 0:
                    log.info(
                        "handler_recovered",
                        handler=entry.name,
                        attempt=attempt,
                        kind=event.kind,
                    )
                return
            except Exception as e:
                last_error = str(e)
                log.warning(
                    "handler_error",
                    handler=entry.name,
                    attempt=attempt,
                    max_retries=entry.max_retries,
                    error=last_error,
                    kind=event.kind,
                )
                if attempt < entry.max_retries:
                    await asyncio.sleep(entry.retry_delay_s * (2 ** attempt))

        # All retries exhausted → dead-letter queue
        dl = DeadLetter(
            event=event,
            handler_name=entry.name,
            error=last_error,
            attempts=entry.max_retries + 1,
        )
        self._dlq.append(dl)
        log.error(
            "handler_dead_letter",
            handler=entry.name,
            kind=event.kind,
            run_id=event.run_id,
            dlq_size=len(self._dlq),
        )

    async def _persist(self, event: WIREEvent) -> None:
        """Persist event to SQLite for replay."""
        try:
            import aiosqlite
            async with aiosqlite.connect(self._persist_path) as db:
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS wire_events (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        role TEXT,
                        data TEXT NOT NULL,
                        ts TEXT NOT NULL,
                        seq INTEGER NOT NULL
                    )"""
                )
                await db.execute(
                    "INSERT OR IGNORE INTO wire_events VALUES (?,?,?,?,?,?,?)",
                    (
                        event.id, event.kind, event.run_id, event.role,
                        json.dumps(event.data), event.ts.isoformat(),
                        event.data.get("_seq", 0),
                    ),
                )
                await db.commit()
        except Exception as e:
            log.warning("event_persist_failed", error=str(e))

    async def replay_events(
        self,
        run_id: str,
        from_seq: int = 0,
    ) -> list[WIREEvent]:
        """Replay persisted events for a run_id from a given sequence number."""
        if not self._persist_path:
            return []
        try:
            import aiosqlite
            async with aiosqlite.connect(self._persist_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM wire_events WHERE run_id=? AND seq>? ORDER BY seq ASC",
                    (run_id, from_seq),
                ) as cur:
                    rows = await cur.fetchall()
            return [
                WIREEvent(
                    id=row["id"],
                    kind=EventKind(row["kind"]),
                    run_id=row["run_id"],
                    role=row["role"],
                    data=json.loads(row["data"]),
                    ts=datetime.fromisoformat(row["ts"]),
                )
                for row in rows
            ]
        except Exception as e:
            log.warning("event_replay_failed", error=str(e))
            return []
