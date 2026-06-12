"""Tests for Sprint 6 Enterprise — compliance, RBAC, multi-tenancy, backends."""

from __future__ import annotations

import pytest

from wire.enterprise.compliance import (
    CompliancePreset, ComplianceConfig,
    SOC2_CONFIG, HIPAA_CONFIG, GDPR_CONFIG, NIST_AI_RMF_CONFIG,
)
from wire.enterprise.rbac import (
    Actor, Permission, PermissionDeniedError, RBACPolicy,
)
from wire.enterprise.multitenancy import (
    Tenant, TenantNotFoundError, TenantRegistry,
)
from wire.enterprise.backends import AuditBackendError, S3AuditBackend, PostgresAuditBackend
from wire.core.models import DeployConfig


# ── Compliance presets ────────────────────────────────────────────────────────

class TestCompliancePresets:
    def test_all_four_presets_exist(self) -> None:
        for preset in CompliancePreset:
            cfg = preset.config()
            assert isinstance(cfg, ComplianceConfig)

    def test_soc2_retention_365_days(self) -> None:
        assert SOC2_CONFIG.audit.retention.days == 365

    def test_hipaa_retention_6_years(self) -> None:
        assert HIPAA_CONFIG.audit.retention.days == 2190

    def test_hipaa_geo_restriction_us(self) -> None:
        assert HIPAA_CONFIG.audit.retention.geo_restriction == "US"

    def test_gdpr_geo_restriction_eu(self) -> None:
        assert GDPR_CONFIG.audit.retention.geo_restriction == "EU"

    def test_hipaa_prohibits_phi(self) -> None:
        assert "phi" in HIPAA_CONFIG.prohibited_data_categories

    def test_gdpr_prohibits_pii(self) -> None:
        assert "pii" in GDPR_CONFIG.prohibited_data_categories

    def test_all_presets_encrypt_at_rest(self) -> None:
        for preset in CompliancePreset:
            cfg = preset.config()
            assert cfg.audit.retention.encrypt_at_rest, \
                f"{preset} should encrypt at rest"

    def test_all_presets_require_role_contracts(self) -> None:
        for preset in CompliancePreset:
            cfg = preset.config()
            assert cfg.require_role_contracts

    def test_all_presets_require_policy_enforcement(self) -> None:
        for preset in CompliancePreset:
            cfg = preset.config()
            assert cfg.require_policy_enforcement

    def test_soc2_hitl_for_high_risk(self) -> None:
        assert "high" in SOC2_CONFIG.hitl.require_for_risk

    def test_hipaa_hitl_for_medium_and_above(self) -> None:
        assert "medium" in HIPAA_CONFIG.hitl.require_for_risk
        assert "high" in HIPAA_CONFIG.hitl.require_for_risk
        assert "critical" in HIPAA_CONFIG.hitl.require_for_risk

    def test_summary_returns_string(self) -> None:
        for preset in CompliancePreset:
            s = preset.summary()
            assert isinstance(s, str)
            assert len(s) > 10

    def test_hipaa_min_confidence_highest(self) -> None:
        assert HIPAA_CONFIG.min_confidence >= SOC2_CONFIG.min_confidence

    def test_nist_ai_exists_and_has_requirements(self) -> None:
        cfg = CompliancePreset.NIST_AI.config()
        assert cfg.audit.log_all_tool_calls
        assert cfg.audit.hash_chain_verified


# ── RBAC ─────────────────────────────────────────────────────────────────────

class TestRBAC:
    def test_admin_group_has_all_permissions(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="admin@co.com", groups=["wire-admins"])
        for perm in Permission:
            assert policy.can(actor, perm)

    def test_engineer_can_deploy(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="eng@co.com", groups=["wire-engineers"])
        assert policy.can(actor, Permission.DEPLOY)

    def test_engineer_cannot_approve_hitl(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="eng@co.com", groups=["wire-engineers"])
        assert not policy.can(actor, Permission.APPROVE_HITL)

    def test_manager_can_approve_hitl(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="mgr@co.com", groups=["wire-managers"])
        assert policy.can(actor, Permission.APPROVE_HITL)

    def test_require_raises_on_denied(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="eng@co.com", groups=["wire-engineers"])
        with pytest.raises(PermissionDeniedError) as exc_info:
            policy.require(actor, Permission.APPROVE_HITL)
        assert exc_info.value.actor_id == "eng@co.com"
        assert exc_info.value.permission == Permission.APPROVE_HITL

    def test_require_passes_for_allowed(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="eng@co.com", groups=["wire-engineers"])
        policy.require(actor, Permission.DEPLOY)  # must not raise

    def test_actor_override_grants_permission(self) -> None:
        policy = RBACPolicy()
        policy.grant_actor(
            actor_id="svc-account@co.com",
            permissions=[Permission.DEPLOY, Permission.VIEW_AUDIT],
        )
        actor = Actor(id="svc-account@co.com", groups=[])
        assert policy.can(actor, Permission.DEPLOY)
        assert not policy.can(actor, Permission.APPROVE_HITL)

    def test_no_groups_no_permissions(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="unknown@co.com", groups=[])
        for perm in Permission:
            assert not policy.can(actor, perm)

    def test_permissions_for_returns_list(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="eng@co.com", groups=["wire-engineers"])
        perms = policy.permissions_for(actor)
        assert isinstance(perms, list)
        assert Permission.DEPLOY in perms
        assert Permission.APPROVE_HITL not in perms

    def test_custom_grant_works(self) -> None:
        policy = RBACPolicy()
        policy.grant(group="qa-team", permissions=[Permission.VIEW_WORKFORCE, Permission.VIEW_AUDIT])
        actor = Actor(id="qa@co.com", groups=["qa-team"])
        assert policy.can(actor, Permission.VIEW_AUDIT)
        assert not policy.can(actor, Permission.DEPLOY)

    def test_multiple_groups_union_permissions(self) -> None:
        policy = RBACPolicy.default()
        actor = Actor(id="lead@co.com", groups=["wire-engineers", "wire-managers"])
        assert policy.can(actor, Permission.DEPLOY)
        assert policy.can(actor, Permission.APPROVE_HITL)

    def test_permission_denied_error_message(self) -> None:
        err = PermissionDeniedError("user@co.com", Permission.EXPORT_AUDIT)
        assert "user@co.com" in str(err)
        assert "export" in str(err).lower()


