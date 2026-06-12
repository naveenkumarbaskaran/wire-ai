"""
Multi-tenancy — isolated namespaces for teams, orgs, and environments.

Each tenant gets:
  - Isolated audit chain (separate path/table/bucket prefix)
  - Isolated durable state
  - Isolated budget tracking
  - Scoped RBAC policy
  - Separate event bus

Usage:
    from wire.enterprise.multitenancy import TenantRegistry, Tenant

    registry = TenantRegistry()
    registry.register(Tenant(
        id="team-vayu",
        name="Team Vayu",
        audit_path="audits/vayu/",
        budget_daily_usd=10.0,
    ))

    tenant = registry.get("team-vayu")
    config = tenant.deploy_config(base_config)
    # → audit_path = "audits/vayu/wire-audit.jsonl"
    # → budget scoped to team-vayu
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import WIREError
from wire.core.models import DeployConfig
from wire.enterprise.rbac import RBACPolicy

log = structlog.get_logger(__name__)


class TenantNotFoundError(WIREError):
    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"Tenant '{tenant_id}' not registered. Call registry.register() first.")


class Tenant(BaseModel):
    id: str
    name: str
    audit_path: str = "wire-audit.jsonl"
    state_namespace: str = ""
    budget_daily_usd: float | None = None
    budget_hourly_usd: float | None = None
    max_concurrent_workforces: int = 10
    metadata: dict[str, Any] = Field(default_factory=dict)

    def scoped_audit_path(self, run_id: str | None = None) -> str:
        """Return audit path scoped to this tenant."""
        base = self.audit_path.rstrip("/")
        if run_id:
            return f"{base}/{run_id}.jsonl"
        return f"{base}/wire-audit.jsonl"

    def deploy_config(self, base: DeployConfig) -> DeployConfig:
        """
        Return a DeployConfig with this tenant's constraints merged in.
        Tenant budgets override base config if more restrictive.
        """
        daily = base.daily_budget_usd
        if self.budget_daily_usd is not None:
            daily = (
                min(daily, self.budget_daily_usd)
                if daily is not None
                else self.budget_daily_usd
            )
        hourly = base.hourly_budget_usd
        if self.budget_hourly_usd is not None:
            hourly = (
                min(hourly, self.budget_hourly_usd)
                if hourly is not None
                else self.budget_hourly_usd
            )
        return base.model_copy(update={
            "audit_path": self.scoped_audit_path(),
            "daily_budget_usd": daily,
            "hourly_budget_usd": hourly,
        })


class TenantRegistry:
    """
    Registry of all active tenants.
    Thread-safe for single-process use.
    """

    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._rbac: dict[str, RBACPolicy] = {}

    def register(self, tenant: Tenant, rbac: RBACPolicy | None = None) -> None:
        self._tenants[tenant.id] = tenant
        self._rbac[tenant.id] = rbac or RBACPolicy.default()
        log.info("tenant_registered", tenant_id=tenant.id, name=tenant.name)

    def get(self, tenant_id: str) -> Tenant:
        if tenant_id not in self._tenants:
            raise TenantNotFoundError(tenant_id)
        return self._tenants[tenant_id]

    def rbac(self, tenant_id: str) -> RBACPolicy:
        if tenant_id not in self._rbac:
            raise TenantNotFoundError(tenant_id)
        return self._rbac[tenant_id]

    def list_tenants(self) -> list[Tenant]:
        return list(self._tenants.values())

    def deregister(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)
        self._rbac.pop(tenant_id, None)
        log.info("tenant_deregistered", tenant_id=tenant_id)

    @property
    def count(self) -> int:
        return len(self._tenants)
