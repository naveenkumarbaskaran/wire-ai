"""
WIRE Plugin System — extensible observability and cost-tracking hooks.

Plugins receive lifecycle events from WIRE adapters and can integrate
with external tools (AgentLens, Tokmon, etc.) or custom instrumentation.

Usage:
    from wire.plugins import get_plugin_registry, AgentLensPlugin, TokmonPlugin

    registry = get_plugin_registry()
    registry.register(AgentLensPlugin(api_url="http://localhost:8080"))
    registry.register(TokmonPlugin(session_name="my-run", budget_usd=5.0))
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class WIREPlugin(ABC):
    """
    Base class for all WIRE plugins.

    Plugins are notified at key lifecycle points during a workforce run.
    All hook methods are async and must not raise — exceptions are caught
    by the PluginRegistry and logged.

    Subclasses must declare:
        name: str    — unique identifier (used for register/unregister)
        version: str — semver string

    and implement all four abstract hooks.
    """

    name: str
    version: str

    def is_available(self) -> bool:
        """
        Return True if the plugin's backing package / service is reachable.

        Default implementation returns True — override in subclasses that
        depend on optional packages or external services.
        """
        return True

    @abstractmethod
    async def on_step_start(self, run_id: str, role: str, iteration: int) -> None:
        """Called immediately before a workforce step (node/agent) executes."""

    @abstractmethod
    async def on_step_end(
        self,
        run_id: str,
        role: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Called after a workforce step completes, with token and cost data."""

    @abstractmethod
    async def on_tool_call(self, run_id: str, tool: str, args: dict[str, Any]) -> None:
        """Called when an agent invokes a tool."""

    @abstractmethod
    async def on_workforce_end(
        self, run_id: str, total_cost: float, iterations: int
    ) -> None:
        """Called once when the entire workforce run finishes."""


class PluginRegistry:
    """
    Fan-out hub for all registered WIRE plugins.

    Thread-safe for registration/unregistration (Python GIL).
    Emit methods never raise — plugin exceptions are caught and logged
    so a misbehaving plugin cannot break the governance layer.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, WIREPlugin] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, plugin: WIREPlugin) -> None:
        """Register a plugin. Replaces any existing plugin with the same name."""
        self._plugins[plugin.name] = plugin
        log.info("plugin_registered", plugin=plugin.name, version=plugin.version)

    def unregister(self, name: str) -> None:
        """Remove a plugin by name. No-op if name is not registered."""
        removed = self._plugins.pop(name, None)
        if removed:
            log.info("plugin_unregistered", plugin=name)

    def get(self, name: str) -> WIREPlugin:
        """Return the registered plugin by name. Raises KeyError if not found."""
        return self._plugins[name]

    def list_plugins(self) -> list[WIREPlugin]:
        """Return all registered plugins in insertion order."""
        return list(self._plugins.values())

    # ── Emit helpers ─────────────────────────────────────────────────────────

    async def emit_step_start(
        self, run_id: str, role: str, iteration: int
    ) -> None:
        """Fan out on_step_start to all registered plugins."""
        for plugin in self._plugins.values():
            await self._safe_call(
                plugin.name,
                "on_step_start",
                plugin.on_step_start(run_id=run_id, role=role, iteration=iteration),
            )

    async def emit_step_end(
        self,
        run_id: str,
        role: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Fan out on_step_end to all registered plugins."""
        for plugin in self._plugins.values():
            await self._safe_call(
                plugin.name,
                "on_step_end",
                plugin.on_step_end(
                    run_id=run_id,
                    role=role,
                    cost_usd=cost_usd,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                ),
            )

    async def emit_tool_call(
        self, run_id: str, tool: str, args: dict[str, Any]
    ) -> None:
        """Fan out on_tool_call to all registered plugins."""
        for plugin in self._plugins.values():
            await self._safe_call(
                plugin.name,
                "on_tool_call",
                plugin.on_tool_call(run_id=run_id, tool=tool, args=args),
            )

    async def emit_workforce_end(
        self, run_id: str, total_cost: float, iterations: int
    ) -> None:
        """Fan out on_workforce_end to all registered plugins."""
        for plugin in self._plugins.values():
            await self._safe_call(
                plugin.name,
                "on_workforce_end",
                plugin.on_workforce_end(
                    run_id=run_id,
                    total_cost=total_cost,
                    iterations=iterations,
                ),
            )

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _safe_call(plugin_name: str, hook: str, coro: Any) -> None:
        """Await *coro*, swallowing any exception so one bad plugin can't kill the run."""
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plugin_hook_error",
                plugin=plugin_name,
                hook=hook,
                error=str(exc),
                error_type=type(exc).__name__,
            )


# ── Module-level default registry ────────────────────────────────────────────

_DEFAULT_REGISTRY = PluginRegistry()


def get_plugin_registry() -> PluginRegistry:
    """Return the process-wide default plugin registry."""
    return _DEFAULT_REGISTRY


# ── Convenience re-exports (populated after submodule imports) ────────────────

from wire.plugins.agentlens_plugin import AgentLensPlugin  # noqa: E402
from wire.plugins.tokmon_plugin import TokmonPlugin  # noqa: E402

__all__ = [
    "WIREPlugin",
    "PluginRegistry",
    "get_plugin_registry",
    "AgentLensPlugin",
    "TokmonPlugin",
]