# ── Multi-tenancy ─────────────────────────────────────────────────────────────

class TestMultiTenancy:
    def test_register_and_get_tenant(self) -> None:
        registry = TenantRegistry()
        tenant = Tenant(id="team-a", name="Team A")
        registry.register(tenant)
        assert registry.get("team-a").name == "Team A"

    def test_get_unknown_tenant_raises(self) -> None:
        registry = TenantRegistry()
        with pytest.raises(TenantNotFoundError) as exc_info:
            registry.get("nonexistent")
        assert "nonexistent" in str(exc_info.value)

    def test_scoped_audit_path(self) -> None:
        tenant = Tenant(id="team-a", name="A", audit_path="audits/team-a")
        path = tenant.scoped_audit_path()
        assert "team-a" in path
        assert path.endswith(".jsonl")

    def test_scoped_audit_path_with_run_id(self) -> None:
        tenant = Tenant(id="team-a", name="A", audit_path="audits/team-a")
        path = tenant.scoped_audit_path("run_123")
        assert "run_123" in path

    def test_deploy_config_applies_tenant_budget(self) -> None:
        tenant = Tenant(id="t1", name="T1", budget_daily_usd=5.0)
        base = DeployConfig(daily_budget_usd=100.0)
        result = tenant.deploy_config(base)
        assert result.daily_budget_usd == 5.0  # tenant more restrictive

    def test_deploy_config_keeps_base_if_more_restrictive(self) -> None:
        tenant = Tenant(id="t1", name="T1", budget_daily_usd=100.0)
        base = DeployConfig(daily_budget_usd=2.0)
        result = tenant.deploy_config(base)
        assert result.daily_budget_usd == 2.0  # base more restrictive

    def test_deploy_config_sets_tenant_audit_path(self) -> None:
        tenant = Tenant(id="t1", name="T1", audit_path="audits/t1")
        base = DeployConfig()
        result = tenant.deploy_config(base)
        assert "t1" in result.audit_path

    def test_list_tenants(self) -> None:
        registry = TenantRegistry()
        registry.register(Tenant(id="a", name="A"))
        registry.register(Tenant(id="b", name="B"))
        assert registry.count == 2
        names = {t.id for t in registry.list_tenants()}
        assert names == {"a", "b"}

    def test_deregister_removes_tenant(self) -> None:
        registry = TenantRegistry()
        registry.register(Tenant(id="x", name="X"))
        registry.deregister("x")
        assert registry.count == 0

    def test_each_tenant_gets_rbac(self) -> None:
        registry = TenantRegistry()
        registry.register(Tenant(id="t1", name="T1"))
        rbac = registry.rbac("t1")
        assert isinstance(rbac, RBACPolicy)

    def test_tenant_rbac_isolation(self) -> None:
        registry = TenantRegistry()
        policy_a = RBACPolicy()
        policy_a.grant(group="a-team", permissions=[Permission.DEPLOY])
        policy_b = RBACPolicy()

        registry.register(Tenant(id="a", name="A"), rbac=policy_a)
        registry.register(Tenant(id="b", name="B"), rbac=policy_b)

        actor = Actor(id="user@co.com", groups=["a-team"])
        assert registry.rbac("a").can(actor, Permission.DEPLOY)
        assert not registry.rbac("b").can(actor, Permission.DEPLOY)


# ── Backends (unit — no real S3/PG required) ─────────────────────────────────

class TestAuditBackends:
    def test_s3_backend_describe(self) -> None:
        backend = S3AuditBackend(bucket="my-audit-bucket", prefix="wire")
        desc = backend.describe()
        assert "my-audit-bucket" in desc
        assert "wire" in desc

    def test_s3_backend_raises_without_boto3(self) -> None:
        import sys
        from unittest.mock import patch
        backend = S3AuditBackend(bucket="test")
        with patch.dict(sys.modules, {"boto3": None}):
            with pytest.raises(AuditBackendError) as exc_info:
                backend._get_client()
            assert "boto3" in str(exc_info.value)

    def test_postgres_backend_describe(self) -> None:
        backend = PostgresAuditBackend(dsn="postgresql://user:pass@localhost/audit", tenant_id="t1")
        desc = backend.describe()
        assert "localhost" in desc
        assert "t1" in desc

    @pytest.mark.asyncio
    async def test_postgres_backend_raises_without_asyncpg(self) -> None:
        import sys
        backend = PostgresAuditBackend(dsn="postgresql://localhost/test")
        with pytest.raises(AuditBackendError) as exc_info:
            from unittest.mock import patch
            with patch.dict(sys.modules, {"asyncpg": None}):
                await backend._get_pool()
        assert "asyncpg" in str(exc_info.value)

    def test_s3_config_object_lock(self) -> None:
        backend = S3AuditBackend(bucket="b", object_lock=True, retention_days=730)
        assert backend.object_lock
        assert backend.retention_days == 730

    def test_s3_config_kms(self) -> None:
        backend = S3AuditBackend(bucket="b", kms_key_id="arn:aws:kms:us-east-1:123:key/abc")
        assert backend.kms_key_id is not None
