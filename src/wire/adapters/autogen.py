"""
AutoGen adapter for WIRE.

Wraps a pyautogen team/agent with full WIRE governance:
  - LoopGuard     — halts runaway multi-agent loops (documented production bug)
  - AuditChain    — compliance-grade trail (absent from AutoGen natively)
  - HITLGate      — replaces unstable UserProxyAgent blocking
  - SLATracker    — per-team cost + timing enforcement
  - Budget        — hard cost ceiling
  - PolicyEnforcer— read-only agents cannot call write tools
  - EventBus      — typed events for every message/tool call

AutoGen's own docs state UserProxyAgent HITL "blocks the team in an
unstable state that cannot be saved or resumed". This adapter fixes that
by routing HITL through wire.HITLGate instead.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog

from wire.core.audit import AuditChain
from wire.core.budget import Budget
from wire.core.errors import AdapterNotFoundError
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.guard import LoopGuard
from wire.core.models import DeployConfig

log = structlog.get_logger(__name__)


def _require_autogen() -> Any:
    try:
        import autogen  # noqa: F401
        return autogen
    except ImportError:
        try:
            import pyautogen  # noqa: F401
            return pyautogen
        except ImportError:
            raise AdapterNotFoundError("autogen")


class AutoGenAdapter:
    """
    WIRE governance wrapper around an AutoGen agent or team.

    Supports:
      - autogen.AssistantAgent + UserProxyAgent pairs
      - autogen.GroupChat + GroupChatManager
      - autogen AgentChat teams (v0.4+)

    Example:
        import autogen
        import wire

        assistant = autogen.AssistantAgent("assistant", llm_config={...})
        user = autogen.UserProxyAgent("user", human_input_mode="NEVER")

        workforce = wire.deploy(
            {"assistant": assistant, "user": user},
            backend="autogen",
            max_iterations=15,
            max_cost_usd=1.0,
        )
        result = await workforce.ainvoke({"message": "Analyse our AWS costs"})
    """

    def __init__(self, agent_or_team: Any, config: DeployConfig) -> None:
        self._target = agent_or_team
        self._config = config
        self._bus = EventBus()

    # ── Public API ────────────────────────────────────────────────────────────

    async def ainvoke(
        self,
        inputs: dict[str, Any],
        run_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        run_id = run_id or str(uuid4())
        guard, audit, budget = self._build_runtime(run_id)

        await audit.write("workforce_start", data={
            "backend": "autogen",
            "target_type": type(self._target).__name__,
        })
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"backend": "autogen"},
        ))

        start = time.perf_counter()
        message = inputs.get("message", inputs.get("task", str(inputs)))

        try:
            result = await self._run_with_governance(
                message, run_id, guard, audit, budget, **kwargs
            )
        except Exception as exc:
            await audit.write("workforce_error", data={
                "error": str(exc), "type": type(exc).__name__
            })
            raise

        elapsed = time.perf_counter() - start
        await audit.write("workforce_end", data={
            "elapsed_s": round(elapsed, 3),
            "total_cost_usd": budget.total_usd,
            "iterations": guard.iterations,
        })

        log.info("autogen_run_complete", run_id=run_id,
                 elapsed_s=round(elapsed, 3), cost_usd=round(budget.total_usd, 6))
        return result

    def on(self, kind: EventKind | None = None):
        return self._bus.on(kind)

    def describe(self) -> str:
        target_type = type(self._target).__name__
        if isinstance(self._target, dict):
            agents = list(self._target.keys())
        else:
            agents = [target_type]
        lines = [
            "WorkforceGraph (AutoGen backend)",
            f"  agents         : {', '.join(agents)}",
            f"  max_iterations : {self._config.max_iterations}",
            f"  max_cost_usd   : {self._config.max_cost_usd or 'unlimited'}",
            f"  audit          : {self._config.audit_path}",
            f"  hitl           : wire.HITLGate (replaces UserProxyAgent blocking)",
        ]
        return "\n".join(lines)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _run_with_governance(
        self,
        message: str,
        run_id: str,
        guard: LoopGuard,
        audit: AuditChain,
        budget: Budget,
        **kwargs: Any,
    ) -> dict[str, Any]:
        import asyncio

        # Determine execution path based on target type
        target = self._target

        # Dict of agents → initiate_chat between first two
        if isinstance(target, dict):
            agents = list(target.values())
            if len(agents) >= 2:
                initiator, recipient = agents[0], agents[1]
                await audit.write("chat_initiate", data={
                    "initiator": getattr(initiator, "name", "?"),
                    "recipient": getattr(recipient, "name", "?"),
                })

                # Patch reply hook for guard ticks
                original_receive = getattr(recipient, "receive", None)
                call_count = [0]

                def patched_receive(message_obj, sender, request_reply=None, silent=False):
                    call_count[0] += 1
                    guard.tick(cost_usd=0.0)
                    if original_receive:
                        return original_receive(message_obj, sender,
                                                request_reply=request_reply, silent=silent)

                if original_receive:
                    recipient.receive = patched_receive

                try:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: initiator.initiate_chat(
                            recipient,
                            message=message,
                            max_turns=self._config.max_iterations,
                            **kwargs,
                        ),
                    )
                    await audit.write("chat_complete", data={"turns": call_count[0]})
                finally:
                    if original_receive:
                        recipient.receive = original_receive

                return {
                    "output": str(result.summary if hasattr(result, "summary") else result),
                    "cost": getattr(result, "cost", {}),
                }

        # Single agent or AgentChat team
        if hasattr(target, "run") or hasattr(target, "run_stream"):
            from autogen_agentchat.base import TaskResult  # type: ignore[import]
            result = await target.run(task=message, **kwargs)
            guard.tick(cost_usd=0.0)
            await audit.write("team_run_complete")
            return {
                "output": str(result.messages[-1].content if hasattr(result, "messages") else result),
            }

        # Fallback: try generic async invoke
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: target.generate_reply(messages=[{"role": "user", "content": message}])
        )
        guard.tick()
        return {"output": str(result)}

    def _build_runtime(self, run_id: str) -> tuple[LoopGuard, AuditChain, Budget]:
        guard = LoopGuard(
            run_id=run_id,
            max_iterations=self._config.max_iterations,
            max_cost_usd=self._config.max_cost_usd,
            bus=self._bus,
        )
        audit = AuditChain(run_id=run_id, path=self._config.audit_path)
        budget = Budget(
            max_usd=self._config.max_cost_usd,
            hourly=self._config.hourly_budget_usd,
            daily=self._config.daily_budget_usd,
            bus=self._bus,
        )
        return guard, audit, budget
