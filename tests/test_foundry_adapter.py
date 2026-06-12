"""
Microsoft Foundry adapter conformance tests.

Tests the full adapter contract without requiring Azure credentials or
a real Foundry deployment. All Foundry SDK objects are mocked to match
the exact azure-ai-agents v1.1 API shapes discovered from the SDK research.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wire.core.errors import LoopBreachError
from wire.core.models import Backend, DeployConfig
from wire.deploy import deploy


# ── Mock Foundry SDK objects (matching azure-ai-agents v1.1 shapes) ───────────

def _make_run(
    status: str = "completed",
    run_id: str = "run_abc123",
    usage_prompt: int = 500,
    usage_completion: int = 200,
    required_action: Any = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.status = status
    run.last_error = None
    run.required_action = required_action
    usage = MagicMock()
    usage.prompt_tokens = usage_prompt
    usage.completion_tokens = usage_completion
    usage.total_tokens = usage_prompt + usage_completion
    run.usage = usage
    return run


def _make_message(content: str = "Analysis complete.") -> MagicMock:
    msg = MagicMock()
    msg.role = "assistant"
    text_block = MagicMock()
    text_block.text = MagicMock()
    text_block.text.value = content
    msg.content = [text_block]
    return msg


def _make_tool_call(
    tc_id: str = "call_001",
    name: str = "get_metrics",
    arguments: str = '{"service": "aws"}',
) -> MagicMock:
    tc = MagicMock()
    tc.id = tc_id
    tc.type = "function"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_requires_action(tool_calls: list) -> MagicMock:
    action = MagicMock()
    action.type = "submit_tool_outputs"
    action.submit_tool_outputs = MagicMock()
    action.submit_tool_outputs.tool_calls = tool_calls
    return action


class _AsyncMessageIter:
    """Async iterator yielding one message."""
    def __init__(self, msg): self._msg = msg; self._done = False
    def __aiter__(self): return self
    async def __anext__(self):
        if self._done: raise StopAsyncIteration
        self._done = True
        return self._msg


def _make_foundry_client(
    run_statuses: list[str] | None = None,
    message_content: str = "Analysis complete.",
    tool_calls: list | None = None,
) -> AsyncMock:
    """Build a fully mocked async AgentsClient."""
    client = AsyncMock()

    # threads.create
    thread = MagicMock(); thread.id = "thread_xyz"
    client.threads.create = AsyncMock(return_value=thread)

    # messages.create
    client.messages.create = AsyncMock(return_value=MagicMock())

    # messages.list — returns async iterable
    client.messages.list = AsyncMock(
        return_value=_AsyncMessageIter(_make_message(message_content))
    )

    if tool_calls:
        # Sequence: create → requires_action; get → completed (after submit)
        req_action = _make_requires_action(tool_calls)
        requires_run = _make_run("requires_action", required_action=req_action)
        completed_run = _make_run("completed")

        client.runs.create = AsyncMock(return_value=requires_run)
        # After submit_tool_outputs, polling returns completed
        client.runs.get = AsyncMock(return_value=completed_run)
        client.runs.submit_tool_outputs = AsyncMock(return_value=completed_run)
    else:
        statuses = run_statuses or ["completed"]
        runs_seq = [_make_run(s) for s in statuses]
        # create returns first; subsequent gets return the rest
        client.runs.create = AsyncMock(return_value=runs_seq[0])
        get_seq = iter(runs_seq[1:] + [_make_run("completed")])
        client.runs.get = AsyncMock(side_effect=lambda **kw: next(get_seq, _make_run("completed")))
        client.runs.submit_tool_outputs = AsyncMock(return_value=_make_run("completed"))

    return client


# ── Foundry adapter conformance tests ────────────────────────────────────────

class TestFoundryAdapterConformance:
    @pytest.mark.asyncio
    async def test_ainvoke_returns_dict_with_output(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        client = _make_foundry_client()
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        result = await adapter.ainvoke({"message": "Analyse costs"})
        assert isinstance(result, dict)
        assert "output" in result
        assert result["output"] == "Analysis complete."

    @pytest.mark.asyncio
    async def test_audit_chain_written(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(path))
        client = _make_foundry_client()
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        await adapter.ainvoke({"message": "test"})
        assert path.exists()
        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_start" in events
        assert "workforce_end" in events
        assert "run_created" in events
        assert "run_completed" in events

    @pytest.mark.asyncio
    async def test_thread_created_when_not_provided(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        client = _make_foundry_client()
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        await adapter.ainvoke({"message": "test"})
        client.threads.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_thread_reused(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        client = _make_foundry_client()
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        await adapter.ainvoke({"message": "test"}, thread_id="thread_existing")
        # threads.create should NOT be called when thread_id provided
        client.threads.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_breach_raised_on_runaway(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        # Client that always returns in_progress — never completes
        client = _make_foundry_client()
        in_progress_run = _make_run("in_progress")
        client.runs.create = AsyncMock(return_value=in_progress_run)
        client.runs.get = AsyncMock(return_value=in_progress_run)

        config = DeployConfig(
            backend=Backend.FOUNDRY,
            audit_path=str(tmp_path / "a.jsonl"),
            max_iterations=3,
        )
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        with pytest.raises(LoopBreachError):
            await adapter.ainvoke({"message": "test"})

    @pytest.mark.asyncio
    async def test_failed_run_raises_error(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter, FoundryRunFailedError
        client = _make_foundry_client(run_statuses=["failed"])
        # Create returns failed run immediately
        failed_run = _make_run("failed")
        failed_run.last_error = "model_error"
        client.runs.create = AsyncMock(return_value=failed_run)

        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        with pytest.raises(FoundryRunFailedError) as exc_info:
            await adapter.ainvoke({"message": "test"})
        assert exc_info.value.status == "failed"
        assert "model_error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_error_written_to_audit_on_failure(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter, FoundryRunFailedError
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(path))
        client = _make_foundry_client()
        failed_run = _make_run("failed")
        client.runs.create = AsyncMock(return_value=failed_run)
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        with pytest.raises(FoundryRunFailedError):
            await adapter.ainvoke({"message": "test"})

        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "workforce_error" in events

    def test_describe_returns_string(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter(
            {"endpoint": "https://myaccount.services.ai.azure.com/api/projects/myproject",
             "agent_id": "asst_abc", "credential": None},
            config,
        )
        desc = adapter.describe()
        assert "foundry" in desc.lower()
        assert "asst_abc" in desc
        assert "hitl" in desc.lower()
        assert "sla" in desc.lower()

    def test_on_returns_decorator(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        from wire.core.events import EventKind
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter({}, config)
        assert callable(adapter.on(EventKind.LOOP_BREACH))


class TestFoundryHITLIntegration:
    @pytest.mark.asyncio
    async def test_requires_action_handled(self, tmp_path: Path) -> None:
        """Foundry requires_action → WIRE routes through IdempotencyGuard → submits outputs."""
        from wire.adapters.foundry import FoundryAdapter

        tc = _make_tool_call("call_001", "get_aws_cost", '{"region": "us-east-1"}')
        client = _make_foundry_client(tool_calls=[tc])

        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        result = await adapter.ainvoke({"message": "check costs"})
        assert isinstance(result, dict)
        # submit_tool_outputs must have been called
        client.runs.submit_tool_outputs.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_call_idempotency(self, tmp_path: Path) -> None:
        """Same tool + same args should never execute twice."""
        from wire.adapters.foundry import FoundryAdapter

        execution_log: list[str] = []
        tc = _make_tool_call("call_001", "send_email", '{"to": "ops@co.com"}')
        client = _make_foundry_client(tool_calls=[tc])

        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        original_execute = adapter._execute_tool

        async def counting_execute(tool_name, args):
            execution_log.append(tool_name)
            return await original_execute(tool_name, args)

        adapter._execute_tool = counting_execute

        # Run twice with same effective tool call
        await adapter.ainvoke({"message": "send"})

        # Reinject same tool call — idempotency guard should catch it
        tc2 = _make_tool_call("call_002", "send_email", '{"to": "ops@co.com"}')
        action2 = _make_requires_action([tc2])
        completed_run = _make_run("completed")
        requires_run = _make_run("requires_action", required_action=action2)
        client.runs.create = AsyncMock(return_value=requires_run)
        client.runs.get = AsyncMock(return_value=completed_run)

        await adapter.ainvoke({"message": "send again"}, thread_id="thread_xyz")
        # Second run — tool should not execute again (idempotency)
        assert execution_log.count("send_email") == 1

    @pytest.mark.asyncio
    async def test_audit_records_tool_execution(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        path = tmp_path / "audit.jsonl"

        tc = _make_tool_call("call_001", "create_jira_ticket", '{"title": "P1"}')
        client = _make_foundry_client(tool_calls=[tc])
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(path))
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"

        await adapter.ainvoke({"message": "create ticket"})

        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        events = [l["event"] for l in lines]
        assert "requires_action" in events
        assert "tool_executed" in events


class TestFoundryDeployRouting:
    def test_deploy_foundry_returns_adapter(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        workforce = deploy(
            {"endpoint": "https://test.ai.azure.com/api/projects/p",
             "agent_id": "asst_x", "credential": None},
            backend="foundry",
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert isinstance(workforce, FoundryAdapter)

    def test_foundry_has_all_contract_methods(self, tmp_path: Path) -> None:
        workforce = deploy({}, backend="foundry",
                           audit_path=str(tmp_path / "a.jsonl"))
        assert hasattr(workforce, "ainvoke")
        assert hasattr(workforce, "on")
        assert hasattr(workforce, "describe")

    def test_foundry_in_backend_enum(self) -> None:
        assert Backend.FOUNDRY == "foundry"

    def test_deploy_foundry_with_pre_built_client(self, tmp_path: Path) -> None:
        from wire.adapters.foundry import FoundryAdapter
        mock_client = AsyncMock()
        config = DeployConfig(
            backend=Backend.FOUNDRY,
            audit_path=str(tmp_path / "a.jsonl"),
            extra={"agent_id": "asst_abc"},
        )
        adapter = FoundryAdapter(mock_client, config)
        assert isinstance(adapter, FoundryAdapter)
        assert adapter._client is mock_client
