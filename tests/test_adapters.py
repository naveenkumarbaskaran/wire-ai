"""
Adapter conformance tests — Sprint 5.

Tests the adapter contract without requiring framework packages installed.
Each adapter is tested via mock objects that simulate the framework's API.
This guarantees all three adapters honour the same WIRE governance contracts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wire.core.errors import AdapterNotFoundError, LoopBreachError
from wire.core.models import Backend, DeployConfig
from wire.deploy import deploy


# ── Mock framework objects ────────────────────────────────────────────────────

class MockLangGraphGraph:
    """Minimal LangGraph CompiledGraph stub."""
    async def astream(self, input: dict, **kwargs):
        yield {"agent": {"messages": [_mock_message(100, 50)]}}
        yield {"agent": {"messages": [_mock_message(80, 40)]}}


class MockLangGraphGraphRunaway:
    """Simulates a runaway LangGraph graph — many iterations."""
    async def astream(self, input: dict, **kwargs):
        for i in range(200):
            yield {"agent": {"messages": []}}


def _mock_message(in_tokens: int, out_tokens: int) -> MagicMock:
    m = MagicMock()
    m.usage_metadata = {"input_tokens": in_tokens, "output_tokens": out_tokens}
    return m


class MockCrewAIAgent:
    def __init__(self, role: str):
        self.role = role
        self.callback = None


class MockCrewAITask:
    def __init__(self, description: str):
        self.description = description
        self.callback = None


class MockCrew:
    """Minimal CrewAI Crew stub."""
    def __init__(self, success: bool = True):
        self.agents = [MockCrewAIAgent("analyst"), MockCrewAIAgent("executor")]
        self.tasks = [MockCrewAITask("analyse cost data"), MockCrewAITask("create report")]
        self._success = success

    def kickoff(self, inputs: dict, **kwargs):
        if not self._success:
            raise RuntimeError("crew failed")
        return MagicMock(summary="Crew completed successfully.")


class MockAutoGenAssistant:
    def __init__(self, name: str = "assistant"):
        self.name = name

    def initiate_chat(self, recipient, message: str, max_turns: int = 10, **kwargs):
        result = MagicMock()
        result.summary = f"Analysis complete: {message[:30]}"
        result.cost = {"total": 0.002}
        return result


class MockAutoGenProxy:
    def __init__(self, name: str = "proxy"):
        self.name = name
        self._original_receive = None

    def receive(self, message, sender, request_reply=None, silent=False):
        pass


class MockOpenAIAgent:
    def __init__(self, name: str = "analyst"):
        self.name = name


# ── LangGraph adapter conformance ─────────────────────────────────────────────

class TestLangGraphAdapterConformance:
    @pytest.mark.asyncio
    async def test_ainvoke_returns_dict(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
        adapter = LangGraphAdapter(MockLangGraphGraph(), config)
        result = await adapter.ainvoke({"messages": []})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_audit_chain_written(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(audit_path=str(path))
        adapter = LangGraphAdapter(MockLangGraphGraph(), config)
        await adapter.ainvoke({"messages": []})
        assert path.exists()
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_start" in events
        assert "workforce_end" in events

    @pytest.mark.asyncio
    async def test_loop_breach_raised_on_runaway(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        config = DeployConfig(
            audit_path=str(tmp_path / "audit.jsonl"),
            max_iterations=5,
        )
        adapter = LangGraphAdapter(MockLangGraphGraphRunaway(), config)
        with pytest.raises(LoopBreachError):
            await adapter.ainvoke({"messages": []})

    def test_describe_returns_string(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
        adapter = LangGraphAdapter(MockLangGraphGraph(), config)
        desc = adapter.describe()
        assert "langgraph" in desc.lower()
        assert str(config.max_iterations) in desc

    def test_on_returns_decorator(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        from wire.core.events import EventKind
        config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
        adapter = LangGraphAdapter(MockLangGraphGraph(), config)
        decorator = adapter.on(EventKind.LOOP_BREACH)
        assert callable(decorator)


# ── CrewAI adapter conformance ────────────────────────────────────────────────

class TestCrewAIAdapterConformance:
    @pytest.mark.asyncio
    async def test_ainvoke_returns_dict(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        config = DeployConfig(
            backend=Backend.CREWAI,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
        adapter = CrewAIAdapter(MockCrew(), config)
        result = await adapter.ainvoke({})
        assert isinstance(result, dict)
        assert "output" in result

    @pytest.mark.asyncio
    async def test_audit_chain_written(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.CREWAI, audit_path=str(path))
        adapter = CrewAIAdapter(MockCrew(), config)
        await adapter.ainvoke({})
        assert path.exists()
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_start" in events
        assert "workforce_end" in events

    @pytest.mark.asyncio
    async def test_idempotency_deduplicates_same_inputs(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        call_count = [0]
        original_kickoff = MockCrew.kickoff

        def counting_kickoff(self, inputs, **kwargs):
            call_count[0] += 1
            return original_kickoff(self, inputs, **kwargs)

        with patch.object(MockCrew, "kickoff", counting_kickoff):
            crew = MockCrew()
            config = DeployConfig(backend=Backend.CREWAI, audit_path=str(tmp_path / "a.jsonl"))
            adapter = CrewAIAdapter(crew, config)
            await adapter.ainvoke({"task": "same task"})
            await adapter.ainvoke({"task": "same task"})
        # Only called once — second call deduplicated
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_error_written_to_audit(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.CREWAI, audit_path=str(path))
        adapter = CrewAIAdapter(MockCrew(success=False), config)
        with pytest.raises(RuntimeError):
            await adapter.ainvoke({})
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_error" in events

    def test_describe_mentions_idempotency(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        config = DeployConfig(backend=Backend.CREWAI, audit_path=str(tmp_path / "a.jsonl"))
        adapter = CrewAIAdapter(MockCrew(), config)
        assert "idempoten" in adapter.describe().lower()

    def test_describe_lists_agents(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        config = DeployConfig(backend=Backend.CREWAI, audit_path=str(tmp_path / "a.jsonl"))
        adapter = CrewAIAdapter(MockCrew(), config)
        desc = adapter.describe()
        assert "analyst" in desc


# ── AutoGen adapter conformance ───────────────────────────────────────────────

class TestAutoGenAdapterConformance:
    @pytest.mark.asyncio
    async def test_ainvoke_dict_of_agents(self, tmp_path: Path) -> None:
        from wire.adapters.autogen import AutoGenAdapter
        config = DeployConfig(
            backend=Backend.AUTOGEN,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
        agent = MockAutoGenAssistant()
        proxy = MockAutoGenProxy()
        adapter = AutoGenAdapter({"assistant": agent, "proxy": proxy}, config)
        result = await adapter.ainvoke({"message": "Analyse costs"})
        assert isinstance(result, dict)
        assert "output" in result

    @pytest.mark.asyncio
    async def test_audit_chain_written(self, tmp_path: Path) -> None:
        from wire.adapters.autogen import AutoGenAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.AUTOGEN, audit_path=str(path))
        agent = MockAutoGenAssistant()
        proxy = MockAutoGenProxy()
        adapter = AutoGenAdapter({"a": agent, "p": proxy}, config)
        await adapter.ainvoke({"message": "test"})
        assert path.exists()
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_start" in events
        assert "workforce_end" in events

    def test_describe_mentions_hitl_fix(self, tmp_path: Path) -> None:
        from wire.adapters.autogen import AutoGenAdapter
        config = DeployConfig(backend=Backend.AUTOGEN, audit_path=str(tmp_path / "a.jsonl"))
        adapter = AutoGenAdapter({}, config)
        assert "hitl" in adapter.describe().lower() or "userproxy" in adapter.describe().lower()


# ── OpenAI adapter conformance ────────────────────────────────────────────────

class TestOpenAIAdapterConformance:
    @pytest.mark.asyncio
    async def test_ainvoke_with_openai_fallback(self, tmp_path: Path) -> None:
        from wire.adapters.openai import OpenAIAdapter

        config = DeployConfig(
            backend=Backend.OPENAI,
            audit_path=str(tmp_path / "audit.jsonl"),
        )

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Analysis complete."
        mock_response.model = "gpt-4o-mini"

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        mock_openai_module = MagicMock()
        mock_openai_module.AsyncOpenAI = MagicMock(return_value=mock_client)

        adapter = OpenAIAdapter(MockOpenAIAgent(), config)

        with patch.dict("sys.modules", {"agents": None, "openai": mock_openai_module}):
            result = await adapter.ainvoke({"message": "What are our costs?"})

        assert isinstance(result, dict)
        assert "output" in result

    @pytest.mark.asyncio
    async def test_audit_written_on_fallback(self, tmp_path: Path) -> None:
        from wire.adapters.openai import OpenAIAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.OPENAI, audit_path=str(path))

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "done"
        mock_response.model = "gpt-4o-mini"

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai_module = MagicMock()
        mock_openai_module.AsyncOpenAI = MagicMock(return_value=mock_client)

        adapter = OpenAIAdapter(MockOpenAIAgent(), config)

        with patch.dict("sys.modules", {"agents": None, "openai": mock_openai_module}):
            await adapter.ainvoke({"message": "test"})

        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_start" in events

    def test_describe_returns_string(self, tmp_path: Path) -> None:
        from wire.adapters.openai import OpenAIAdapter
        config = DeployConfig(backend=Backend.OPENAI, audit_path=str(tmp_path / "a.jsonl"))
        adapter = OpenAIAdapter(MockOpenAIAgent(name="cost-analyst"), config)
        desc = adapter.describe()
        assert "openai" in desc.lower()
        assert "cost-analyst" in desc


# ── deploy() routing conformance ─────────────────────────────────────────────

class TestDeployRouting:
    def test_deploy_langgraph_returns_adapter(self, tmp_path: Path) -> None:
        from wire.adapters.langgraph import LangGraphAdapter
        workforce = deploy(MockLangGraphGraph(), backend="langgraph",
                           audit_path=str(tmp_path / "a.jsonl"))
        assert isinstance(workforce, LangGraphAdapter)

    def test_deploy_crewai_returns_adapter(self, tmp_path: Path) -> None:
        from wire.adapters.crewai import CrewAIAdapter
        workforce = deploy(MockCrew(), backend="crewai",
                           audit_path=str(tmp_path / "a.jsonl"))
        assert isinstance(workforce, CrewAIAdapter)

    def test_deploy_autogen_returns_adapter(self, tmp_path: Path) -> None:
        from wire.adapters.autogen import AutoGenAdapter
        workforce = deploy({}, backend="autogen",
                           audit_path=str(tmp_path / "a.jsonl"))
        assert isinstance(workforce, AutoGenAdapter)

    def test_deploy_openai_returns_adapter(self, tmp_path: Path) -> None:
        from wire.adapters.openai import OpenAIAdapter
        workforce = deploy(MockOpenAIAgent(), backend="openai",
                           audit_path=str(tmp_path / "a.jsonl"))
        assert isinstance(workforce, OpenAIAdapter)

    def test_all_adapters_have_ainvoke(self, tmp_path: Path) -> None:
        path = str(tmp_path / "a.jsonl")
        adapters = [
            deploy(MockLangGraphGraph(), backend="langgraph", audit_path=path),
            deploy(MockCrew(), backend="crewai", audit_path=path),
            deploy({}, backend="autogen", audit_path=path),
            deploy(MockOpenAIAgent(), backend="openai", audit_path=path),
        ]
        for adapter in adapters:
            assert hasattr(adapter, "ainvoke")
            assert hasattr(adapter, "on")
            assert hasattr(adapter, "describe")

    def test_all_adapters_describe_returns_string(self, tmp_path: Path) -> None:
        path = str(tmp_path / "a.jsonl")
        adapters = [
            deploy(MockLangGraphGraph(), backend="langgraph", audit_path=path),
            deploy(MockCrew(), backend="crewai", audit_path=path),
            deploy({}, backend="autogen", audit_path=path),
            deploy(MockOpenAIAgent(), backend="openai", audit_path=path),
        ]
        for adapter in adapters:
            desc = adapter.describe()
            assert isinstance(desc, str)
            assert len(desc) > 0
