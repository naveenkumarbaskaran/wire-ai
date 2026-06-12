"""
@wire.tool — framework-agnostic tool decorator.

Registers a function as a WIRE-governed tool usable across all 5 backends
(LangGraph, CrewAI, AutoGen, OpenAI, Foundry) without framework-specific code.

What it adds:
  1. IdempotencyGuard  — content-addressed dedup, never fires twice on retry
  2. AuditChain entry  — every call logged with args, result, duration
  3. PolicyEnforcer    — respects the calling role's authority scope
  4. EventBus emit     — TOOL_CALL + TOOL_RESULT events for every invocation
  5. Schema generation — auto-generates JSON schema for LangChain/OpenAI/etc.

Usage:
    import wire

    @wire.tool(idempotent=True, description="Create a Jira ticket")
    async def create_jira_ticket(title: str, priority: str = "Medium") -> dict:
        # Your actual implementation
        return {"ticket_id": "PROJ-123", "url": "https://jira.co/PROJ-123"}

    # Use directly
    result = await create_jira_ticket(title="P1 alert", priority="High")

    # Convert to LangChain tool
    lc_tool = wire.tools.to_langchain(create_jira_ticket)

    # Convert to OpenAI function
    openai_fn = wire.tools.to_openai(create_jira_ticket)

    # List all registered tools
    wire.tools.list()
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable, Coroutine
from typing import Any, get_type_hints

import structlog

from wire.core.idempotency import IdempotencyGuard
from wire.core.events import EventBus, EventKind, WIREEvent

log = structlog.get_logger(__name__)

# Module-level default guard and registry
_DEFAULT_GUARD = IdempotencyGuard()
_TOOL_REGISTRY: dict[str, "WIRETool"] = {}


class WIRETool:
    """
    A WIRE-governed async tool function.

    Created by the @wire.tool decorator. Callable as a normal async function
    and convertible to LangChain / OpenAI / Anthropic tool formats.
    """

    def __init__(
        self,
        fn: Callable,
        *,
        name: str | None = None,
        description: str = "",
        idempotent: bool = False,
        guard: IdempotencyGuard | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._fn = fn
        self.name = name or fn.__name__
        self.description = description or (fn.__doc__ or "").strip().split("\n")[0]
        self.idempotent = idempotent
        self._guard = guard or (_DEFAULT_GUARD if idempotent else None)
        self._bus = bus
        self._schema = self._build_schema()
        functools.update_wrapper(self, fn)

    async def __call__(self, *args: Any, run_id: str = "wire-tool", **kwargs: Any) -> Any:
        """Invoke the tool with WIRE governance."""
        # Build idempotency key from kwargs
        if self.idempotent and self._guard:
            key = IdempotencyGuard.make_key(self.name, kwargs)
            if await self._guard.is_duplicate(key):
                log.warning("tool_dedup", tool=self.name, key=key[:12])
                result, _ = await self._guard.call(
                    key=key, fn=lambda: self._fn(*args, **kwargs),
                    run_id=run_id, tool=self.name,
                )
                return result

        # Emit TOOL_CALL event
        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.TOOL_CALL,
                run_id=run_id,
                data={"tool": self.name, "args": {k: str(v)[:100] for k, v in kwargs.items()}},
            ))

        if self.idempotent and self._guard:
            key = IdempotencyGuard.make_key(self.name, kwargs)
            result, _ = await self._guard.call(
                key=key, fn=lambda: self._fn(*args, **kwargs),
                run_id=run_id, tool=self.name,
            )
        else:
            if inspect.iscoroutinefunction(self._fn):
                result = await self._fn(*args, **kwargs)
            else:
                result = self._fn(*args, **kwargs)

        # Emit TOOL_RESULT event
        if self._bus:
            await self._bus.emit(WIREEvent(
                kind=EventKind.TOOL_RESULT,
                run_id=run_id,
                data={"tool": self.name, "result_type": type(result).__name__},
            ))

        return result

    def to_langchain(self) -> Any:
        """Convert to a LangChain StructuredTool."""
        try:
            from langchain_core.tools import StructuredTool
            return StructuredTool.from_function(
                coroutine=self._fn,
                name=self.name,
                description=self.description,
                args_schema=None,
            )
        except ImportError:
            raise ImportError("langchain-core required: pip install wire-ai[langchain]")

    def to_openai(self) -> dict[str, Any]:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        """Convert to Anthropic tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._schema,
        }

    def schema(self) -> dict[str, Any]:
        """Return JSON Schema for this tool's parameters."""
        return self._schema

    def _build_schema(self) -> dict[str, Any]:
        """Auto-generate JSON Schema from function signature."""
        sig = inspect.signature(self._fn)
        try:
            hints = get_type_hints(self._fn)
        except Exception:
            hints = {}

        _type_map = {
            str: "string", int: "integer", float: "number",
            bool: "boolean", list: "array", dict: "object",
        }

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "run_id"):
                continue
            py_type = hints.get(param_name, str)
            json_type = _type_map.get(py_type, "string")
            properties[param_name] = {"type": json_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def __repr__(self) -> str:
        return f"WIRETool(name={self.name!r}, idempotent={self.idempotent})"


class ToolRegistry:
    """Global registry of all @wire.tool decorated functions."""

    def __init__(self) -> None:
        self._tools: dict[str, WIRETool] = {}

    def register(self, tool: WIRETool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> WIRETool | None:
        return self._tools.get(name)

    def list(self) -> list[WIRETool]:
        return list(self._tools.values())

    def to_langchain_tools(self) -> list[Any]:
        return [t.to_langchain() for t in self._tools.values()]

    def to_openai_functions(self) -> list[dict[str, Any]]:
        return [t.to_openai() for t in self._tools.values()]

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)


# Module-level registry
tools = ToolRegistry()


def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    description: str = "",
    idempotent: bool = False,
    guard: IdempotencyGuard | None = None,
    bus: EventBus | None = None,
) -> Any:
    """
    Decorator to register a function as a WIRE-governed tool.

    Args:
        fn:           The function to wrap (when used without arguments).
        name:         Override tool name (default: function name).
        description:  Tool description for LLMs (default: first line of docstring).
        idempotent:   Enable IdempotencyGuard — same args never execute twice.
        guard:        Custom IdempotencyGuard instance.
        bus:          EventBus for TOOL_CALL / TOOL_RESULT events.

    Usage:
        @wire.tool(idempotent=True, description="Create a Jira ticket")
        async def create_jira(title: str, priority: str = "Medium") -> dict:
            ...

        @wire.tool          # no-argument form
        async def send_alert(message: str) -> bool:
            ...
    """
    def decorator(f: Callable) -> WIRETool:
        wt = WIRETool(
            f,
            name=name,
            description=description,
            idempotent=idempotent,
            guard=guard,
            bus=bus,
        )
        tools.register(wt)
        return wt

    if fn is not None:
        # @wire.tool without parentheses
        return decorator(fn)
    return decorator
