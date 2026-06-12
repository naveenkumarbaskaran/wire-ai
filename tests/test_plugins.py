"""
Plugin system tests — PluginRegistry, AgentLensPlugin, TokmonPlugin,
and LangGraph adapter integration.

All tests work without agentlens or tokmon installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wire.plugins import (
    AgentLensPlugin,
    PluginRegistry,
    TokmonPlugin,
    WIREPlugin,
    get_plugin_registry,
)


# ── Helpers / stubs ───────────────────────────────────────────────────────────

class RecordingPlugin(WIREPlugin):
    """Concrete plugin that records every call for assertion in tests."""

    name = "recorder"
    version = "0.1.0"

    def __init__(self) -> None:
        self.step_starts: list[dict[str, Any]] = []
        self.step_ends: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.workforce_ends: list[dict[str, Any]] = []

    async def on_step_start(self, run_id: str, role: str, iteration: int) -> None:
        self.step_starts.append({"run_id": run_id, "role": role, "iteration": iteration})

    async def on_step_end(
        self,
        run_id: str,
        role: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        self.step_ends.append(
            {"run_id": run_id, "role": role, "cost_usd": cost_usd,
             "tokens_in": tokens_in, "tokens_out": tokens_out}
        )

    async def on_tool_call(self, run_id: str, tool: str, args: dict[str, Any]) -> None:
        self.tool_calls.append({"run_id": run_id, "tool": tool, "args": args})

    async def on_workforce_end(
        self, run_id: str, total_cost: float, iterations: int
    ) -> None:
        self.workforce_ends.append(
            {"run_id": run_id, "total_cost": total_cost, "iterations": iterations}
        )


class BombPlugin(WIREPlugin):
    """Plugin that always raises — verifies registry swallows exceptions."""

    name = "bomb"
    version = "0.1.0"

    async def on_step_start(self, run_id: str, role: str, iteration: int) -> None:
        raise RuntimeError("intentional bomb")

    async def on_step_end(self, run_id: str, role: str, cost_usd: float,
                          tokens_in: int, tokens_out: int) -> None:
        raise RuntimeError("intentional bomb")

    async def on_tool_call(self, run_id: str, tool: str, args: dict[str, Any]) -> None:
        raise RuntimeError("intentional bomb")

    async def on_workforce_end(self, run_id: str, total_cost: float, iterations: int) -> None:
        raise RuntimeError("intentional bomb")


class _MockLangGraphGraph:
    """Minimal LangGraph CompiledGraph stub for integration tests."""

    async def astream(self, input: dict[str, Any], **kwargs: Any):  # type: ignore[override]
        yield {"agent": {"messages": [_mock_message(100, 50)]}}
        yield {"agent": {"messages": [_mock_message(80, 40)]}}


def _mock_message(in_tokens: int, out_tokens: int) -> MagicMock:
    m = MagicMock()
    m.usage_metadata = {"input_tokens": in_tokens, "output_tokens": out_tokens}
    return m


# ── PluginRegistry — registration ────────────────────────────────────────────

class TestPluginRegistryRegistration:
    def test_register_adds_plugin(self) -> None:
        registry = PluginRegistry()
        plugin = RecordingPlugin()
        registry.register(plugin)
        assert registry.get("recorder") is plugin

    def test_list_plugins_returns_all(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p2 = AgentLensPlugin()
        registry.register(p1)
        registry.register(p2)
        listed = registry.list_plugins()
        assert p1 in listed
        assert p2 in listed

    def test_register_replaces_same_name(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p2 = RecordingPlugin()
        registry.register(p1)
        registry.register(p2)
        # Should only have one entry; the newest wins
        assert registry.get("recorder") is p2
        assert len(registry.list_plugins()) == 1

    def test_unregister_removes_plugin(self) -> None:
        registry = PluginRegistry()
        registry.register(RecordingPlugin())
        registry.unregister("recorder")
        assert len(registry.list_plugins()) == 0

    def test_unregister_noop_for_missing_name(self) -> None:
        registry = PluginRegistry()
        # Must not raise
        registry.unregister("does_not_exist")

    def test_get_raises_key_error_for_missing(self) -> None:
        registry = PluginRegistry()
        with pytest.raises(KeyError):
            registry.get("missing")


# ── PluginRegistry — fan-out emit ────────────────────────────────────────────

class TestPluginRegistryEmit:
    @pytest.mark.asyncio
    async def test_emit_step_start_fans_out(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p1.name = "r1"
        p2 = RecordingPlugin()
        p2.name = "r2"
        registry.register(p1)
        registry.register(p2)

        await registry.emit_step_start(run_id="x", role="agent", iteration=1)

        assert len(p1.step_starts) == 1
        assert len(p2.step_starts) == 1

    @pytest.mark.asyncio
    async def test_emit_step_end_fans_out(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p1.name = "r1"
        p2 = RecordingPlugin()
        p2.name = "r2"
        registry.register(p1)
        registry.register(p2)

        await registry.emit_step_end(
            run_id="x", role="agent", cost_usd=0.01, tokens_in=100, tokens_out=50
        )

        assert p1.step_ends[0]["cost_usd"] == 0.01
        assert p2.step_ends[0]["tokens_in"] == 100

    @pytest.mark.asyncio
    async def test_emit_tool_call_fans_out(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p1.name = "r1"
        p2 = RecordingPlugin()
        p2.name = "r2"
        registry.register(p1)
        registry.register(p2)

        await registry.emit_tool_call(run_id="x", tool="search", args={"q": "test"})

        assert p1.tool_calls[0]["tool"] == "search"
        assert p2.tool_calls[0]["args"] == {"q": "test"}

    @pytest.mark.asyncio
    async def test_emit_workforce_end_fans_out(self) -> None:
        registry = PluginRegistry()
        p1 = RecordingPlugin()
        p1.name = "r1"
        p2 = RecordingPlugin()
        p2.name = "r2"
        registry.register(p1)
        registry.register(p2)

        await registry.emit_workforce_end(run_id="x", total_cost=1.23, iterations=7)

        assert p1.workforce_ends[0]["total_cost"] == 1.23
        assert p2.workforce_ends[0]["iterations"] == 7

    @pytest.mark.asyncio
    async def test_plugin_not_called_after_unregister(self) -> None:
        registry = PluginRegistry()
        plugin = RecordingPlugin()
        registry.register(plugin)
        registry.unregister("recorder")

        await registry.emit_step_start(run_id="x", role="agent", iteration=1)
        await registry.emit_step_end(
            run_id="x", role="agent", cost_usd=0.0, tokens_in=0, tokens_out=0
        )
        await registry.emit_tool_call(run_id="x", tool="t", args={})
        await registry.emit_workforce_end(run_id="x", total_cost=0.0, iterations=0)

        assert plugin.step_starts == []
        assert plugin.step_ends == []
        assert plugin.tool_calls == []
        assert plugin.workforce_ends == []

    @pytest.mark.asyncio
    async def test_bomb_plugin_does_not_raise(self) -> None:
        """A plugin that always raises must never crash the registry."""
        registry = PluginRegistry()
        registry.register(BombPlugin())
        # None of these should raise
        await registry.emit_step_start(run_id="x", role="agent", iteration=1)
        await registry.emit_step_end(
            run_id="x", role="agent", cost_usd=0.0, tokens_in=0, tokens_out=0
        )
        await registry.emit_tool_call(run_id="x", tool="t", args={})
        await registry.emit_workforce_end(run_id="x", total_cost=0.0, iterations=1)

    @pytest.mark.asyncio
    async def test_multiple_plugins_receive_same_events(self) -> None:
        """Three plugins all receive the same payload — values are identical."""
        registry = PluginRegistry()
        plugins = []
        for i in range(3):
            p = RecordingPlugin()
            p.name = f"r{i}"
            plugins.append(p)
            registry.register(p)

        await registry.emit_step_end(
            run_id="run-42", role="planner", cost_usd=0.005, tokens_in=200, tokens_out=80
        )

        for p in plugins:
            assert p.step_ends[0]["run_id"] == "run-42"
            assert p.step_ends[0]["role"] == "planner"
            assert p.step_ends[0]["cost_usd"] == 0.005


# ── get_plugin_registry ───────────────────────────────────────────────────────

class TestGetPluginRegistry:
    def test_returns_same_instance(self) -> None:
        r1 = get_plugin_registry()
        r2 = get_plugin_registry()
        assert r1 is r2

    def test_is_plugin_registry_instance(self) -> None:
        assert isinstance(get_plugin_registry(), PluginRegistry)


# ── AgentLensPlugin ───────────────────────────────────────────────────────────

class TestAgentLensPlugin:
    def test_is_available_false_when_not_installed(self) -> None:
        """is_available() must return False when agentlens is not installed
        and the API server is unreachable."""
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            # httpx will get a connection refused → returns False
            result = plugin.is_available()
        assert result is False

    def test_is_available_true_when_package_present(self) -> None:
        mock_agentlens = MagicMock()
        with patch.dict(sys.modules, {"agentlens": mock_agentlens}):
            plugin = AgentLensPlugin()
            assert plugin.is_available() is True

    @pytest.mark.asyncio
    async def test_on_step_start_does_not_raise_without_package(self) -> None:
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            # httpx POST will fail silently — should not raise
            await plugin.on_step_start(run_id="r1", role="agent", iteration=1)

    @pytest.mark.asyncio
    async def test_on_step_end_does_not_raise_without_package(self) -> None:
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            await plugin.on_step_end(
                run_id="r1", role="agent", cost_usd=0.01, tokens_in=50, tokens_out=20
            )

    @pytest.mark.asyncio
    async def test_on_tool_call_does_not_raise_without_package(self) -> None:
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            await plugin.on_tool_call(run_id="r1", tool="search", args={"q": "test"})

    @pytest.mark.asyncio
    async def test_on_workforce_end_does_not_raise_without_package(self) -> None:
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            await plugin.on_workforce_end(run_id="r1", total_cost=0.5, iterations=10)

    @pytest.mark.asyncio
    async def test_uses_sdk_when_available(self) -> None:
        """When agentlens is importable and exposes the expected API, SDK path is used."""
        mock_agentlens = MagicMock()
        mock_agentlens.record_step_start = MagicMock()
        mock_agentlens.record_step_end = MagicMock()
        mock_agentlens.record_tool_call = MagicMock()
        mock_agentlens.flush_session = MagicMock()

        with patch.dict(sys.modules, {"agentlens": mock_agentlens}):
            plugin = AgentLensPlugin()
            await plugin.on_step_start(run_id="r1", role="agent", iteration=1)
            await plugin.on_step_end(
                run_id="r1", role="agent", cost_usd=0.002, tokens_in=100, tokens_out=40
            )
            await plugin.on_tool_call(run_id="r1", tool="fetch", args={})
            await plugin.on_workforce_end(run_id="r1", total_cost=0.002, iterations=1)

        mock_agentlens.record_step_start.assert_called_once()
        mock_agentlens.record_step_end.assert_called_once()
        mock_agentlens.record_tool_call.assert_called_once()
        mock_agentlens.flush_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_tracks_span_duration(self) -> None:
        """on_step_start records a start time; on_step_end uses it for duration_ms."""
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        with patch.dict(sys.modules, {"agentlens": None}):
            await plugin.on_step_start(run_id="r1", role="agent", iteration=1)
            assert "agent" in plugin._spans.get("r1", {})
            await plugin.on_step_end(
                run_id="r1", role="agent", cost_usd=0.0, tokens_in=0, tokens_out=0
            )
            # span should be consumed after step_end
            assert "agent" not in plugin._spans.get("r1", {})

    @pytest.mark.asyncio
    async def test_workforce_end_cleans_up_spans(self) -> None:
        plugin = AgentLensPlugin(api_url="http://localhost:19999")
        plugin._spans["r1"] = {"agent": 1234.5}
        with patch.dict(sys.modules, {"agentlens": None}):
            await plugin.on_workforce_end(run_id="r1", total_cost=0.0, iterations=1)
        assert "r1" not in plugin._spans


# ── TokmonPlugin ──────────────────────────────────────────────────────────────

class TestTokmonPlugin:
    def test_is_available_false_when_not_installed(self) -> None:
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            assert plugin.is_available() is False

    def test_is_available_true_when_package_present(self) -> None:
        mock_tokmon = MagicMock()
        with patch.dict(sys.modules, {"tokmon": mock_tokmon}):
            plugin = TokmonPlugin()
            assert plugin.is_available() is True

    @pytest.mark.asyncio
    async def test_on_step_end_records_to_ledger(self) -> None:
        """Even without tokmon installed, CostLedger receives the record."""
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            await plugin.on_step_end(
                run_id="r1", role="planner", cost_usd=0.003,
                tokens_in=150, tokens_out=60,
            )
        assert plugin._ledger.by_run("r1") == pytest.approx(0.003)

    @pytest.mark.asyncio
    async def test_on_step_start_does_not_raise(self) -> None:
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            await plugin.on_step_start(run_id="r1", role="agent", iteration=3)

    @pytest.mark.asyncio
    async def test_on_tool_call_does_not_raise(self) -> None:
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            await plugin.on_tool_call(run_id="r1", tool="search", args={"q": "test"})

    @pytest.mark.asyncio
    async def test_on_workforce_end_does_not_raise_without_package(self) -> None:
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            await plugin.on_workforce_end(run_id="r1", total_cost=0.1, iterations=5)

    @pytest.mark.asyncio
    async def test_uses_tokmon_session_when_available(self) -> None:
        """When tokmon is importable, TokmonPlugin creates a session and records to it."""
        mock_session = MagicMock()
        mock_session.record = MagicMock()
        mock_session.flush = MagicMock()

        mock_tokmon = MagicMock()
        mock_tokmon.Session = MagicMock(return_value=mock_session)

        with patch.dict(sys.modules, {"tokmon": mock_tokmon}):
            plugin = TokmonPlugin(session_name="test-run")
            await plugin.on_step_end(
                run_id="r1", role="agent", cost_usd=0.002, tokens_in=100, tokens_out=40
            )
            await plugin.on_workforce_end(run_id="r1", total_cost=0.002, iterations=1)

        mock_session.record.assert_called_once()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_ledger_accumulates_across_steps(self) -> None:
        """Multiple on_step_end calls accumulate in the fallback CostLedger."""
        with patch.dict(sys.modules, {"tokmon": None}):
            plugin = TokmonPlugin()
            await plugin.on_step_end(
                run_id="r1", role="planner", cost_usd=0.001,
                tokens_in=50, tokens_out=20,
            )
            await plugin.on_step_end(
                run_id="r1", role="executor", cost_usd=0.002,
                tokens_in=100, tokens_out=40,
            )
        assert plugin._ledger.total_usd == pytest.approx(0.003)
        assert plugin._ledger.by_run("r1") == pytest.approx(0.003)

    @pytest.mark.asyncio
    async def test_budget_passed_to_session(self) -> None:
        """budget_usd kwarg is forwarded to the tokmon Session constructor."""
        mock_session = MagicMock()
        mock_session.record = MagicMock()

        mock_tokmon = MagicMock()
        captured: list[dict[str, Any]] = []

        def capture_session(**kwargs: Any) -> MagicMock:
            captured.append(kwargs)
            return mock_session

        mock_tokmon.Session = MagicMock(side_effect=capture_session)

        with patch.dict(sys.modules, {"tokmon": mock_tokmon}):
            plugin = TokmonPlugin(budget_usd=2.5)
            await plugin.on_step_end(
                run_id="r1", role="agent", cost_usd=0.001, tokens_in=0, tokens_out=0
            )

        assert captured[0].get("budget_usd") == 2.5


# ── LangGraph adapter — plugin integration ───────────────────────────────────

class TestLangGraphAdapterPluginIntegration:
    @pytest.mark.asyncio
    async def test_adapter_emits_step_end_to_registry(self, tmp_path: Path) -> None:
        """LangGraph adapter must call emit_step_end on the process-wide registry."""
        from wire.adapters.langgraph import LangGraphAdapter
        from wire.core.models import DeployConfig
        from wire.plugins import get_plugin_registry

        registry = get_plugin_registry()
        recorder = RecordingPlugin()
        recorder.name = "lg_test_recorder"
        registry.register(recorder)

        try:
            config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
            adapter = LangGraphAdapter(_MockLangGraphGraph(), config)
            await adapter.ainvoke({"messages": []})

            assert len(recorder.step_ends) > 0
        finally:
            registry.unregister("lg_test_recorder")

    @pytest.mark.asyncio
    async def test_adapter_emits_workforce_end_to_registry(self, tmp_path: Path) -> None:
        """LangGraph adapter must call emit_workforce_end once per run."""
        from wire.adapters.langgraph import LangGraphAdapter
        from wire.core.models import DeployConfig
        from wire.plugins import get_plugin_registry

        registry = get_plugin_registry()
        recorder = RecordingPlugin()
        recorder.name = "lg_we_recorder"
        registry.register(recorder)

        try:
            config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
            adapter = LangGraphAdapter(_MockLangGraphGraph(), config)
            await adapter.ainvoke({"messages": []})

            assert len(recorder.workforce_ends) == 1
        finally:
            registry.unregister("lg_we_recorder")

    @pytest.mark.asyncio
    async def test_adapter_bomb_plugin_does_not_break_run(self, tmp_path: Path) -> None:
        """A misbehaving plugin must not cause the LangGraph adapter to fail."""
        from wire.adapters.langgraph import LangGraphAdapter
        from wire.core.models import DeployConfig
        from wire.plugins import get_plugin_registry

        registry = get_plugin_registry()
        bomb = BombPlugin()
        bomb.name = "lg_bomb"
        registry.register(bomb)

        try:
            config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
            adapter = LangGraphAdapter(_MockLangGraphGraph(), config)
            result = await adapter.ainvoke({"messages": []})
            assert isinstance(result, dict)
        finally:
            registry.unregister("lg_bomb")

    @pytest.mark.asyncio
    async def test_adapter_emits_step_start_before_step_end(self, tmp_path: Path) -> None:
        """emit_step_start must be called the same number of times as emit_step_end."""
        from wire.adapters.langgraph import LangGraphAdapter
        from wire.core.models import DeployConfig
        from wire.plugins import get_plugin_registry

        registry = get_plugin_registry()
        recorder = RecordingPlugin()
        recorder.name = "lg_order_recorder"
        registry.register(recorder)

        try:
            config = DeployConfig(audit_path=str(tmp_path / "audit.jsonl"))
            adapter = LangGraphAdapter(_MockLangGraphGraph(), config)
            await adapter.ainvoke({"messages": []})

            assert len(recorder.step_starts) == len(recorder.step_ends)
        finally:
            registry.unregister("lg_order_recorder")
