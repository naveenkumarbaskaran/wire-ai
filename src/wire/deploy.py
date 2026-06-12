"""
wire.deploy() — primary entry point for wrapping any agent framework with WIRE governance.

Usage:
    import wire
    workforce = wire.deploy(graph, backend="langgraph", max_iterations=50, max_cost_usd=1.0)
    result = await workforce.ainvoke({"messages": [...]})
"""

from __future__ import annotations

from typing import Any

from wire.core.errors import AdapterNotFoundError
from wire.core.models import Backend, DeployConfig


def deploy(
    agent: Any,
    *,
    backend: str | Backend = Backend.LANGGRAPH,
    max_iterations: int = 50,
    max_cost_usd: float | None = None,
    hourly_budget_usd: float | None = None,
    daily_budget_usd: float | None = None,
    audit_path: str = "wire-audit.jsonl",
    otel_enabled: bool = False,
    otel_endpoint: str | None = None,
    otel_service_name: str = "wire-ai",
    metrics_enabled: bool = False,
    **kwargs: Any,
) -> Any:
    """
    Wrap an agent with WIRE governance.

    Args:
        agent:              The agent/graph/crew/team to govern.
        backend:            Framework backend ("langgraph", "crewai", "autogen", "openai").
        max_iterations:     Hard iteration ceiling — raises LoopBreachError when exceeded.
        max_cost_usd:       Lifetime cost ceiling for this run.
        hourly_budget_usd:  Rolling 1-hour cost ceiling.
        daily_budget_usd:   Rolling 24-hour cost ceiling.
        audit_path:         Path for the local JSONL audit chain.
        otel_enabled:       Enable OpenTelemetry tracing.
        otel_endpoint:      OTLP endpoint URL (e.g. http://localhost:4317).
        otel_service_name:  OTel service.name attribute.
        metrics_enabled:    Enable Prometheus /metrics endpoint.

    Returns:
        A governed workforce object with .ainvoke(), .astream(), .on(), .describe().

    Raises:
        AdapterNotFoundError: If the required adapter package is not installed.
    """
    backend_enum = Backend(backend) if isinstance(backend, str) else backend

    config = DeployConfig(
        backend=backend_enum,
        max_iterations=max_iterations,
        max_cost_usd=max_cost_usd,
        hourly_budget_usd=hourly_budget_usd,
        daily_budget_usd=daily_budget_usd,
        audit_path=audit_path,
        otel_enabled=otel_enabled,
        otel_endpoint=otel_endpoint,
        otel_service_name=otel_service_name,
        metrics_enabled=metrics_enabled,
        extra=kwargs,
    )

    if backend_enum == Backend.LANGGRAPH:
        from wire.adapters.langgraph import LangGraphAdapter
        return LangGraphAdapter(agent, config)

    if backend_enum == Backend.CREWAI:
        try:
            from wire.adapters.crewai import CrewAIAdapter  # noqa: F401
        except ImportError:
            raise AdapterNotFoundError("crewai")
        from wire.adapters.crewai import CrewAIAdapter
        return CrewAIAdapter(agent, config)

    if backend_enum == Backend.AUTOGEN:
        try:
            from wire.adapters.autogen import AutoGenAdapter  # noqa: F401
        except ImportError:
            raise AdapterNotFoundError("autogen")
        from wire.adapters.autogen import AutoGenAdapter
        return AutoGenAdapter(agent, config)

    if backend_enum == Backend.OPENAI:
        try:
            from wire.adapters.openai import OpenAIAdapter  # noqa: F401
        except ImportError:
            raise AdapterNotFoundError("openai")
        from wire.adapters.openai import OpenAIAdapter
        return OpenAIAdapter(agent, config)

    raise AdapterNotFoundError(str(backend_enum))
