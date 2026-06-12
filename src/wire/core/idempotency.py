"""
IdempotencyGuard — prevents duplicate tool execution on retry.

The CrewAI production bug: side-effecting tools (payments, Jira tickets,
emails, Slack messages) fire twice when a task fails and retries.
IdempotencyGuard deduplicates at the call site using a content-addressed key.

Key = SHA-256(tool_name + sorted(input_args))

Storage backends (pluggable):
  MemoryBackend    — default, in-process, zero config (lost on restart)
  SQLiteBackend    — survives restarts, single-node, zero external deps
  RedisBackend     — multi-process, multi-node, TTL expiry
  PostgresBackend  — enterprise, multi-tenant, full history

Usage:
    # Default — in-memory
    guard = IdempotencyGuard()

    # Durable — SQLite (survives restarts)
    from wire.core.idempotency_backends import SQLiteBackend
    guard = IdempotencyGuard(backend=SQLiteBackend("wire-idempotency.db"))

    # Distributed — Redis
    from wire.core.idempotency_backends import RedisBackend
    guard = IdempotencyGuard(backend=RedisBackend("redis://localhost:6379"))

    # Enterprise — Postgres multi-tenant
    from wire.core.idempotency_backends import PostgresBackend
    guard = IdempotencyGuard(backend=PostgresBackend(dsn="postgresql://...", tenant_id="team-a"))

    # All backends share identical call() API:
    result, was_duplicate = await guard.call(
        key=guard.make_key("jira_create", {"title": "P1", "project": "OPS"}),
        fn=lambda: jira.create_issue(...),
        run_id="run_abc",
        tool="jira_create",
    )
    # Second call with same key → returns cached result, fn never called.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field

from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)


class IdempotencyRecord(BaseModel):
    key: str
    run_id: str
    tool: str
    result: Any
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    call_count: int = 1


class IdempotencyGuard:
    """
    Content-addressed deduplication for side-effecting tool calls.

    Backend is pluggable — swap from in-memory to SQLite/Redis/Postgres
    with a single constructor argument. All backends share identical API.
    """

    def __init__(
        self,
        backend: Any | None = None,
        bus: EventBus | None = None,
    ) -> None:
        if backend is None:
            from wire.core.idempotency_backends import MemoryBackend
            backend = MemoryBackend()
        self._backend = backend
        self._bus = bus

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def make_key(tool_name: str, args: dict[str, Any]) -> str:
        """
        Produce a deterministic idempotency key for a tool call.
        Same tool + same args → same key, always.
        """
        canonical = json.dumps(
            {"tool": tool_name, "args": args},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def call(
        self,
        *,
        key: str,
        fn: Callable[[], Coroutine[Any, Any, Any]],
        run_id: str,
        tool: str,
    ) -> tuple[Any, bool]:
        """
        Execute fn() exactly once for a given key.

        Returns:
            (result, was_duplicate) — was_duplicate=True means the call
            was skipped and the cached result returned.
        """
        record = await self._backend.get(key)

        if record is not None:
            new_count = await self._backend.increment(key)
            log.warning(
                "idempotency_skip",
                tool=tool,
                run_id=run_id,
                key=key[:12],
                call_count=new_count,
                original_execution=record.executed_at.isoformat(),
                backend=type(self._backend).__name__,
            )
            if self._bus:
                await self._bus.emit(WIREEvent(
                    kind=EventKind.TOOL_CALL,
                    run_id=run_id,
                    data={
                        "tool": tool,
                        "key": key[:12],
                        "duplicate": True,
                        "call_count": new_count,
                    },
                ))
            return record.result, True

        # First call — execute and store
        log.debug(
            "idempotency_execute",
            tool=tool, run_id=run_id, key=key[:12],
            backend=type(self._backend).__name__,
        )
        result = await fn()

        await self._backend.set(key, IdempotencyRecord(
            key=key, run_id=run_id, tool=tool, result=result,
        ))

        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.TOOL_RESULT,
                run_id=run_id,
                data={"tool": tool, "key": key[:12], "duplicate": False},
            ))

        return result, False

    async def is_duplicate(self, key: str) -> bool:
        """Async check without executing — works across all backends."""
        return await self._backend.exists(key)

    # Keep sync compat for tests that don't need async
    def is_duplicate_sync(self, key: str) -> bool:
        """Sync check — only valid for MemoryBackend."""
        from wire.core.idempotency_backends import MemoryBackend
        if isinstance(self._backend, MemoryBackend):
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Can't block — return False conservatively
                    return False
                return loop.run_until_complete(self._backend.exists(key))
            except Exception:
                return False
        raise RuntimeError(
            "is_duplicate_sync() only works with MemoryBackend. "
            "Use `await guard.is_duplicate(key)` for other backends."
        )

    async def clear(self, key: str | None = None) -> None:
        """Clear one key or all keys across the backend."""
        if key:
            await self._backend.delete(key)
        else:
            await self._backend.clear()

    @property
    def backend_name(self) -> str:
        return type(self._backend).__name__

    @property
    def call_count(self) -> int:
        """Number of recorded keys — only meaningful for MemoryBackend."""
        from wire.core.idempotency_backends import MemoryBackend
        if isinstance(self._backend, MemoryBackend):
            return len(self._backend)
        return -1  # unknown for remote backends
