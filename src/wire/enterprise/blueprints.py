"""
Agent Identity Blueprints — Foundry enterprise governance for WIRE.

A blueprint governs a *type* of agent (e.g. "Contoso Sales Agent"), enabling
admins to apply Conditional Access policies or revoke permissions for all
instances of that type at once.

This is a WIRE-side governance primitive — no Azure SDK calls are made here.
Blueprint enforcement happens during workforce deployment before any Foundry
API interaction begins.

Usage:
    from wire.enterprise.blueprints import AgentBlueprint, get_registry

    bp = AgentBlueprint(
        id="cost-monitor-v1",
        name="AWS Cost Monitor Agent",
        description="Monitors AWS spend and alerts on anomalies.",
        entra_app_id="a1b2c3d4-...",
        allowed_roles=["wire-engineers", "finops-team"],
        required_permissions=["Cost.Read"],
        compliance_preset="soc2",
        max_concurrent_instances=5,
    )

    registry = get_registry()
    registry.register(bp)

    # Enforce before deploying
    registry.check_deployment_allowed("cost-monitor-v1", actor, policy)
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import WIREError
from wire.enterprise.rbac import Actor, PermissionDeniedError, RBACPolicy

log = structlog.get_logger(__name__)


class BlueprintNotFoundError(WIREError):
    """Raised when a blueprint ID is not found in the registry."""

    def __init__(self, blueprint_id: str) -> None:
        self.blueprint_id = blueprint_id
        super().__init__(
            f"Blueprint '{blueprint_id}' not registered. "
            "Call registry.register() first."
        )


class AgentBlueprint(BaseModel):
    """
    Foundry agent identity blueprint — governs a type of agent across all instances.

    A blueprint is an org-level governance object. Admins register blueprints once;
    every workforce of that type references the blueprint ID. Conditional Access
    policies and permission revocations applied at the blueprint level propagate
    to all running instances automatically.
    """

    id: str                                          # blueprint ID (e.g. "cost-monitor-v1")
    name: str                                        # human-readable (e.g. "AWS Cost Monitor Agent")
    description: str
    entra_app_id: str | None = None                  # Azure AD app registration ID
    allowed_roles: list[str] = Field(default_factory=list)   # WIRE RBAC groups allowed to deploy
    required_permissions: list[str] = Field(default_factory=list)  # Azure RBAC permissions required
    compliance_preset: str | None = None             # "soc2", "hipaa", "gdpr", "nist_ai"
    max_concurrent_instances: int = 10
    metadata: dict[str, Any] = Field(default_factory=dict)


class BlueprintRegistry:
    """
    Registry of all agent blueprints for this organisation.

    Thread-safe for single-process use. Use get_registry() for the module-level
    singleton, or instantiate directly for isolated test scenarios.
    """

    def __init__(self) -> None:
        self._blueprints: dict[str, AgentBlueprint] = {}

    def register(self, blueprint: AgentBlueprint) -> None:
        """Register a blueprint. Overwrites any existing entry with the same ID."""
        self._blueprints[blueprint.id] = blueprint
        log.info(
            "blueprint_registered",
            blueprint_id=blueprint.id,
            name=blueprint.name,
            compliance_preset=blueprint.compliance_preset,
        )

    def get(self, blueprint_id: str) -> AgentBlueprint:
        """Return the blueprint for the given ID. Raises BlueprintNotFoundError if absent."""
        if blueprint_id not in self._blueprints:
            raise BlueprintNotFoundError(blueprint_id)
        return self._blueprints[blueprint_id]

    def list_blueprints(self) -> list[AgentBlueprint]:
        """Return all registered blueprints."""
        return list(self._blueprints.values())

    def deregister(self, blueprint_id: str) -> None:
        """Remove a blueprint from the registry. No-op if not present."""
        removed = self._blueprints.pop(blueprint_id, None)
        if removed:
            log.info("blueprint_deregistered", blueprint_id=blueprint_id)

    def check_deployment_allowed(
        self,
        blueprint_id: str,
        actor: Actor,
        policy: RBACPolicy,
    ) -> None:
        """
        Assert that actor is permitted to deploy a workforce governed by this blueprint.

        Checks two things:
          1. Blueprint exists (raises BlueprintNotFoundError if not).
          2. Actor belongs to at least one of blueprint.allowed_roles
             — or is a WIRE admin — (raises PermissionDeniedError if not).

        Args:
            blueprint_id: ID of the blueprint to check.
            actor:        The authenticated actor requesting deployment.
            policy:       The RBAC policy for additional role lookups (unused currently
                          but present for future SSO/JWT claims integration).
        """
        blueprint = self.get(blueprint_id)

        # Admins bypass blueprint role restriction
        if actor.is_admin():
            log.debug(
                "blueprint_deploy_allowed_admin",
                blueprint_id=blueprint_id,
                actor_id=actor.id,
            )
            return

        # Check intersection between actor groups and blueprint allowed_roles
        actor_groups = set(actor.groups)
        allowed = set(blueprint.allowed_roles)

        if actor_groups & allowed:
            log.debug(
                "blueprint_deploy_allowed",
                blueprint_id=blueprint_id,
                actor_id=actor.id,
                matched_roles=list(actor_groups & allowed),
            )
            return

        log.warning(
            "blueprint_deploy_denied",
            blueprint_id=blueprint_id,
            actor_id=actor.id,
            actor_groups=actor.groups,
            allowed_roles=blueprint.allowed_roles,
        )
        # Re-use PermissionDeniedError with a synthetic "deploy_blueprint" permission
        # so callers can catch the existing enterprise error type.
        from wire.enterprise.rbac import Permission  # local import avoids circularity risk
        err = PermissionDeniedError(actor.id, Permission.DEPLOY)
        # Emit structured log entry that EventStore and MetricsCollector can pick up
        try:
            import asyncio
            from wire.core.events import EventBus, EventKind, WIREEvent
            loop = asyncio.get_running_loop()
            bus = EventBus()
            loop.create_task(bus.emit(WIREEvent(
                kind=EventKind.WORKFORCE_START,  # closest available kind for policy events
                run_id=f"blueprint-denied-{blueprint_id}",
                data={
                    "event_type": "blueprint_deploy_denied",
                    "blueprint_id": blueprint_id,
                    "actor_id": actor.id,
                    "allowed_roles": blueprint.allowed_roles,
                },
            )))
        except RuntimeError:
            pass  # No running loop — skip event emission
        raise err

    @property
    def count(self) -> int:
        """Number of registered blueprints."""
        return len(self._blueprints)


# ── Module-level singleton ────────────────────────────────────────────────────

_DEFAULT_REGISTRY = BlueprintRegistry()


def get_registry() -> BlueprintRegistry:
    """Return the default module-level BlueprintRegistry singleton."""
    return _DEFAULT_REGISTRY
