"""Typed event system — every WIRE runtime event flows through EventBus."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


class EventKind(str, Enum):
    LOOP_BREACH    = "loop_breach"
    BUDGET_BREACH  = "budget_breach"
    AUDIT_WRITE    = "audit_write"
    AUDIT_VERIFIED = "audit_verified"
    STEP_START     = "step_start"
    STEP_END       = "step_end"
    TOOL_CALL      = "tool_call"
    TOOL_RESULT    = "tool_result"
    HITL_REQUEST   = "hitl_request"
    HITL_RESPONSE  = "hitl_response"
    SLA_BREACH     = "sla_breach"
    COST_UPDATE    = "cost_update"
    WORKFORCE_START = "workforce_start"
    WORKFORCE_END   = "workforce_end"


class WIREEvent(BaseModel):
    """Immutable event emitted by WIRE runtime components."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EventKind
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str
    role: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


Handler = Callable[[WIREEvent], Coroutine[Any, Any, None]]


class EventBus:
    """
    Async pub/sub bus for WIRE runtime events.
    Handlers are fire-and-forget coroutines; exceptions are logged, not raised.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventKind, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []

    def on(self, kind: EventKind | None = None) -> Callable[[Handler], Handler]:
        """Decorator: subscribe a handler to a specific event kind, or all events."""
        def decorator(fn: Handler) -> Handler:
            if kind is None:
                self._wildcard.append(fn)
            else:
                self._handlers[kind].append(fn)
            return fn
        return decorator

    async def emit(self, event: WIREEvent) -> None:
        handlers = self._handlers.get(event.kind, []) + self._wildcard
        if not handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.error("event_handler_error", kind=event.kind, error=str(r))
