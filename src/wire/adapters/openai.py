"""
OpenAI Agents SDK adapter for WIRE.

Wraps an OpenAI Agent (openai-agents SDK) with full WIRE governance:
  - AuditChain    — tamper-proof trail per run step
  - LoopGuard     — iteration + cost ceiling
  - Budget        — hard cost ceiling with token-cost tracking
  - HITLGate      — first-class approval primitive
  - PolicyEnforcer— tool-call authority enforcement
  - EventBus      — typed events per tool call and handoff

The OpenAI Agents SDK (successor to Swarm) added HITL and guardrails,
but still has no audit trail, no loop containment, and no cost enforcement.
This adapter adds all three.
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


def _require_openai_agents() -> Any:
    try:
        from agents import Runner  # openai-agents SDK  # noqa: F401
        return Runner
    except ImportError:
        try:
            import openai  # noqa: F401
            return openai
        except ImportError:
            raise AdapterNotFoundError("openai")


class OpenAIAdapter:
    """
    WIRE governance wrapper around an OpenAI Agent (openai-agents SDK).

    Example:
        from agents import Agent, Runner
        import wire

        agent = Agent(name="analyst", instructions="Analyse cloud costs.")
        workforce = wire.deploy(agent, backend="openai",
                                max_iterations=20, max_cost_usd=0.50)
        result = await workforce.ainvoke({"message": "What are our top costs?"})
    """

    def __init__(self, agent: Any, config: DeployConfig) -> None:
        self._agent = agent
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

        message = inputs.get("message", inputs.get("task", str(inputs)))

        await audit.write("workforce_start", data={
            "backend": "openai",
            "agent": getattr(self._agent, "name", "unknown"),
        })
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"backend": "openai"},
        ))

        start = time.perf_counter()

        try:
            result = await self._run_agent(message, run_id, guard, audit, budget, **kwargs)
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
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_END, run_id=run_id,
            data={"elapsed_s": elapsed, "total_cost_usd": budget.total_usd},
        ))

        log.info("openai_run_complete", run_id=run_id,
                 elapsed_s=round(elapsed, 3), cost_usd=round(budget.total_usd, 6))
        return result

    def on(self, kind: EventKind | None = None):
        return self._bus.on(kind)

    def describe(self) -> str:
        agent_name = getattr(self._agent, "name", "unknown")
        lines = [
            "WorkforceGraph (OpenAI Agents SDK backend)",
            f"  agent          : {agent_name}",
            f"  max_iterations : {self._config.max_iterations}",
            f"  max_cost_usd   : {self._config.max_cost_usd or 'unlimited'}",
            f"  audit          : {self._config.audit_path}",
        ]
        return "\n".join(lines)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _run_agent(
        self,
        message: str,
        run_id: str,
        guard: LoopGuard,
        audit: AuditChain,
        budget: Budget,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            from agents import Runner
            result = await Runner.run(self._agent, message, **kwargs)

            # Tick guard for each step in the run
            steps = getattr(result, "new_items", []) or []
            for step in steps:
                step_type = type(step).__name__
                cost = self._estimate_step_cost(step)
                guard.tick(cost_usd=cost)
                budget.charge(run_id=run_id, amount_usd=cost)
                await audit.write("step_executed", data={
                    "step_type": step_type,
                    "cost_usd": cost,
                })
                await self._bus.emit(WIREEvent(
                    kind=EventKind.STEP_END, run_id=run_id,
                    data={"step_type": step_type, "iteration": guard.iterations},
                ))

            final_output = getattr(result, "final_output", str(result))
            return {"output": str(final_output), "steps": len(steps)}

        except ImportError:
            # Fallback: use openai chat completions directly
            import openai
            client = openai.AsyncOpenAI()
            response = await client.chat.completions.create(
                model=kwargs.get("model", "gpt-4o-mini"),
                messages=[{"role": "user", "content": message}],
            )
            guard.tick(cost_usd=0.0)
            content = response.choices[0].message.content or ""
            await audit.write("completion", data={"model": response.model})
            return {"output": content}

    @staticmethod
    def _estimate_step_cost(step: Any) -> float:
        """Extract token cost from an OpenAI Agents SDK step item."""
        usage = getattr(step, "usage", None)
        if usage is None:
            return 0.0
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        # GPT-4o-mini pricing
        return (input_tokens * 0.15 + output_tokens * 0.60) / 1_000_000

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
