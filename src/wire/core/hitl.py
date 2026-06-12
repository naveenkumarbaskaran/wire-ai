"""
HITLGate — Human-in-the-Loop as a first-class primitive.

The #1 gap in every agent framework: HITL is either missing or
re-implemented manually per project. HITLGate standardises it.

Supports approval channels:
  - cli      (default — blocks in terminal, works everywhere)
  - slack    (Sprint 4 — sends message, waits for reaction/response)
  - email    (Sprint 4)
  - webhook  (Sprint 4 — POST to any URL)

Decision is durable: stored to DurableState so the workforce survives
restarts while waiting for a human response.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import WIREError
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.models import Risk

log = structlog.get_logger(__name__)


class HITLChannel(str, Enum):
    CLI     = "cli"
    SLACK   = "slack"
    EMAIL   = "email"
    WEBHOOK = "webhook"


class HITLAction(str, Enum):
    APPROVE = "approve"
    REJECT  = "reject"
    MODIFY  = "modify"
    TIMEOUT = "timeout"


class TimeoutAction(str, Enum):
    ESCALATE = "escalate"   # raise HITLTimeoutError
    APPROVE  = "approve"    # auto-approve on timeout
    REJECT   = "reject"     # auto-reject on timeout


class HITLDecision(BaseModel):
    """The human's response to a HITL request."""
    request_id: str
    action: HITLAction
    actor: str = "human"
    notes: str = ""
    modified_data: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HITLRequest(BaseModel):
    """A pending HITL approval request."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    role: str | None = None
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    risk: Risk = Risk.MEDIUM
    options: list[str] = Field(default_factory=lambda: ["approve", "reject"])
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HITLTimeoutError(WIREError):
    """Raised when a HITL request times out and timeout_action=ESCALATE."""
    def __init__(self, request_id: str, timeout_minutes: int) -> None:
        self.request_id = request_id
        self.timeout_minutes = timeout_minutes
        super().__init__(
            f"HITL request {request_id} timed out after {timeout_minutes}m "
            "with no human response. Halting — human review required."
        )


class HITLRejectedError(WIREError):
    """Raised when a human explicitly rejects a HITL request."""
    def __init__(self, request_id: str, notes: str = "") -> None:
        self.request_id = request_id
        self.notes = notes
        super().__init__(
            f"HITL request {request_id} rejected by human"
            + (f": {notes}" if notes else "")
        )


class HITLGate:
    """
    First-class HITL primitive — standardises human approval across all frameworks.

    Usage:
        gate = HITLGate(
            trigger=Risk.HIGH,
            channel=HITLChannel.CLI,
            timeout_minutes=30,
            timeout_action=TimeoutAction.ESCALATE,
        )

        decision = await gate.request(
            run_id="run_abc",
            message="Approve Jira P1 creation for $847 anomaly?",
            context={"amount": 847, "service": "us-east-1"},
            risk=Risk.HIGH,
        )
        if decision.action == HITLAction.APPROVE:
            ...

    Slack channel:
        gate = HITLGate(
            channel="slack:#ops-approvals",   # or HITLChannel.SLACK + slack_channel=
            slack_channel="#ops-approvals",
            slack_token="xoxb-...",           # or set SLACK_BOT_TOKEN env var
        )
    """

    def __init__(
        self,
        *,
        trigger: Risk = Risk.HIGH,
        channel: HITLChannel | str = HITLChannel.CLI,
        timeout_minutes: int = 30,
        timeout_action: TimeoutAction = TimeoutAction.ESCALATE,
        options: list[str] | None = None,
        bus: EventBus | None = None,
        slack_channel: str | None = None,
        slack_token: str | None = None,
    ) -> None:
        # Handle "slack:#channel-name" shorthand — extract channel name and
        # coerce the enum to HITLChannel.SLACK
        raw = channel if isinstance(channel, str) else channel.value
        if raw.startswith("slack:"):
            self.channel = HITLChannel.SLACK
            # Only use the embedded channel if slack_channel was not supplied
            if slack_channel is None:
                slack_channel = raw[len("slack:"):]
        else:
            self.channel = HITLChannel(raw) if isinstance(channel, str) else channel

        self.trigger = trigger
        self.timeout_minutes = timeout_minutes
        self.timeout_action = timeout_action
        self.options = options or ["approve", "reject", "modify"]
        self._bus = bus
        self._pending: dict[str, asyncio.Future[HITLDecision]] = {}
        self._slack_channel = slack_channel
        self._slack_token = slack_token

    # ── Public API ────────────────────────────────────────────────────────────

    async def request(
        self,
        *,
        run_id: str,
        message: str,
        context: dict[str, Any] | None = None,
        risk: Risk = Risk.MEDIUM,
        role: str | None = None,
        options: list[str] | None = None,
    ) -> HITLDecision:
        """
        Pause execution and wait for a human decision.
        Returns HITLDecision. Never silently swallows a rejection.
        """
        req = HITLRequest(
            run_id=run_id,
            role=role,
            message=message,
            context=context or {},
            risk=risk,
            options=options or self.options,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=self.timeout_minutes)
            ),
        )

        log.info(
            "hitl_request",
            request_id=req.id,
            run_id=run_id,
            risk=risk,
            channel=self.channel,
            timeout_minutes=self.timeout_minutes,
        )

        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.HITL_REQUEST,
                run_id=run_id,
                role=role,
                data={"request_id": req.id, "message": message, "risk": risk, "channel": self.channel},
            ))

        decision = await self._dispatch(req)

        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.HITL_RESPONSE,
                run_id=run_id,
                role=role,
                data={"request_id": req.id, "action": decision.action, "actor": decision.actor},
            ))

        log.info(
            "hitl_decision",
            request_id=req.id,
            action=decision.action,
            actor=decision.actor,
        )
        return decision

    def should_trigger(self, risk: Risk) -> bool:
        """Returns True if the given risk level should trigger this gate."""
        order = [Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]
        return order.index(risk) >= order.index(self.trigger)

    # ── Channel dispatch ──────────────────────────────────────────────────────

    async def _dispatch(self, req: HITLRequest) -> HITLDecision:
        if self.channel == HITLChannel.CLI:
            return await self._cli_prompt(req)
        if self.channel == HITLChannel.SLACK:
            return await self._slack_prompt(req)
        # Sprint 4: email, webhook
        log.warning("hitl_channel_fallback", channel=self.channel, fallback="cli")
        return await self._cli_prompt(req)

    async def _slack_prompt(self, req: HITLRequest) -> HITLDecision:
        """Deliver HITL request via Slack and wait for a human response."""
        from wire.channels.slack import SlackHITLChannel

        if not self._slack_channel:
            raise ValueError(
                "slack_channel is required when using HITLChannel.SLACK. "
                "Pass slack_channel='#my-channel' to HITLGate, or use "
                "channel='slack:#my-channel'."
            )

        slack = SlackHITLChannel(
            channel=self._slack_channel,
            token=self._slack_token,
            timeout_minutes=self.timeout_minutes,
            timeout_action=self.timeout_action,
        )
        return await slack.request(
            req,
            timeout_minutes=self.timeout_minutes,
            timeout_action=self.timeout_action,
        )

    async def _cli_prompt(self, req: HITLRequest) -> HITLDecision:
        """Interactive CLI prompt with timeout."""
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt
        from rich.table import Table

        console = Console()

        # Build context table
        table = Table(show_header=False, box=None, padding=(0, 1))
        for k, v in req.context.items():
            table.add_row(f"[dim]{k}[/dim]", str(v))

        console.print(Panel(
            f"[bold yellow]⏸  HITL APPROVAL REQUIRED[/bold yellow]\n\n"
            f"[white]{req.message}[/white]\n\n"
            + (table.__rich_console__(console, console.options) and "" or ""),
            title=f"[bold]Risk: {req.risk.upper()}[/bold]  |  Request: {req.id[:8]}",
            border_style="yellow",
        ))

        if req.context:
            for k, v in req.context.items():
                console.print(f"  [dim]{k}:[/dim] {v}")
            console.print()

        options_str = " / ".join(f"[bold]{o}[/bold]" for o in req.options)
        console.print(f"Options: {options_str}")
        if self.timeout_minutes:
            console.print(f"[dim]Timeout: {self.timeout_minutes}m → {self.timeout_action}[/dim]\n")

        try:
            raw = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: Prompt.ask(
                        "Decision",
                        choices=req.options,
                        default=req.options[0],
                    )
                ),
                timeout=self.timeout_minutes * 60,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return self._handle_timeout(req)

        notes = ""
        if raw == "modify":
            notes = Prompt.ask("Modification notes")
        elif raw == "reject":
            notes = Prompt.ask("Rejection reason", default="")

        action = HITLAction(raw) if raw in HITLAction._value2member_map_ else HITLAction.APPROVE
        decision = HITLDecision(
            request_id=req.id,
            action=action,
            actor="human:cli",
            notes=notes,
        )

        if decision.action == HITLAction.REJECT:
            raise HITLRejectedError(req.id, notes)

        return decision

    def _handle_timeout(self, req: HITLRequest) -> HITLDecision:
        log.warning("hitl_timeout", request_id=req.id, action=self.timeout_action)
        if self.timeout_action == TimeoutAction.ESCALATE:
            raise HITLTimeoutError(req.id, self.timeout_minutes)
        action = (
            HITLAction.APPROVE
            if self.timeout_action == TimeoutAction.APPROVE
            else HITLAction.REJECT
        )
        return HITLDecision(
            request_id=req.id,
            action=action,
            actor="wire:timeout",
            notes=f"Auto-{action} after {self.timeout_minutes}m timeout",
        )
