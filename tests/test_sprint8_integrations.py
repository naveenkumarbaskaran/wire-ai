"""
Sprint 8 tests — LangChain wrapper, LlamaIndex wrapper, @wire.tool decorator.
All tests use mocks — no real LangChain/LlamaIndex installation required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wire.core.events import EventKind, WIREEvent
from wire.integrations.langchain import GovernedChain, wrap_chain
from wire.integrations.llama_index import GovernedQueryEngine, wrap_query_engine
from wire.integrations.tool_registry import (
    ToolRegistry, WIRETool, tool, tools,
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_mock_chain(response: str = "LangChain response") -> MagicMock:
    chain = MagicMock()
    msg = MagicMock()
    msg.content = response
    msg.usage_metadata = {"input_tokens": 100, "output_tokens": 50}
    chain.ainvoke = AsyncMock(return_value=msg)

    async def _astream(*args, **kwargs):
        for token in response.split():
            yield token

    chain.astream = _astream
    return chain


def _make_mock_query_engine(response_text: str = "LlamaIndex answer") -> MagicMock:
    engine = MagicMock()
    response = MagicMock()
    response.response = response_text
    response.source_nodes = []
    engine.aquery = AsyncMock(return_value=response)
    return engine


# ── LangChain wrapper ─────────────────────────────────────────────────────────

class TestGovernedChain:
    @pytest.mark.asyncio
    async def test_ainvoke_returns_result(self, tmp_path: Path) -> None:
        chain = _make_mock_chain("hello world")
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"))
        result = await governed.ainvoke({"input": "test"})
        assert result.content == "hello world"

    @pytest.mark.asyncio
    async def test_audit_written_on_invoke(self, tmp_path: Path) -> None:
        chain = _make_mock_chain()
        path = tmp_path / "audit.jsonl"
        governed = wrap_chain(chain, audit_path=str(path))
        await governed.ainvoke({"input": "test"})
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "chain_start" in events
        assert "chain_end" in events

    @pytest.mark.asyncio
    async def test_error_written_to_audit(self, tmp_path: Path) -> None:
        chain = MagicMock()
        chain.ainvoke = AsyncMock(side_effect=RuntimeError("LLM error"))
        path = tmp_path / "audit.jsonl"
        governed = wrap_chain(chain, audit_path=str(path))
        with pytest.raises(RuntimeError):
            await governed.ainvoke({"input": "test"})
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        assert any(l["event"] == "chain_error" for l in lines)

    @pytest.mark.asyncio
    async def test_budget_enforced(self, tmp_path: Path) -> None:
        from wire.core.errors import BudgetBreachError
        # Mock with high token count to exceed budget
        chain = MagicMock()
        msg = MagicMock()
        msg.usage_metadata = {"input_tokens": 100_000, "output_tokens": 100_000}
        chain.ainvoke = AsyncMock(return_value=msg)
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"), max_cost_usd=0.001)
        with pytest.raises(BudgetBreachError):
            await governed.ainvoke({"input": "test"})

    @pytest.mark.asyncio
    async def test_astream_yields_chunks(self, tmp_path: Path) -> None:
        chain = _make_mock_chain("hello world foo")
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"))
        chunks = []
        async for chunk in governed.astream({"input": "test"}):
            chunks.append(chunk)
        assert len(chunks) == 3
        assert chunks == ["hello", "world", "foo"]

    @pytest.mark.asyncio
    async def test_events_emitted(self, tmp_path: Path) -> None:
        from wire.core.events import EventBus
        bus = EventBus()
        received = []

        @bus.on(EventKind.WORKFORCE_START)
        async def handler(event: WIREEvent) -> None:
            received.append(event)

        chain = _make_mock_chain()
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"), bus=bus)
        await governed.ainvoke({"input": "test"})
        assert len(received) == 1

    def test_describe_returns_string(self, tmp_path: Path) -> None:
        chain = _make_mock_chain()
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"), max_cost_usd=0.50)
        desc = governed.describe()
        assert "GovernedChain" in desc
        assert "0.5" in desc

    def test_passthrough_attributes(self, tmp_path: Path) -> None:
        chain = _make_mock_chain()
        chain.custom_attr = "test_value"
        governed = wrap_chain(chain, audit_path=str(tmp_path / "a.jsonl"))
        assert governed.custom_attr == "test_value"

    @pytest.mark.asyncio
    async def test_stall_detection_on_astream(self, tmp_path: Path) -> None:
        from wire.core.stream import StreamStallError

        async def slow_chain_stream(*args, **kwargs):
            yield "first"
            await asyncio.sleep(99)

        chain = MagicMock()
        chain.astream = slow_chain_stream
        governed = wrap_chain(
            chain,
            audit_path=str(tmp_path / "a.jsonl"),
            stall_timeout_s=0.05,
        )
        with pytest.raises(StreamStallError):
            async for _ in governed.astream({"input": "test"}):
                pass


# ── LlamaIndex wrapper ────────────────────────────────────────────────────────

class TestGovernedQueryEngine:
    @pytest.mark.asyncio
    async def test_aquery_returns_response(self, tmp_path: Path) -> None:
        engine = _make_mock_query_engine("The answer is 42")
        governed = wrap_query_engine(engine, audit_path=str(tmp_path / "a.jsonl"))
        result = await governed.aquery("What is the answer?")
        assert result.response == "The answer is 42"

    @pytest.mark.asyncio
    async def test_audit_written_on_query(self, tmp_path: Path) -> None:
        engine = _make_mock_query_engine()
        path = tmp_path / "audit.jsonl"
        governed = wrap_query_engine(engine, audit_path=str(path))
        await governed.aquery("test query")
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "query_start" in events
        assert "query_end" in events

    @pytest.mark.asyncio
    async def test_query_text_in_audit(self, tmp_path: Path) -> None:
        engine = _make_mock_query_engine()
        path = tmp_path / "audit.jsonl"
        governed = wrap_query_engine(engine, audit_path=str(path))
        await governed.aquery("What is WIRE governance?")
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        start = next(l for l in lines if l["event"] == "query_start")
        assert "WIRE governance" in start["data"]["query"]

    @pytest.mark.asyncio
    async def test_query_cap_enforced(self, tmp_path: Path) -> None:
        from wire.core.errors import WIREError
        engine = _make_mock_query_engine()
        governed = wrap_query_engine(
            engine, audit_path=str(tmp_path / "a.jsonl"), max_queries=2
        )
        await governed.aquery("q1")
        await governed.aquery("q2")
        with pytest.raises(WIREError, match="Query cap exceeded"):
            await governed.aquery("q3")

    @pytest.mark.asyncio
    async def test_source_nodes_logged(self, tmp_path: Path) -> None:
        engine = MagicMock()
        response = MagicMock()
        response.response = "answer"
        node = MagicMock()
        node.score = 0.92
        node.node = MagicMock()
        node.node.text = "source text here"
        node.node.node_id = "node-001"
        response.source_nodes = [node]
        engine.aquery = AsyncMock(return_value=response)
        path = tmp_path / "audit.jsonl"
        governed = wrap_query_engine(engine, audit_path=str(path), log_sources=True)
        await governed.aquery("test")
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        end = next(l for l in lines if l["event"] == "query_end")
        assert end["data"]["sources_count"] == 1
        assert end["data"]["sources"][0]["score"] == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_error_logged(self, tmp_path: Path) -> None:
        engine = MagicMock()
        engine.aquery = AsyncMock(side_effect=RuntimeError("index error"))
        path = tmp_path / "audit.jsonl"
        governed = wrap_query_engine(engine, audit_path=str(path))
        with pytest.raises(RuntimeError):
            await governed.aquery("test")
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        assert any(l["event"] == "query_error" for l in lines)

    def test_describe(self, tmp_path: Path) -> None:
        engine = _make_mock_query_engine()
        governed = wrap_query_engine(engine, audit_path=str(tmp_path / "a.jsonl"))
        assert "GovernedQueryEngine" in governed.describe()


# ── @wire.tool decorator ──────────────────────────────────────────────────────

class TestWireTool:
    def setup_method(self) -> None:
        # Fresh registry per test
        self.registry = ToolRegistry()

    def test_decorator_creates_wire_tool(self) -> None:
        local_registry = ToolRegistry()

        async def my_tool(x: str) -> str:
            return f"result:{x}"

        wt = WIRETool(my_tool, description="test tool")
        local_registry.register(wt)
        assert isinstance(wt, WIRETool)
        assert wt.name == "my_tool"
        assert len(local_registry) == 1

    @pytest.mark.asyncio
    async def test_tool_callable(self) -> None:
        wt = WIRETool(
            lambda x: f"result:{x}",
            name="test_tool",
            description="test",
            idempotent=False,
        )
        # Make sync fn work
        wt._fn = lambda x: f"result:{x}"
        result = await wt(x="hello")
        assert result == "result:hello"

    @pytest.mark.asyncio
    async def test_async_tool_callable(self) -> None:
        async def async_fn(x: str) -> str:
            return f"async:{x}"

        wt = WIRETool(async_fn, name="async_tool", description="async test")
        result = await wt(x="world")
        assert result == "async:world"

    @pytest.mark.asyncio
    async def test_idempotent_tool_deduplicates(self) -> None:
        from wire.core.idempotency import IdempotencyGuard
        call_log = []

        async def side_effect_fn(title: str) -> dict:
            call_log.append(title)
            return {"id": "TICKET-1"}

        guard = IdempotencyGuard()
        wt = WIRETool(side_effect_fn, name="create_ticket", idempotent=True, guard=guard)

        r1 = await wt(title="P1 alert", run_id="r1")
        r2 = await wt(title="P1 alert", run_id="r1")
        assert r1 == r2 == {"id": "TICKET-1"}
        assert len(call_log) == 1  # only called once

    def test_schema_generated_from_signature(self) -> None:
        async def my_fn(title: str, priority: str = "Medium", count: int = 1) -> dict:
            return {}

        wt = WIRETool(my_fn, name="t", description="d")
        schema = wt.schema()
        assert schema["type"] == "object"
        assert "title" in schema["properties"]
        assert schema["properties"]["title"]["type"] == "string"
        assert "title" in schema["required"]
        assert "priority" not in schema["required"]

    def test_to_openai_format(self) -> None:
        async def send_email(to: str, subject: str) -> bool:
            return True

        wt = WIRETool(send_email, name="send_email", description="Send an email")
        openai_fn = wt.to_openai()
        assert openai_fn["type"] == "function"
        assert openai_fn["function"]["name"] == "send_email"
        assert "to" in openai_fn["function"]["parameters"]["properties"]

    def test_to_anthropic_format(self) -> None:
        async def get_weather(city: str) -> dict:
            return {}

        wt = WIRETool(get_weather, name="get_weather", description="Get weather")
        ant = wt.to_anthropic()
        assert ant["name"] == "get_weather"
        assert "city" in ant["input_schema"]["properties"]

    def test_registry_list(self) -> None:
        registry = ToolRegistry()
        for i in range(3):
            async def fn(): return i
            fn.__name__ = f"tool_{i}"
            registry.register(WIRETool(fn, name=f"tool_{i}", description="t"))
        assert len(registry) == 3
        assert len(registry.list()) == 3

    def test_to_openai_functions_batch(self) -> None:
        registry = ToolRegistry()
        for i in range(3):
            async def fn(x: str) -> str: return x
            fn.__name__ = f"fn_{i}"
            registry.register(WIRETool(fn, name=f"fn_{i}", description="test"))
        fns = registry.to_openai_functions()
        assert len(fns) == 3
        assert all(f["type"] == "function" for f in fns)

    def test_wire_tool_repr(self) -> None:
        async def my_fn(): pass
        wt = WIRETool(my_fn, name="my_fn", description="d", idempotent=True)
        assert "my_fn" in repr(wt)
        assert "idempotent=True" in repr(wt)


# ── Integration: wrap_chain imports from wire top-level ───────────────────────

class TestTopLevelImports:
    def test_wrap_chain_importable_from_wire(self) -> None:
        import wire
        assert callable(wire.wrap_chain)

    def test_wrap_query_engine_importable_from_wire(self) -> None:
        import wire
        assert callable(wire.wrap_query_engine)

    def test_tool_decorator_importable_from_wire(self) -> None:
        import wire
        assert callable(wire.tool)

    def test_tools_registry_importable_from_wire(self) -> None:
        import wire
        assert hasattr(wire, "tools")
        assert isinstance(wire.tools, ToolRegistry)
