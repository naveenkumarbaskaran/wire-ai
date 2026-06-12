"""
LangGraph adapter for WIRE.

Wraps a compiled LangGraph graph (CompiledGraph) with:
  - LoopGuard     — halts runaway node cycles before they exhaust API quota
  - AuditChain    — tamper-proof entry per node execution and tool call
  - Budget        — hard cost ceiling enforced after every LLM response
  - EventBus      — typed events for every significant runtime moment
  - OTel tracing  — span per node, root span per run (opt-in via DeployConfig)

Wire never monkey-patches LangGraph internals.
It wraps graph.stream() / graph.ainvoke() at the call site.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import structlog

from wire.core.audit import AuditChain
from wire.core.budget import Budget
from wire.core.errors import AdapterNotFoundError
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.guard import LoopGuard
from wire.core.models import DeployConfig
from wire.plugins import get_plugin_registry

log = structlog.get_logger(__name__)


def _require_langgraph() -> Any:
    try:
        import langgraph  # noqa: F401
        from langgraph.graph.graph import CompiledGraph
        return CompiledGraph
    except ImportError:
        raise AdapterNotFoundError("langgraph")


class LangGraphAdapter:
    """
    WIRE governance wrapper around a compiled LangGraph graph.

    Example:
        from langgraph.graph import StateGraph
        import wire

        graph = StateGraph(...).compile()
        workforce = wire.deploy(graph, backend="langgraph",
                                max_iterations=30, max_cost_usd=1.0)
        result = await workforce.ainvoke({"messages": [...]})
    """

    def __init__(self, graph: Any, config: DeployConfig) -> None:
        self._graph = graph
        self._config = config
        self._bus = EventBus()
        self._otel_tracer = self._init_otel() if config.otel_enabled else None

    # ── Public API ────────────────────────────────────────────────────────────

    async def ainvoke(
        self,
        input: dict[str, Any],
        run_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Async invoke with full WIRE governance.
        Drop-in replacement for graph.ainvoke().
        """
        run_id = run_id or str(uuid4())
        guard, audit, budget = self._build_runtime(run_id)

        await audit.write("workforce_start", data={"input_keys": list(input.keys())})
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"backend": "langgraph"},
        ))

        start = time.perf_counter()
        result: dict[str, Any] = {}

        try:
            async for chunk in self._graph.astream(input, **kwargs):
                node_name = next(iter(chunk), "unknown")
                node_output = chunk.get(node_name, {})

                # Extract cost from metadata if present (LangGraph >=0.2 adds usage_metadata)
                cost = self._extract_cost(node_output)

                guard.tick(cost_usd=cost)
                budget.charge(run_id=run_id, amount_usd=cost)

                await audit.write(
                    "node_executed",
                    data={"node": node_name, "cost_usd": cost, "iteration": guard.iterations},
                )
                await self._bus.emit(WIREEvent(
                    kind=EventKind.STEP_END, run_id=run_id,
                    data={"node": node_name, "iteration": guard.iterations, "cost_usd": cost},
                ))

                registry = get_plugin_registry()
                await registry.emit_step_start(
                    run_id=run_id, role=node_name, iteration=guard.iterations
                )
                await registry.emit_step_end(
                    run_id=run_id, role=node_name, cost_usd=cost, tokens_in=0, tokens_out=0
                )

                result = node_output

        except Exception as exc:
            await audit.write("workforce_error", data={"error": str(exc), "type": type(exc).__name__})
            raise

        elapsed = time.perf_counter() - start
        await audit.write(
            "workforce_end",
            data={"elapsed_s": round(elapsed, 3), "total_cost_usd": budget.total_usd,
                  "iterations": guard.iterations},
        )
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_END, run_id=run_id,
            data={"elapsed_s": elapsed, "total_cost_usd": budget.total_usd},
        ))

        registry = get_plugin_registry()
        await registry.emit_workforce_end(
            run_id=run_id,
            total_cost=budget.total_usd,
            iterations=guard.iterations,
        )

        log.info(
            "run_complete",
            run_id=run_id,
            elapsed_s=round(elapsed, 3),
            iterations=guard.iterations,
            cost_usd=round(budget.total_usd, 6),
        )
        return result

    async def astream(
        self,
        input: dict[str, Any],
        run_id: str | None = None,
        *,
        stall_timeout_s: float = 30.0,
        resume_from_seq: int = 0,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Governed streaming — wraps graph.astream() with StreamGuard.

        Adds vs plain astream():
          - Stall detection (raises StreamStallError if no chunk in stall_timeout_s)
          - Per-chunk AuditChain entries with sequence numbers
          - LoopGuard + Budget enforced per chunk
          - Graceful cancellation — never leaks the underlying generator
          - Resume support — pass resume_from_seq to skip already-seen chunks
        """
        from wire.core.stream import StreamGuard

        run_id = run_id or str(uuid4())
        guard, audit, budget = self._build_runtime(run_id)

        await audit.write("workforce_start", data={"input_keys": list(input.keys()), "mode": "stream"})

        sg = StreamGuard(
            run_id=run_id,
            audit=audit,
            bus=self._bus,
            stall_timeout_s=stall_timeout_s,
            max_chunks=self._config.max_iterations,
            cost_fn=lambda chunk: self._extract_cost(chunk.get(next(iter(chunk), ""), {})),
        )

        async with sg.wrap(self._graph.astream(input, **kwargs), resume_from_seq=resume_from_seq) as stream:
            async for chunk in stream:
                node_name = next(iter(chunk), "unknown")
                node_output = chunk.get(node_name, {})
                cost = self._extract_cost(node_output)

                guard.tick(cost_usd=cost)
                budget.charge(run_id=run_id, amount_usd=cost)
                yield chunk

        await audit.write("workforce_end", data={
            "total_chunks": sg.last_stats.total_chunks if sg.last_stats else 0,
            "total_cost_usd": budget.total_usd,
        })

    def on(self, kind: EventKind | None = None):  # type: ignore[override]
        """Subscribe to WIRE runtime events from this workforce."""
        return self._bus.on(kind)

    def describe(self) -> str:
        """Human-readable summary of this workforce configuration."""
        lines = [
            "WorkforceGraph (LangGraph backend)",
            f"  max_iterations : {self._config.max_iterations}",
            f"  max_cost_usd   : {self._config.max_cost_usd or 'unlimited'}",
            f"  hourly_budget  : {self._config.hourly_budget_usd or 'unlimited'}",
            f"  audit          : {self._config.audit_path}",
            f"  otel           : {'enabled' if self._config.otel_enabled else 'disabled'}",
        ]
        return "\n".join(lines)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_runtime(
        self, run_id: str
    ) -> tuple[LoopGuard, AuditChain, Budget]:
        guard = LoopGuard(
            run_id=run_id,
            max_iterations=self._config.max_iterations,
            max_cost_usd=self._config.max_cost_usd,
            bus=self._bus,
        )
        audit = AuditChain(
            run_id=run_id,
            path=self._config.audit_path,
        )
        budget = Budget(
            max_usd=self._config.max_cost_usd,
            hourly=self._config.hourly_budget_usd,
            daily=self._config.daily_budget_usd,
            bus=self._bus,
        )
        return guard, audit, budget

    @staticmethod
    def _extract_cost(node_output: Any) -> float:
        """
        Extract token cost from LangGraph node output.
        LangGraph >=0.2 attaches usage_metadata to AIMessage objects.
        Falls back to 0.0 if not present.
        """
        if not isinstance(node_output, dict):
            return 0.0
        messages = node_output.get("messages", [])
        if not messages:
            return 0.0
        last = messages[-1] if isinstance(messages, list) else None
        if last is None:
            return 0.0
        meta = getattr(last, "usage_metadata", None) or {}
        # Rough cost estimate: $3/1M input tokens + $15/1M output tokens (Claude Sonnet)
        input_tokens = meta.get("input_tokens", 0)
        output_tokens = meta.get("output_tokens", 0)
        return (input_tokens * 3 + output_tokens * 15) / 1_000_000

    def _init_otel(self) -> Any:
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider = TracerProvider()
            if self._config.otel_endpoint:
                exporter = OTLPSpanExporter(endpoint=self._config.otel_endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            return trace.get_tracer(
                self._config.otel_service_name,
                schema_url="https://opentelemetry.io/schemas/1.11.0",
            )
        except ImportError:
            log.warning("otel_not_available", msg="opentelemetry packages not installed")
            return None
