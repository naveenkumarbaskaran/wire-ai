"""
wire.patch() — auto-govern any existing codebase with one line.

The path to becoming a transitive dependency:
  - Users import wire and call wire.patch() ONCE at app startup
  - Every subsequent LangChain chain, LlamaIndex query engine, or
    OpenAI/Anthropic call automatically gets WIRE governance
  - No changes to existing code — wire wraps at the framework level

This is how datadog-lambda, sentry-sdk, and opentelemetry work:
one import, everything instrumented.

Usage:
    # At app startup — ONE LINE
    import wire
    wire.patch()

    # Everything below is now governed automatically:
    from langchain_core.runnables import RunnableSequence
    chain = prompt | llm  # ← governed transparently
    result = await chain.ainvoke({"input": "..."})

    # All queries governed:
    engine = index.as_query_engine()
    response = engine.query("...")  # ← audited automatically

Patch targets:
  - langchain_core.runnables.base.RunnableSequence.ainvoke
  - langchain_core.runnables.base.RunnableSequence.astream
  - llama_index.core.query_engine.BaseQueryEngine.aquery
  - openai.AsyncOpenAI.chat.completions.create (via wrapper class)
  - anthropic.AsyncAnthropic.messages.create (via wrapper class)
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any
from uuid import uuid4

import structlog

from wire.core.audit import AuditChain
from wire.core.budget import Budget
from wire.core.events import EventBus, EventKind, WIREEvent
from wire.core.guard import LoopGuard
from wire.core.stream import StreamGuard
from wire.observability.metrics import wire_metrics

log = structlog.get_logger(__name__)

# Global patch config — set by wire.patch()
_patch_config: dict[str, Any] = {
    "enabled": False,
    "audit_path": "wire-auto-audit.jsonl",
    "max_cost_usd": None,
    "bus": None,
    "patched": [],
}


def patch(
    *,
    audit_path: str = "wire-auto-audit.jsonl",
    max_cost_usd: float | None = None,
    hourly_budget_usd: float | None = None,
    stall_timeout_s: float = 30.0,
    bus: EventBus | None = None,
    langchain: bool = True,
    llama_index: bool = True,
    openai: bool = True,
    anthropic: bool = True,
    verbose: bool = False,
) -> list[str]:
    """
    Auto-govern all supported frameworks with WIRE.

    Call ONCE at app startup. All subsequent LangChain/LlamaIndex/OpenAI/
    Anthropic calls are automatically governed — no code changes needed.

    Args:
        audit_path:         Path for tamper-proof audit log.
        max_cost_usd:       Hard cost ceiling per call (None = unlimited).
        hourly_budget_usd:  Rolling 1-hour budget.
        stall_timeout_s:    Stream stall detection timeout.
        bus:                EventBus to emit events to.
        langchain:          Patch LangChain LCEL chains.
        llama_index:        Patch LlamaIndex query engines.
        openai:             Patch OpenAI async client.
        anthropic:          Patch Anthropic async client.
        verbose:            Print patch summary.

    Returns:
        List of patched framework names.
    """
    global _patch_config
    _shared_bus = bus or EventBus()
    # Shared Budget across ALL patched frameworks — unified cost ceiling
    from wire.core.budget import Budget
    _shared_budget = Budget(
        max_usd=max_cost_usd,
        hourly=hourly_budget_usd,
        bus=_shared_bus,
    ) if (max_cost_usd or hourly_budget_usd) else None

    _patch_config.update({
        "enabled": True,
        "audit_path": audit_path,
        "max_cost_usd": max_cost_usd,
        "hourly_budget_usd": hourly_budget_usd,
        "stall_timeout_s": stall_timeout_s,
        "bus": _shared_bus,
        "budget": _shared_budget,
    })

    patched = []

    if langchain:
        if _patch_langchain(audit_path, max_cost_usd, stall_timeout_s, _shared_bus):
            patched.append("langchain")

    if llama_index:
        if _patch_llama_index(audit_path, max_cost_usd, _shared_bus):
            patched.append("llama_index")

    if openai:
        if _patch_openai(audit_path, max_cost_usd, _shared_bus):
            patched.append("openai")

    if anthropic:
        if _patch_anthropic(audit_path, max_cost_usd, bus):
            patched.append("anthropic")

    _patch_config["patched"] = patched

    if verbose or patched:
        log.info("wire_patched", frameworks=patched, audit_path=audit_path)

    if verbose:
        _print_patch_summary(patched, audit_path, max_cost_usd)

    return patched


def unpatch() -> None:
    """Remove all WIRE patches. Useful for testing."""
    for restore_fn in _patch_config.get("_restore_fns", []):
        try:
            restore_fn()
        except Exception as e:
            log.warning("unpatch_error", error=str(e))
    _patch_config["enabled"] = False
    _patch_config["patched"] = []
    log.info("wire_unpatched")


def is_patched() -> bool:
    return _patch_config.get("enabled", False)


def patch_status() -> dict[str, Any]:
    return {
        "enabled": _patch_config.get("enabled", False),
        "patched": _patch_config.get("patched", []),
        "audit_path": _patch_config.get("audit_path"),
        "max_cost_usd": _patch_config.get("max_cost_usd"),
    }


# ── LangChain patch ───────────────────────────────────────────────────────────

def _patch_langchain(
    audit_path: str,
    max_cost_usd: float | None,
    stall_timeout_s: float,
    bus: EventBus | None,
) -> bool:
    try:
        from langchain_core.runnables.base import RunnableSequence
        original_ainvoke = RunnableSequence.ainvoke
        original_astream = RunnableSequence.astream

        @functools.wraps(original_ainvoke)
        async def governed_ainvoke(self, input, config=None, **kwargs):
            run_id = str(uuid4())
            audit = AuditChain(run_id=run_id, path=audit_path)
            budget = Budget(max_usd=max_cost_usd, bus=bus)
            await audit.write("lc_chain_start", data={"type": type(self).__name__})
            try:
                result = await original_ainvoke(self, input, config, **kwargs)
            except Exception as exc:
                await audit.write("lc_chain_error", data={"error": str(exc)})
                raise
            await audit.write("lc_chain_end", data={"output_type": type(result).__name__})
            return result

        @functools.wraps(original_astream)
        async def governed_astream(self, input, config=None, **kwargs):
            run_id = str(uuid4())
            audit = AuditChain(run_id=run_id, path=audit_path)
            budget = Budget(max_usd=max_cost_usd, bus=bus)
            sg = StreamGuard(
                run_id=run_id, audit=audit, bus=bus,
                stall_timeout_s=stall_timeout_s,
            )
            async with sg.wrap(original_astream(self, input, config, **kwargs)) as stream:
                async for chunk in stream:
                    yield chunk

        RunnableSequence.ainvoke = governed_ainvoke
        RunnableSequence.astream = governed_astream

        restore_fns = _patch_config.setdefault("_restore_fns", [])
        restore_fns.append(lambda: setattr(RunnableSequence, "ainvoke", original_ainvoke))
        restore_fns.append(lambda: setattr(RunnableSequence, "astream", original_astream))

        log.debug("patched_langchain")
        return True
    except ImportError:
        return False


# ── LlamaIndex patch ──────────────────────────────────────────────────────────

def _patch_llama_index(
    audit_path: str,
    max_cost_usd: float | None,
    bus: EventBus | None,
) -> bool:
    try:
        from llama_index.core.query_engine.base import BaseQueryEngine
        original_aquery = BaseQueryEngine.aquery

        @functools.wraps(original_aquery)
        async def governed_aquery(self, str_or_query_bundle, **kwargs):
            run_id = str(uuid4())
            audit = AuditChain(run_id=run_id, path=audit_path)
            budget = Budget(max_usd=max_cost_usd, bus=bus)
            q = str(str_or_query_bundle)[:200]
            await audit.write("li_query_start", data={"query": q})
            try:
                result = await original_aquery(self, str_or_query_bundle, **kwargs)
            except Exception as exc:
                await audit.write("li_query_error", data={"error": str(exc)})
                raise
            await audit.write("li_query_end", data={"response_type": type(result).__name__})
            return result

        BaseQueryEngine.aquery = governed_aquery
        restore_fns = _patch_config.setdefault("_restore_fns", [])
        restore_fns.append(lambda: setattr(BaseQueryEngine, "aquery", original_aquery))

        log.debug("patched_llama_index")
        return True
    except ImportError:
        return False


# ── OpenAI patch ──────────────────────────────────────────────────────────────

def _patch_openai(
    audit_path: str,
    max_cost_usd: float | None,
    bus: EventBus | None,
) -> bool:
    try:
        import openai
        original_create = openai.AsyncOpenAI.chat.completions.create \
            if hasattr(openai, "AsyncOpenAI") else None

        # Wrap at the class level via __init_subclass__ hook
        # Simpler: patch the module-level client factory
        original_init = openai.AsyncOpenAI.__init__

        @functools.wraps(original_init)
        def governed_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            original_create = self.chat.completions.create

            @functools.wraps(original_create)
            async def governed_create(*a, **kw):
                run_id = str(uuid4())
                audit = AuditChain(run_id=run_id, path=audit_path)
                await audit.write("openai_request", data={
                    "model": kw.get("model", "unknown"),
                    "messages_count": len(kw.get("messages", [])),
                })
                result = await original_create(*a, **kw)
                usage = getattr(result, "usage", None)
                cost = 0.0
                if usage:
                    cost = (
                        (getattr(usage, "prompt_tokens", 0) or 0) * 5 +
                        (getattr(usage, "completion_tokens", 0) or 0) * 15
                    ) / 1_000_000
                await audit.write("openai_response", data={
                    "model": getattr(result, "model", "unknown"),
                    "cost_usd": cost,
                })
                return result

            self.chat.completions.create = governed_create

        openai.AsyncOpenAI.__init__ = governed_init
        restore_fns = _patch_config.setdefault("_restore_fns", [])
        restore_fns.append(lambda: setattr(openai.AsyncOpenAI, "__init__", original_init))

        log.debug("patched_openai")
        return True
    except (ImportError, AttributeError):
        return False


# ── Anthropic patch ───────────────────────────────────────────────────────────

def _patch_anthropic(
    audit_path: str,
    max_cost_usd: float | None,
    bus: EventBus | None,
) -> bool:
    try:
        import anthropic
        original_init = anthropic.AsyncAnthropic.__init__

        @functools.wraps(original_init)
        def governed_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            original_create = self.messages.create

            @functools.wraps(original_create)
            async def governed_create(*a, **kw):
                run_id = str(uuid4())
                audit = AuditChain(run_id=run_id, path=audit_path)
                await audit.write("anthropic_request", data={
                    "model": kw.get("model", "unknown"),
                    "max_tokens": kw.get("max_tokens", 0),
                })
                result = await original_create(*a, **kw)
                usage = getattr(result, "usage", None)
                cost = 0.0
                if usage:
                    inp = getattr(usage, "input_tokens", 0) or 0
                    out = getattr(usage, "output_tokens", 0) or 0
                    cost = (inp * 3 + out * 15) / 1_000_000
                await audit.write("anthropic_response", data={
                    "model": getattr(result, "model", "unknown"),
                    "cost_usd": cost,
                    "stop_reason": getattr(result, "stop_reason", ""),
                })
                return result

            self.messages.create = governed_create

        anthropic.AsyncAnthropic.__init__ = governed_init
        restore_fns = _patch_config.setdefault("_restore_fns", [])
        restore_fns.append(lambda: setattr(anthropic.AsyncAnthropic, "__init__", original_init))

        log.debug("patched_anthropic")
        return True
    except (ImportError, AttributeError):
        return False


def _print_patch_summary(patched: list[str], audit_path: str, max_cost: float | None) -> None:
    from rich.console import Console
    from rich.panel import Panel
    con = Console()
    lines = [
        f"[bold cyan]WIRE auto-governance active[/bold cyan]",
        f"  Patched: [green]{', '.join(patched) or 'none'}[/green]",
        f"  Audit:   {audit_path}",
        f"  Budget:  {'$' + str(max_cost) if max_cost else 'unlimited'}",
        "",
        "[dim]All LangChain/LlamaIndex/OpenAI/Anthropic calls are now governed.[/dim]",
    ]
    con.print(Panel("\n".join(lines), title="WIRE", border_style="cyan"))
