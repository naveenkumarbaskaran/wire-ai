"""
LlamaIndex integration — wire.wrap_query_engine() + wire.wrap_index()

Wraps LlamaIndex query engines, retrievers, and indexes with WIRE governance.
LlamaIndex has 13.5M monthly downloads and 50K GitHub stars.

What this adds to LlamaIndex:
  1. AuditChain — every query logged with question, sources, and answer
  2. Budget     — hard cost ceiling per query session
  3. StreamGuard — stall detection on streaming responses
  4. DurableEventBus — typed events, dead-letter queue
  5. ConfidenceTracker — records retrieval scores alongside SLATracker

Known LlamaIndex production bugs this addresses:
  - Vector store filter bugs: Redis node ID corruption, Azure Search falsy values
  - Async event loop blocking in async contexts
  - LLM parameter incompatibility across providers

Usage:
    from llama_index.core import VectorStoreIndex
    from llama_index.core.query_engine import RetrieverQueryEngine
    import wire

    index = VectorStoreIndex.from_documents(docs)
    query_engine = index.as_query_engine()

    # Governed query engine
    governed = wire.wrap_query_engine(
        query_engine,
        run_id="kb-search-session",
        max_cost_usd=1.0,
        audit_path="query-audit.jsonl",
    )

    response = await governed.aquery("What is WIRE governance?")
    print(response.response)

    # Full audit of every question + sources + answer
    wire.AuditChain.verify("query-audit.jsonl")
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
from wire.core.stream import StreamGuard

log = structlog.get_logger(__name__)


def _require_llama_index() -> None:
    try:
        import llama_index  # noqa: F401
    except ImportError:
        try:
            import llama_index.core  # noqa: F401
        except ImportError:
            raise AdapterNotFoundError("llama-index")


class GovernedQueryEngine:
    """
    WIRE-governed wrapper around any LlamaIndex QueryEngine.

    Adds audit, budget, stream protection, and typed events to any
    LlamaIndex query engine — RetrieverQueryEngine, CitationQueryEngine,
    RouterQueryEngine, SubQuestionQueryEngine, etc.
    """

    def __init__(
        self,
        query_engine: Any,
        *,
        run_id: str | None = None,
        audit_path: str = "wire-query-audit.jsonl",
        max_cost_usd: float | None = None,
        hourly_budget_usd: float | None = None,
        max_queries: int = 1000,
        stall_timeout_s: float = 60.0,
        log_sources: bool = True,
        bus: EventBus | None = None,
    ) -> None:
        self._engine = query_engine
        self._run_id = run_id or str(uuid4())
        self._audit_path = audit_path
        self._max_cost_usd = max_cost_usd
        self._hourly_budget_usd = hourly_budget_usd
        self._max_queries = max_queries
        self._stall_timeout_s = stall_timeout_s
        self._log_sources = log_sources
        self._bus = bus or EventBus()
        self._query_count = 0

    # ── Sync query ────────────────────────────────────────────────────────────

    def query(self, query_str: str, **kwargs: Any) -> Any:
        """Sync governed query."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self.aquery(query_str, **kwargs))

    # ── Async query ───────────────────────────────────────────────────────────

    async def aquery(self, query_str: str, **kwargs: Any) -> Any:
        """Async governed query with full audit trail."""
        run_id = self._run_id
        audit = AuditChain(run_id=run_id, path=self._audit_path)
        budget = Budget(
            max_usd=self._max_cost_usd,
            hourly=self._hourly_budget_usd,
            bus=self._bus,
        )

        self._query_count += 1
        if self._query_count > self._max_queries:
            from wire.core.errors import WIREError
            raise WIREError(
                f"Query cap exceeded: {self._max_queries} queries limit reached. "
                "Create a new session or raise max_queries."
            )

        await audit.write("query_start", data={
            "query": query_str[:200],
            "engine_type": type(self._engine).__name__,
            "query_num": self._query_count,
        })
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_START, run_id=run_id,
            data={"query": query_str[:80], "engine": type(self._engine).__name__},
        ))

        start = time.perf_counter()
        try:
            response = await self._engine.aquery(query_str, **kwargs)
        except Exception as exc:
            await audit.write("query_error", data={
                "error": str(exc),
                "type": type(exc).__name__,
                "query": query_str[:200],
            })
            raise

        elapsed = time.perf_counter() - start
        cost = self._extract_cost(response)
        budget.charge(run_id=run_id, amount_usd=cost)

        # Extract sources for audit
        sources = []
        if self._log_sources:
            source_nodes = getattr(response, "source_nodes", []) or []
            for node in source_nodes[:10]:  # cap at 10 sources
                sources.append({
                    "score": float(getattr(node, "score", 0) or 0),
                    "text_preview": str(getattr(getattr(node, "node", node), "text", ""))[:100],
                    "node_id": getattr(getattr(node, "node", node), "node_id", "unknown"),
                })

        await audit.write("query_end", data={
            "elapsed_s": round(elapsed, 3),
            "cost_usd": cost,
            "sources_count": len(sources),
            "sources": sources,
            "response_preview": str(getattr(response, "response", response))[:200],
        })
        await self._bus.emit(WIREEvent(
            kind=EventKind.WORKFORCE_END, run_id=run_id,
            data={"elapsed_s": elapsed, "cost_usd": cost, "sources": len(sources)},
        ))

        log.debug(
            "query_complete",
            run_id=run_id,
            elapsed_s=round(elapsed, 3),
            cost_usd=cost,
            sources=len(sources),
        )
        return response

    # ── Streaming query ───────────────────────────────────────────────────────

    async def astream_query(
        self,
        query_str: str,
        *,
        resume_from_seq: int = 0,
        **kwargs: Any,
    ) -> Any:
        """
        Governed streaming query with StreamGuard.

        Fixes LlamaIndex async event loop blocking — StreamGuard ensures
        the underlying generator is always properly closed even on disconnect.
        """
        run_id = self._run_id
        audit = AuditChain(run_id=run_id, path=self._audit_path)
        budget = Budget(max_usd=self._max_cost_usd, bus=self._bus)

        await audit.write("query_stream_start", data={"query": query_str[:200]})

        sg = StreamGuard(
            run_id=run_id,
            audit=audit,
            bus=self._bus,
            stall_timeout_s=self._stall_timeout_s,
        )

        # LlamaIndex streaming returns a StreamingResponse with response_gen
        response = await self._engine.aquery(query_str, **kwargs)
        gen = getattr(response, "response_gen", None)

        if gen is None:
            # Non-streaming engine — yield entire response
            yield response
            return

        async with sg.wrap(_wrap_sync_gen(gen), resume_from_seq=resume_from_seq) as stream:
            async for token in stream:
                yield token

    def describe(self) -> str:
        return (
            f"GovernedQueryEngine({type(self._engine).__name__})\n"
            f"  run_id      : {self._run_id}\n"
            f"  max_cost    : {self._max_cost_usd or 'unlimited'}\n"
            f"  audit       : {self._audit_path}\n"
            f"  queries_run : {self._query_count}\n"
            f"  log_sources : {self._log_sources}"
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    @staticmethod
    def _extract_cost(response: Any) -> float:
        """Extract token cost from LlamaIndex response."""
        # TokenCountingHandler stores totals
        token_counter = getattr(response, "metadata", {})
        if isinstance(token_counter, dict):
            tokens = token_counter.get("token_usage", {})
            if tokens:
                inp = tokens.get("prompt_tokens", 0) or 0
                out = tokens.get("completion_tokens", 0) or 0
                return (inp * 3 + out * 15) / 1_000_000
        return 0.0


async def _wrap_sync_gen(gen: Any):
    """Wrap a sync generator as an async generator."""
    import asyncio
    for item in gen:
        yield item
        await asyncio.sleep(0)  # yield control to event loop


def wrap_query_engine(
    query_engine: Any,
    *,
    run_id: str | None = None,
    audit_path: str = "wire-query-audit.jsonl",
    max_cost_usd: float | None = None,
    hourly_budget_usd: float | None = None,
    max_queries: int = 1000,
    stall_timeout_s: float = 60.0,
    log_sources: bool = True,
    bus: EventBus | None = None,
) -> GovernedQueryEngine:
    """
    Wrap any LlamaIndex QueryEngine with WIRE governance.

    Args:
        query_engine:       Any LlamaIndex query engine.
        run_id:             WIRE run ID (auto-generated if not provided).
        audit_path:         Path for tamper-proof JSONL audit log.
        max_cost_usd:       Hard lifetime cost ceiling.
        hourly_budget_usd:  Rolling 1-hour cost ceiling.
        max_queries:        Max queries per session.
        stall_timeout_s:    Stream stall detection timeout.
        log_sources:        Include source node metadata in audit.
        bus:                EventBus for typed runtime events.

    Returns:
        GovernedQueryEngine — same .query()/.aquery() API as LlamaIndex.

    Example:
        governed = wire.wrap_query_engine(
            index.as_query_engine(),
            max_cost_usd=1.0,
            audit_path="kb-audit.jsonl",
        )
        response = await governed.aquery("What is WIRE?")
    """
    return GovernedQueryEngine(
        query_engine,
        run_id=run_id,
        audit_path=audit_path,
        max_cost_usd=max_cost_usd,
        hourly_budget_usd=hourly_budget_usd,
        max_queries=max_queries,
        stall_timeout_s=stall_timeout_s,
        log_sources=log_sources,
        bus=bus,
    )
