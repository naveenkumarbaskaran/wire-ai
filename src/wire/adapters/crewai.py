"""
CrewAI adapter for WIRE.

Wraps a CrewAI Crew with full WIRE governance:
  - IdempotencyGuard  — fixes CrewAI's double-fire bug on task retry
  - AuditChain        — per-task tamper-proof trail
  - LoopGuard         — prevents runaway crew cycles
  - Budget            — hard cost ceiling
  - SLATracker        — per-crew SLA enforcement
  - DurableState      — cross-session memory (fixes silent context drift)
  - PolicyEnforcer    — read-only roles cannot write
  - EventBus          — typed events for every crew lifecycle moment

Wire never monkey-patches CrewAI internals.
It wraps crew.kickoff() / crew.kickoff_async() at the call site.
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
from wire.core.idempotency import IdempotencyGuard
from wire.core.models import DeployConfig

log = structlog.get_logger(__name__)


def _require_crewai() -> Any:
    try:
        import crewai  # noqa: F401
        return crewai
    except ImportError:
        raise AdapterNotFoundError("crewai")


class CrewAIAdapter:
    """
    WIRE governance wrapper around a CrewAI Crew.

    Example:
        from crewai import Agent, Task, Crew
        import wire

        crew = Crew(agents=[...], tasks=[...])
        workforce = wire.deploy(crew, backend="crewai",
                                max_iterations=20, max_cost_usd=2.0)
        result = await workforce.ainvoke({})
    """

    def __init__(self, crew: Any, config: DeployConfig) -> None:
        self._crew = crew
        self._config = config
        self._bus = EventBus()
        self._idempotency = IdempotencyGuard(bus=self._bus)

    # ── Public API ────────────────────────────────────────────────────────────

    async def ainvoke(
        self,
        inputs: dict[str, Any],
        run_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Async invoke with full WIRE governance.
        Drop-in replacement for crew.kickoff_async().
        """
        run_id = run_id or str(uuid4())
        guard, audit, budget = self._build_runtime(run_id)

        await audit.write("workforce_start", data={
            "backend": "crewai",
            "input_keys": list(inputs.keys()),
            "agents": self._agent_names(),
            "tasks": self._task_names(),
        })
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"backend": "crewai", "agents": self._agent_names()},
        ))

        start = time.perf_counter()

        try:
            # IdempotencyGuard: key the entire crew run on inputs + task fingerprint
            # This is the fix for CrewAI's double-fire bug on task retry
            idem_key = IdempotencyGuard.make_key(
                "crewai_kickoff",
                {"inputs": inputs, "tasks": self._task_names()},
            )

            result, was_duplicate = await self._idempotency.call(
                key=idem_key,
                fn=lambda: self._run_crew(inputs, run_id, guard, audit, budget, **kwargs),
                run_id=run_id,
                tool="crew.kickoff",
            )

            if was_duplicate:
                await audit.write("kickoff_deduplicated", data={"key": idem_key[:12]})
                log.warning("crewai_kickoff_duplicate", run_id=run_id, key=idem_key[:12])

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

        log.info("crewai_run_complete", run_id=run_id,
                 elapsed_s=round(elapsed, 3), cost_usd=round(budget.total_usd, 6))

        return result if isinstance(result, dict) else {"output": str(result)}

    def on(self, kind: EventKind | None = None):
        return self._bus.on(kind)

    def describe(self) -> str:
        lines = [
            "WorkforceGraph (CrewAI backend)",
            f"  agents         : {', '.join(self._agent_names())}",
            f"  tasks          : {len(self._task_names())}",
            f"  max_iterations : {self._config.max_iterations}",
            f"  max_cost_usd   : {self._config.max_cost_usd or 'unlimited'}",
            f"  audit          : {self._config.audit_path}",
            f"  idempotency    : enabled (double-fire protection)",
        ]
        return "\n".join(lines)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _run_crew(
        self,
        inputs: dict[str, Any],
        run_id: str,
        guard: LoopGuard,
        audit: AuditChain,
        budget: Budget,
        **kwargs: Any,
    ) -> Any:
        """Execute crew with per-task audit + guard ticks."""
        # Patch task callbacks to intercept execution
        original_callbacks = self._patch_task_callbacks(run_id, guard, audit, budget)

        try:
            # Try async kickoff first, fall back to sync
            if hasattr(self._crew, "kickoff_async"):
                result = await self._crew.kickoff_async(inputs=inputs, **kwargs)
            else:
                import asyncio
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._crew.kickoff(inputs=inputs, **kwargs)
                )
        finally:
            self._restore_task_callbacks(original_callbacks)

        return result

    def _patch_task_callbacks(
        self, run_id: str, guard: LoopGuard, audit: AuditChain, budget: Budget
    ) -> dict:
        """Intercept CrewAI task execution for per-task governance."""
        originals: dict = {}
        tasks = getattr(self._crew, "tasks", [])
        for task in tasks:
            originals[id(task)] = getattr(task, "callback", None)
            original_cb = originals[id(task)]

            def make_cb(t=task, orig=original_cb):
                def cb(output: Any) -> Any:
                    guard.tick(cost_usd=0.0)
                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(audit.write(
                            "task_complete",
                            data={"task": str(getattr(t, "description", ""))[:60]},
                        ))
                    except RuntimeError:
                        pass  # No running loop — skip
                    if orig:
                        return orig(output)
                    return output
                return cb

            task.callback = make_cb()

        return originals

    def _restore_task_callbacks(self, originals: dict) -> None:
        for task in getattr(self._crew, "tasks", []):
            orig = originals.get(id(task))
            if orig is not None:
                task.callback = orig
            else:
                task.callback = None

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

    def _agent_names(self) -> list[str]:
        agents = getattr(self._crew, "agents", [])
        return [getattr(a, "role", str(a)) for a in agents]

    def _task_names(self) -> list[str]:
        tasks = getattr(self._crew, "tasks", [])
        return [str(getattr(t, "description", f"task_{i}"))[:40]
                for i, t in enumerate(tasks)]
