"""
Blueprint registry and Foundry adapter blueprint integration tests.

All tests run without Azure credentials. The Foundry client is fully mocked,
following the same patterns established in tests/test_foundry_adapter.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from wire.enterprise.blueprints import (
    AgentBlueprint,
    BlueprintNotFoundError,
    BlueprintRegistry,
    get_registry,
)
from wire.enterprise.rbac import Actor, PermissionDeniedError, RBACPolicy
from wire.core.models import Backend, DeployConfig


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_blueprint(
    bp_id: str = "cost-monitor-v1",
    name: str = "AWS Cost Monitor Agent",
    allowed_roles: list[str] | None = None,
) -> AgentBlueprint:
    return AgentBlueprint(
        id=bp_id,
        name=name,
        description="Monitors AWS spend and alerts on anomalies.",
        entra_app_id="a1b2c3d4-0000-0000-0000-000000000001",
        allowed_roles=allowed_roles if allowed_roles is not None else ["wire-engineers"],
        required_permissions=["Cost.Read"],
        compliance_preset="soc2",
        max_concurrent_instances=5,
    )


def _fresh_registry() -> BlueprintRegistry:
    """Return an empty registry (not the singleton) for isolation."""
    return BlueprintRegistry()


# ── Shared Foundry mock helpers (mirrors test_foundry_adapter.py) ─────────────

def _make_run(status: str = "completed") -> MagicMock:
    run = MagicMock()
    run.id = "run_abc"
    run.status = status
    run.last_error = None
    run.required_action = None
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    run.usage = usage
    return run


class _AsyncMsgIter:
    def __init__(self, msg): self._msg = msg; self._done = False
    def __aiter__(self): return self
    async def __anext__(self):
        if self._done: raise StopAsyncIteration
        self._done = True; return self._msg


def _make_message(content: str = "Done.") -> MagicMock:
    msg = MagicMock()
    msg.role = "assistant"
    tb = MagicMock()
    tb.text = MagicMock()
    tb.text.value = content
    msg.content = [tb]
    return msg


def _make_foundry_client() -> AsyncMock:
    client = AsyncMock()
    thread = MagicMock(); thread.id = "thread_xyz"
    client.threads.create = AsyncMock(return_value=thread)
    client.messages.create = AsyncMock(return_value=MagicMock())
    client.messages.list = AsyncMock(return_value=_AsyncMsgIter(_make_message()))
    client.runs.create = AsyncMock(return_value=_make_run("completed"))
    client.runs.get = AsyncMock(return_value=_make_run("completed"))
    client.runs.submit_tool_outputs = AsyncMock(return_value=_make_run("completed"))
    return client


# ── BlueprintRegistry unit tests ──────────────────────────────────────────────

class TestBlueprintRegistry:

    def test_register_and_get(self) -> None:
        reg = _fresh_registry()
        bp = _make_blueprint()
        reg.register(bp)
        retrieved = reg.get("cost-monitor-v1")
        assert retrieved.id == "cost-monitor-v1"
        assert retrieved.name == "AWS Cost Monitor Agent"

    def test_get_unknown_raises_blueprint_not_found(self) -> None:
        reg = _fresh_registry()
        with pytest.raises(BlueprintNotFoundError) as exc_info:
            reg.get("nonexistent-blueprint")
        assert "nonexistent-blueprint" in str(exc_info.value)

    def test_blueprint_not_found_error_attributes(self) -> None:
        err = BlueprintNotFoundError("my-bp")
        assert err.blueprint_id == "my-bp"
        assert "my-bp" in str(err)

    def test_list_blueprints_empty(self) -> None:
        reg = _fresh_registry()
        assert reg.list_blueprints() == []

    def test_list_blueprints_multiple(self) -> None:
        reg = _fresh_registry()
        bp1 = _make_blueprint("bp-1", "First")
        bp2 = _make_blueprint("bp-2", "Second")
        reg.register(bp1)
        reg.register(bp2)
        ids = {bp.id for bp in reg.list_blueprints()}
        assert ids == {"bp-1", "bp-2"}

    def test_deregister_removes_blueprint(self) -> None:
        reg = _fresh_registry()
        bp = _make_blueprint()
        reg.register(bp)
        assert reg.count == 1
        reg.deregister("cost-monitor-v1")
        assert reg.count == 0
        with pytest.raises(BlueprintNotFoundError):
            reg.get("cost-monitor-v1")

    def test_deregister_nonexistent_is_noop(self) -> None:
        """deregister() must not raise when ID is absent."""
        reg = _fresh_registry()
        reg.deregister("ghost-blueprint")  # should not raise

    def test_register_overwrites_existing(self) -> None:
        reg = _fresh_registry()
        reg.register(_make_blueprint("bp-1", "Original"))
        reg.register(_make_blueprint("bp-1", "Updated"))
        assert reg.get("bp-1").name == "Updated"
        assert reg.count == 1

    def test_count_property(self) -> None:
        reg = _fresh_registry()
        assert reg.count == 0
        reg.register(_make_blueprint("a"))
        assert reg.count == 1
        reg.register(_make_blueprint("b"))
        assert reg.count == 2


# ── check_deployment_allowed tests ───────────────────────────────────────────

class TestCheckDeploymentAllowed:

    def test_allowed_actor_passes(self) -> None:
        reg = _fresh_registry()
        bp = _make_blueprint(allowed_roles=["wire-engineers", "finops-team"])
        reg.register(bp)
        actor = Actor(id="naveen@co.com", groups=["wire-engineers"])
        policy = RBACPolicy.default()
        # Should not raise
        reg.check_deployment_allowed("cost-monitor-v1", actor, policy)

    def test_disallowed_actor_raises_permission_denied(self) -> None:
        reg = _fresh_registry()
        bp = _make_blueprint(allowed_roles=["finops-team"])
        reg.register(bp)
        actor = Actor(id="external@co.com", groups=["contractors"])
        policy = RBACPolicy.default()
        with pytest.raises(PermissionDeniedError) as exc_info:
            reg.check_deployment_allowed("cost-monitor-v1", actor, policy)
        assert "external@co.com" in str(exc_info.value)

    def test_admin_actor_bypasses_allowed_roles(self) -> None:
        """is_admin() actors always pass regardless of blueprint allowed_roles."""
        reg = _fresh_registry()
        bp = _make_blueprint(allowed_roles=["only-special-team"])
        reg.register(bp)
        admin = Actor(id="admin@co.com", groups=["wire-admins"])
        policy = RBACPolicy.default()
        # Should not raise
        reg.check_deployment_allowed("cost-monitor-v1", admin, policy)

    def test_check_deployment_unknown_blueprint_raises(self) -> None:
        reg = _fresh_registry()
        actor = Actor(id="user@co.com", groups=["wire-engineers"])
        policy = RBACPolicy.default()
        with pytest.raises(BlueprintNotFoundError):
            reg.check_deployment_allowed("ghost-bp", actor, policy)

    def test_actor_with_multiple_groups_one_allowed(self) -> None:
        """Actor in multiple groups — only one needs to match."""
        reg = _fresh_registry()
        bp = _make_blueprint(allowed_roles=["finops-team"])
        reg.register(bp)
        actor = Actor(id="bob@co.com", groups=["contractors", "finops-team", "readers"])
        policy = RBACPolicy()
        # Should not raise
        reg.check_deployment_allowed("cost-monitor-v1", actor, policy)

    def test_empty_allowed_roles_denies_non_admin(self) -> None:
        """Blueprint with no allowed_roles denies everyone except admins."""
        reg = _fresh_registry()
        bp = _make_blueprint(allowed_roles=[])
        reg.register(bp)
        actor = Actor(id="user@co.com", groups=["wire-engineers"])
        policy = RBACPolicy.default()
        with pytest.raises(PermissionDeniedError):
            reg.check_deployment_allowed("cost-monitor-v1", actor, policy)


# ── Default registry singleton tests ─────────────────────────────────────────

class TestDefaultRegistry:

    def test_get_registry_returns_instance(self) -> None:
        reg = get_registry()
        assert isinstance(reg, BlueprintRegistry)

    def test_get_registry_is_singleton(self) -> None:
        """Same object returned on repeated calls."""
        assert get_registry() is get_registry()

    def test_singleton_importable_from_enterprise(self) -> None:
        from wire.enterprise import get_registry as gr
        assert gr() is get_registry()


# ── Foundry adapter blueprint integration tests ───────────────────────────────

class TestFoundryAdapterBlueprintIntegration:
    """
    Tests that blueprint_id wires through the FoundryAdapter correctly.
    Uses a fresh BlueprintRegistry per test to avoid singleton state pollution.
    """

    def _adapter_with_blueprint(self, client, tmp_path: Path, bp_id: str):
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(
            backend=Backend.FOUNDRY,
            audit_path=str(tmp_path / "audit.jsonl"),
            extra={"blueprint_id": bp_id},
        )
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        return adapter

    @pytest.mark.asyncio
    async def test_blueprint_id_in_workforce_start_audit(self, tmp_path: Path) -> None:
        """workforce_start entry must include blueprint_id when set."""
        import wire.enterprise.blueprints as bm
        reg = _fresh_registry()
        reg.register(_make_blueprint("my-bp", "My Agent"))
        original = bm.get_registry
        bm.get_registry = lambda: reg

        try:
            path = tmp_path / "audit.jsonl"
            client = _make_foundry_client()
            adapter = self._adapter_with_blueprint(client, tmp_path, "my-bp")
            await adapter.ainvoke({"message": "test"})

            lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
            start = next(l for l in lines if l["event"] == "workforce_start")
            assert start["data"]["blueprint_id"] == "my-bp"
        finally:
            bm.get_registry = original

    @pytest.mark.asyncio
    async def test_blueprint_name_in_workforce_start_audit(self, tmp_path: Path) -> None:
        """workforce_start entry must include blueprint_name when blueprint is registered."""
        import wire.enterprise.blueprints as bm
        reg = _fresh_registry()
        reg.register(_make_blueprint("my-bp", "My Named Agent"))
        original = bm.get_registry
        bm.get_registry = lambda: reg

        try:
            path = tmp_path / "audit.jsonl"
            client = _make_foundry_client()
            adapter = self._adapter_with_blueprint(client, tmp_path, "my-bp")
            await adapter.ainvoke({"message": "test"})

            lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
            start = next(l for l in lines if l["event"] == "workforce_start")
            assert start["data"]["blueprint_name"] == "My Named Agent"
        finally:
            bm.get_registry = original

    @pytest.mark.asyncio
    async def test_no_blueprint_fields_when_not_set(self, tmp_path: Path) -> None:
        """workforce_start must NOT include blueprint fields when no blueprint_id configured."""
        from wire.adapters.foundry import FoundryAdapter
        path = tmp_path / "audit.jsonl"
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(path))
        client = _make_foundry_client()
        adapter = FoundryAdapter(client, config)
        adapter._agent_id = "asst_test"
        await adapter.ainvoke({"message": "test"})

        lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
        start = next(l for l in lines if l["event"] == "workforce_start")
        assert "blueprint_id" not in start["data"]
        assert "blueprint_name" not in start["data"]

    def test_describe_shows_blueprint_name(self, tmp_path: Path) -> None:
        """describe() output must include blueprint name and ID when set."""
        import wire.enterprise.blueprints as bm
        reg = _fresh_registry()
        reg.register(_make_blueprint("sales-v2", "Contoso Sales Agent"))
        original = bm.get_registry
        bm.get_registry = lambda: reg

        try:
            from wire.adapters.foundry import FoundryAdapter
            config = DeployConfig(
                backend=Backend.FOUNDRY,
                audit_path=str(tmp_path / "a.jsonl"),
                extra={"blueprint_id": "sales-v2"},
            )
            adapter = FoundryAdapter(
                {"endpoint": "https://test.ai.azure.com", "agent_id": "asst_abc", "credential": None},
                config,
            )
            desc = adapter.describe()
            assert "blueprint" in desc.lower()
            assert "Contoso Sales Agent" in desc
            assert "sales-v2" in desc
        finally:
            bm.get_registry = original

    def test_describe_no_blueprint_omits_line(self, tmp_path: Path) -> None:
        """describe() must not include a blueprint: line when no blueprint_id configured."""
        from wire.adapters.foundry import FoundryAdapter
        config = DeployConfig(backend=Backend.FOUNDRY, audit_path=str(tmp_path / "a.jsonl"))
        adapter = FoundryAdapter(
            {"endpoint": "https://test.ai.azure.com", "agent_id": "asst_abc", "credential": None},
            config,
        )
        desc = adapter.describe()
        # No "  blueprint      :" label should appear in the output lines
        assert not any(line.strip().startswith("blueprint") for line in desc.splitlines())


# ── AgentBlueprint model tests ────────────────────────────────────────────────

class TestAgentBlueprintModel:

    def test_blueprint_default_values(self) -> None:
        bp = AgentBlueprint(id="x", name="X", description="desc")
        assert bp.entra_app_id is None
        assert bp.allowed_roles == []
        assert bp.required_permissions == []
        assert bp.compliance_preset is None
        assert bp.max_concurrent_instances == 10
        assert bp.metadata == {}

    def test_blueprint_full_construction(self) -> None:
        bp = _make_blueprint()
        assert bp.compliance_preset == "soc2"
        assert bp.max_concurrent_instances == 5
        assert "wire-engineers" in bp.allowed_roles

    def test_blueprint_importable_from_wire(self) -> None:
        from wire import AgentBlueprint as AB, BlueprintRegistry as BR
        assert AB is AgentBlueprint
        assert BR is BlueprintRegistry
