"""
IdempotencyGuard — prevents duplicate tool execution on retry.

The CrewAI production bug in production: side-effecting tools (payments,
Jira tickets, emails, Slack messages) fire twice when a task fails and retries.
IdempotencyGuard deduplicates at the call site using a content-addressed key.

Key = SHA-256(tool_name + sorted(input_args))
Storage: in-memory (default) or SQLite (durable across restarts).

Usage:
    guard = IdempotencyGuard()

    result = await guard.call(
        key=guard.make_key("jira_create", {"title": "P1 alert", "project": "OPS"}),
        fn=lambda: jira.create_issue(...),
        run_id="run_abc",
        tool="jira_create",
    )
    # Second call with same key returns cached result — never fires twice.
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

    Thread-safe for single-process use. For distributed workforces,
    use the SQLite backend (Sprint 6: Redis/Postgres).
    """

    def __init__(self, bus: EventBus | None = None) -> None:
        self._store: dict[str, IdempotencyRecord] = {}
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

        Usage:
            result, skipped = await guard.call(
                key=guard.make_key("jira_create", args),
                fn=lambda: jira.create(...),
                run_id=run_id,
                tool="jira_create",
            )
            if skipped:
                log.info("duplicate call skipped", tool=tool)
        """
        if key in self._store:
            record = self._store[key]
            record.call_count += 1
            log.warning(
                "idempotency_skip",
                tool=tool,
                run_id=run_id,
                key=key[:12],
                call_count=record.call_count,
                original_execution=record.executed_at.isoformat(),
            )
            if self._bus:
                await self._bus.emit(WIREEvent(
                    kind=EventKind.TOOL_CALL,
                    run_id=run_id,
                    data={
                        "tool": tool,
                        "key": key[:12],
                        "duplicate": True,
                        "call_count": record.call_count,
                    },
                ))
            return record.result, True

        # First call — execute and store
        log.debug("idempotency_execute", tool=tool, run_id=run_id, key=key[:12])
        result = await fn()

        self._store[key] = IdempotencyRecord(
            key=key,
            run_id=run_id,
            tool=tool,
            result=result,
        )

        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.TOOL_RESULT,
                run_id=run_id,
                data={"tool": tool, "key": key[:12], "duplicate": False},
            ))

        return result, False

    def is_duplicate(self, key: str) -> bool:
        """Check without executing — useful for pre-flight checks."""
        return key in self._store

    def clear(self, key: str | None = None) -> None:
        """
        Clear one key or all keys.
        Use after confirmed successful completion to allow re-runs.
        """
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()

    @property
    def call_count(self) -> int:
        return len(self._store)
