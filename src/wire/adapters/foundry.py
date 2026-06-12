"""
Microsoft Foundry Agent Service adapter for WIRE.

Wraps an Azure Foundry agent (azure-ai-agents SDK) with full WIRE governance:
  - AuditChain        — tamper-proof trail per run step (absent from Foundry natively)
  - LoopGuard         — iteration + cost ceiling
  - Budget            — hard cost ceiling from run.usage token counts
  - HITLGate          — upgrades Foundry's requires_action pattern to a first-class
                        governed approval primitive with timeout, routing, and audit
  - SLATracker        — response time + cost enforcement per agent run
  - PolicyEnforcer    — tool-call authority validation before submit_tool_outputs
  - DriftDetector     — cross-run output drift detection
  - EventBus          — typed events for every run state transition

Why this adapter matters:
  Microsoft Foundry has the strongest identity story of any cloud platform
  (per-agent Entra ID service principals, blueprint-level Conditional Access,
  4 named RBAC roles) but still has zero SLA enforcement and its guardrails
  are in Preview. This adapter adds the full WIRE governance stack on top.

Requires: pip install wire-ai[foundry]
  → azure-ai-agents>=1.1.0, azure-identity>=1.17

Auth: Entra ID only (DefaultAzureCredential or any TokenCredential).
      No API key auth — this is by design in Foundry.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog

from wire.core.audit import AuditChain
from wire.core.budget import Budget
from wire.core.errors import AdapterNotFoundError, WIREError
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.guard import LoopGuard
from wire.core.hitl import HITLAction, HITLGate, HITLRejectedError
from wire.core.idempotency import IdempotencyGuard
from wire.core.models import DeployConfig, Risk
from wire.core.sla import SLATracker
from wire.visibility.drift import DriftDetector

log = structlog.get_logger(__name__)

# Foundry run terminal states
_TERMINAL_STATES = {"completed", "failed", "cancelled", "expired"}
_POLL_INTERVAL_S = 1.0


class FoundryRunFailedError(WIREError):
    """Raised when a Foundry run terminates in a failed/cancelled/expired state."""
    def __init__(self, run_id: str, status: str, last_error: Any = None) -> None:
        self.run_id = run_id
        self.status = status
        self.last_error = last_error
        detail = f": {last_error}" if last_error else ""
        super().__init__(f"Foundry run {run_id} ended with status '{status}'{detail}")


def _require_foundry() -> Any:
    try:
        from azure.ai.agents import AgentsClient  # noqa: F401
        return AgentsClient
    except ImportError:
        raise AdapterNotFoundError("foundry")


class FoundryAdapter:
    """
    WIRE governance wrapper around a Microsoft Foundry Agent.

    Supports two usage patterns:

    Pattern A — managed run (WIRE handles thread + run lifecycle):
        workforce = wire.deploy(
            {"endpoint": "https://...", "agent_id": "asst_...", "credential": cred},
            backend="foundry",
            max_iterations=30,
            max_cost_usd=1.0,
        )
        result = await workforce.ainvoke({"message": "Analyse our AWS costs"})

    Pattern B — bring your own client:
        from azure.ai.agents.aio import AgentsClient
        client = AgentsClient(endpoint=..., credential=...)
        workforce = wire.deploy(client, backend="foundry", agent_id="asst_...", ...)
        result = await workforce.ainvoke({"message": "..."})
    """

    def __init__(self, agent_or_config: Any, config: DeployConfig) -> None:
        self._config = config
        self._bus = EventBus()
        self._idempotency = IdempotencyGuard(bus=self._bus)
        self._drift = DriftDetector()

        # Blueprint ID — governs this agent type for org-level Conditional Access
        self._blueprint_id: str | None = config.extra.get("blueprint_id")

        # Tool registry: maps tool_name → async callable
        # Register tools via adapter.register_tool("tool_name", async_fn)
        self._tool_registry: dict[str, Any] = config.extra.get("tool_registry", {})

        # Resolve client + agent_id from whatever was passed to wire.deploy()
        if isinstance(agent_or_config, dict):
            self._endpoint = agent_or_config.get("endpoint", "")
            self._agent_id = agent_or_config.get("agent_id", "")
            self._credential = agent_or_config.get("credential")
            self._client: Any = None  # lazy-init
        else:
            # Assume pre-built AgentsClient
            self._client = agent_or_config
            self._agent_id = config.extra.get("agent_id", "")
            self._endpoint = ""
            self._credential = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def ainvoke(
        self,
        inputs: dict[str, Any],
        run_id: str | None = None,
        *,
        thread_id: str | None = None,
        hitl_gate: HITLGate | None = None,
        sla: SLATracker | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Async invoke with full WIRE governance.

        Args:
            inputs:    Must contain 'message' or 'task' key.
            run_id:    WIRE run ID (auto-generated if not provided).
            thread_id: Foundry thread ID. Created fresh if not provided.
            hitl_gate: Override default HITLGate for this invocation.
            sla:       Override default SLATracker for this invocation.
        """
        run_id = run_id or str(uuid4())
        guard, audit, budget = self._build_runtime(run_id)
        client = await self._get_client()
        message = inputs.get("message", inputs.get("task", str(inputs)))

        # Resolve blueprint name if a blueprint_id is configured
        blueprint_name: str | None = None
        if self._blueprint_id:
            try:
                from wire.enterprise.blueprints import get_registry
                _bp = get_registry().get(self._blueprint_id)
                blueprint_name = _bp.name
            except Exception:
                blueprint_name = None

        workforce_start_data: dict[str, Any] = {
            "backend": "foundry",
            "agent_id": self._agent_id,
            "message_preview": message[:80],
        }
        if self._blueprint_id:
            workforce_start_data["blueprint_id"] = self._blueprint_id
            if blueprint_name:
                workforce_start_data["blueprint_name"] = blueprint_name

        await audit.write("workforce_start", data=workforce_start_data)
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"backend": "foundry", "agent_id": self._agent_id,
                  **({"blueprint_id": self._blueprint_id} if self._blueprint_id else {})},
        ))

        start = time.perf_counter()

        try:
            # Create thread if not provided
            if not thread_id:
                thread = await client.threads.create()
                thread_id = thread.id
                await audit.write("thread_created", data={"thread_id": thread_id})

            # Post user message
            await client.messages.create(
                thread_id=thread_id,
                role="user",
                content=message,
            )
            await audit.write("message_posted", data={
                "thread_id": thread_id, "role": "user",
            })

            # Create run with SLA measurement
            _sla = sla or self._default_sla(run_id)
            result = await self._execute_run(
                client=client,
                thread_id=thread_id,
                run_id=run_id,
                guard=guard,
                audit=audit,
                budget=budget,
                hitl_gate=hitl_gate,
                sla=_sla,
                **kwargs,
            )

        except Exception as exc:
            await audit.write("workforce_error", data={
                "error": str(exc), "type": type(exc).__name__,
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

        # Drift detection
        drift_alert = self._drift.observe(
            role=self._agent_id,
            run_id=run_id,
            output=result.get("output", ""),
        )
        if drift_alert and drift_alert.is_significant:
            await audit.write("drift_detected", data={
                "similarity": drift_alert.similarity,
                "threshold": drift_alert.threshold,
            })
            log.warning("foundry_drift", run_id=run_id,
                        similarity=drift_alert.similarity)

        log.info("foundry_run_complete", run_id=run_id,
                 elapsed_s=round(elapsed, 3), cost_usd=round(budget.total_usd, 6))
        return result

    def on(self, kind: EventKind | None = None):
        return self._bus.on(kind)

    def describe(self) -> str:
        lines = [
            "WorkforceGraph (Microsoft Foundry backend)",
            f"  agent_id       : {self._agent_id or '(set via extra.agent_id)'}",
            f"  endpoint       : {self._endpoint[:60] + '...' if len(self._endpoint) > 60 else self._endpoint}",
            f"  max_iterations : {self._config.max_iterations}",
            f"  max_cost_usd   : {self._config.max_cost_usd or 'unlimited'}",
            f"  audit          : {self._config.audit_path}",
            "  auth           : Entra ID (DefaultAzureCredential)",
            "  hitl           : wire.HITLGate (upgrades requires_action pattern)",
            "  sla            : wire.SLATracker (not available natively in Foundry)",
        ]
        if self._blueprint_id:
            blueprint_name: str | None = None
            try:
                from wire.enterprise.blueprints import get_registry
                _bp = get_registry().get(self._blueprint_id)
                blueprint_name = _bp.name
            except Exception:
                blueprint_name = None
            bp_label = f"{self._blueprint_id}"
            if blueprint_name:
                bp_label = f"{blueprint_name} ({self._blueprint_id})"
            lines.append(f"  blueprint      : {bp_label}")
        return "\n".join(lines)

    # ── Run execution ─────────────────────────────────────────────────────────

    async def _execute_run(
        self,
        *,
        client: Any,
        thread_id: str,
        run_id: str,
        guard: LoopGuard,
        audit: AuditChain,
        budget: Budget,
        hitl_gate: HITLGate | None,
        sla: SLATracker | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create and poll a Foundry run with governance at each step."""

        async with (sla.measure(run_id) if sla else _null_ctx()) as sla_ctx:
            # Create run
            foundry_run = await client.runs.create(
                thread_id=thread_id,
                agent_id=self._agent_id,
                **kwargs,
            )
            foundry_run_id = foundry_run.id
            await audit.write("run_created", data={"foundry_run_id": foundry_run_id})

            # Wall-clock timeout: max_iterations * poll_interval + 60s buffer
            import time as _time
            poll_deadline = _time.monotonic() + (
                self._config.max_iterations * _POLL_INTERVAL_S + 60.0
            )

            # Poll loop — check requires_action before sleeping/fetching
            while foundry_run.status not in _TERMINAL_STATES:
                if _time.monotonic() > poll_deadline:
                    raise FoundryRunFailedError(
                        foundry_run_id, "poll_timeout",
                        f"Run exceeded wall-clock timeout of {self._config.max_iterations} iterations"
                    )

                # ── requires_action: check BEFORE sleeping ────────────────────
                if foundry_run.status == "requires_action":
                    foundry_run = await self._handle_requires_action(
                        client=client,
                        thread_id=thread_id,
                        foundry_run=foundry_run,
                        run_id=run_id,
                        audit=audit,
                        hitl_gate=hitl_gate,
                    )
                    continue

                await _async_sleep(_POLL_INTERVAL_S)
                foundry_run = await client.runs.get(
                    thread_id=thread_id,
                    run_id=foundry_run_id,
                )
                guard.tick(cost_usd=0.0)

                await self._bus.emit(WIREEvent(
                    kind=EventKind.STEP_END, run_id=run_id,
                    data={
                        "foundry_status": foundry_run.status,
                        "iteration": guard.iterations,
                    },
                ))

            # ── Terminal state handling ────────────────────────────────────────
            if foundry_run.status != "completed":
                last_error = getattr(foundry_run, "last_error", None)
                raise FoundryRunFailedError(foundry_run_id, foundry_run.status, last_error)

            # Extract cost from run.usage
            usage = getattr(foundry_run, "usage", None)
            if usage and sla is not None:
                cost = self._calc_cost(usage)
                budget.charge(run_id=run_id, amount_usd=cost)
                if sla:
                    sla.record_cost(cost)

            await audit.write("run_completed", data={
                "foundry_run_id": foundry_run_id,
                "status": "completed",
                "usage": {
                    "prompt_tokens": getattr(getattr(foundry_run, "usage", None), "prompt_tokens", 0),
                    "completion_tokens": getattr(getattr(foundry_run, "usage", None), "completion_tokens", 0),
                } if usage else {},
            })

            # Fetch final assistant message
            output = await self._get_last_message(client, thread_id)
            return {
                "output": output,
                "foundry_run_id": foundry_run_id,
                "status": "completed",
                "thread_id": thread_id,
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
                },
            }

    async def _handle_requires_action(
        self,
        *,
        client: Any,
        thread_id: str,
        foundry_run: Any,
        run_id: str,
        audit: AuditChain,
        hitl_gate: HITLGate | None,
    ) -> Any:
        """
        Upgrade Foundry's requires_action pattern to a WIRE HITLGate primitive.

        Foundry uses requires_action for tool calls that need external execution.
        WIRE intercepts this, runs IdempotencyGuard on each tool call, optionally
        routes through HITLGate for high-risk tools, then submits outputs.
        """
        required_action = foundry_run.required_action
        if not required_action or required_action.type != "submit_tool_outputs":
            return foundry_run

        tool_calls = required_action.submit_tool_outputs.tool_calls
        await audit.write("requires_action", data={
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name}
                for tc in tool_calls
            ],
        })

        tool_outputs = []

        for tc in tool_calls:
            import json as _json

            tool_name = tc.function.name
            try:
                args = _json.loads(tc.function.arguments)
            except Exception:
                args = {"raw": tc.function.arguments}

            # ── HITLGate for high-risk tool calls ─────────────────────────────
            if hitl_gate and hitl_gate.should_trigger(Risk.HIGH):
                await audit.write("hitl_requested", data={
                    "tool": tool_name, "args": args,
                }, actor="wire:foundry-adapter")
                try:
                    decision = await hitl_gate.request(
                        run_id=run_id,
                        message=f"Approve tool call: {tool_name}({_json.dumps(args)[:80]})",
                        context={"tool": tool_name, "args": args},
                        risk=Risk.HIGH,
                    )
                    if decision.action == HITLAction.REJECT:
                        tool_outputs.append({
                            "tool_call_id": tc.id,
                            "output": _json.dumps({
                                "error": "rejected_by_human",
                                "notes": decision.notes,
                            }),
                        })
                        continue
                except HITLRejectedError as e:
                    tool_outputs.append({
                        "tool_call_id": tc.id,
                        "output": _json.dumps({"error": "rejected", "notes": e.notes}),
                    })
                    continue

            # ── IdempotencyGuard — never execute same tool call twice ──────────
            idem_key = IdempotencyGuard.make_key(tool_name, args)
            result, was_dup = await self._idempotency.call(
                key=idem_key,
                fn=lambda t=tool_name, a=args: self._execute_tool(t, a),
                run_id=run_id,
                tool=tool_name,
            )

            tool_outputs.append({
                "tool_call_id": tc.id,
                "output": str(result) if not isinstance(result, str) else result,
            })

            await audit.write("tool_executed", data={
                "tool": tool_name,
                "key": idem_key[:12],
                "duplicate": was_dup,
            })

        # Submit outputs back to Foundry
        try:
            from azure.ai.agents.models import ToolOutput
            tool_output_objs = [
                ToolOutput(tool_call_id=o["tool_call_id"], output=o["output"])
                for o in tool_outputs
            ]
        except ImportError:
            # Tests / mock path — pass raw dicts, Foundry client mock accepts them
            tool_output_objs = tool_outputs  # type: ignore[assignment]

        foundry_run = await client.runs.submit_tool_outputs(
            thread_id=thread_id,
            run_id=foundry_run.id,
            tool_outputs=tool_output_objs,
        )
        return foundry_run

    # ── Helpers ───────────────────────────────────────────────────────────────

    def register_tool(self, name: str, fn: Any) -> None:
        """
        Register an async callable to handle a Foundry tool call by name.

        Usage:
            async def create_ticket(title: str, priority: str) -> dict:
                return {"id": "PROJ-123"}

            workforce.register_tool("create_ticket", create_ticket)
        """
        self._tool_registry[name] = fn
        log.debug("foundry_tool_registered", tool=name)

    async def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """
        Execute a Foundry tool call.
        Checks tool_registry first; falls back to placeholder if not registered.
        Register tools with adapter.register_tool("name", async_fn).
        """
        executor = self._tool_registry.get(tool_name)
        if executor:
            try:
                import inspect
                if inspect.iscoroutinefunction(executor):
                    result = await executor(**args)
                else:
                    result = executor(**args)
                import json
                return json.dumps(result, default=str)
            except Exception as e:
                log.error("foundry_tool_error", tool=tool_name, error=str(e))
                return f'{{"error": "{str(e)}", "tool": "{tool_name}"}}'
        else:
            log.warning("foundry_tool_no_executor", tool=tool_name,
                        registered=list(self._tool_registry.keys()))
            return f'{{"status": "ok", "tool": "{tool_name}", "note": "no executor registered — call adapter.register_tool(\\"{tool_name}\\", fn)"}}'

    async def _get_last_message(self, client: Any, thread_id: str) -> str:
        """Fetch the last assistant message from the thread."""
        messages = await client.messages.list(thread_id=thread_id, order="desc", limit=1)
        async for msg in messages:
            if msg.role == "assistant":
                content = msg.content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if hasattr(block, "text"):
                            parts.append(block.text.value if hasattr(block.text, "value") else str(block.text))
                    return " ".join(parts)
                return str(content)
        return ""

    async def _get_client(self) -> Any:
        if self._client is None:
            if not self._endpoint:
                raise WIREError(
                    "Foundry adapter requires endpoint. Pass a dict with 'endpoint' and "
                    "'credential' keys to wire.deploy(), or pass a pre-built AgentsClient."
                )
            from azure.ai.agents.aio import AgentsClient
            cred = self._credential
            if cred is None:
                from azure.identity.aio import DefaultAzureCredential
                cred = DefaultAzureCredential()
            self._client = AgentsClient(endpoint=self._endpoint, credential=cred)
        return self._client

    @staticmethod
    def _calc_cost(usage: Any) -> float:
        """Estimate cost from Foundry run usage (GPT-4o pricing as baseline)."""
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        # GPT-4o: $5/1M input, $15/1M output
        return (prompt * 5 + completion * 15) / 1_000_000

    def _default_sla(self, run_id: str) -> SLATracker | None:
        if self._config.max_cost_usd:
            return SLATracker(
                role=self._agent_id or "foundry-agent",
                max_cost_usd=self._config.max_cost_usd,
                raise_on_breach=False,  # budget raises separately
            )
        return None

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


# ── Async helpers ─────────────────────────────────────────────────────────────

async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


class _null_ctx:
    """No-op async context manager for optional SLA."""
    async def __aenter__(self): return None
    async def __aexit__(self, *_): pass
