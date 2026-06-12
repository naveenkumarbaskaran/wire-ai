"""
RBAC — Role-Based Access Control for WIRE workforces.

Controls who can:
  - deploy workforces
  - approve HITL requests
  - view audit chains
  - override SLA breaches
  - access specific role templates

Integrates with SSO via JWT claims (Sprint 6).
Standalone mode uses a local policy file.

Usage:
    from wire.enterprise.rbac import RBACPolicy, Permission, Actor

    policy = RBACPolicy()
    policy.grant(group="ops-engineers",  permissions=[Permission.DEPLOY, Permission.VIEW_AUDIT])
    policy.grant(group="ops-managers",   permissions=[Permission.APPROVE_HITL, Permission.OVERRIDE_SLA])
    policy.grant(group="security-team",  permissions=[Permission.VIEW_AUDIT, Permission.EXPORT_AUDIT])

    actor = Actor(id="naveen@company.com", groups=["ops-engineers"])
    policy.require(actor, Permission.DEPLOY)      # passes
    policy.require(actor, Permission.APPROVE_HITL)  # raises PermissionDeniedError
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog
from pydantic import BaseModel, Field

from wire.core.errors import WIREError

log = structlog.get_logger(__name__)


class Permission(str, Enum):
    DEPLOY          = "deploy"           # create and start workforces
    VIEW_WORKFORCE  = "view_workforce"   # read workforce status + dashboard
    VIEW_AUDIT      = "view_audit"       # read audit chain entries
    EXPORT_AUDIT    = "export_audit"     # export audit chain to file/S3
    APPROVE_HITL    = "approve_hitl"     # respond to HITL approval requests
    REJECT_HITL     = "reject_hitl"      # reject HITL requests
    OVERRIDE_SLA    = "override_sla"     # acknowledge and override SLA breaches
    MANAGE_BUDGETS  = "manage_budgets"   # set/change budget ceilings
    ADMIN           = "admin"            # all permissions


class PermissionDeniedError(WIREError):
    """Raised when an actor attempts an action they are not authorised for."""
    def __init__(self, actor_id: str, permission: Permission) -> None:
        self.actor_id = actor_id
        self.permission = permission
        super().__init__(
            f"Permission denied: '{actor_id}' does not have '{permission}' access. "
            "Contact your WIRE administrator to request access."
        )


class Actor(BaseModel):
    """Represents an authenticated user or service account."""
    id: str                              # email, I-number, service account name
    groups: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    def is_admin(self) -> bool:
        return "admin" in self.groups or "wire-admins" in self.groups


class GroupPolicy(BaseModel):
    group: str
    permissions: list[Permission]


class RBACPolicy:
    """
    Declarative RBAC policy for WIRE governance actions.

    Groups map to permissions. An actor belongs to groups.
    Admin permission supersedes all others.
    """

    def __init__(self) -> None:
        self._policies: list[GroupPolicy] = []
        self._actor_overrides: dict[str, list[Permission]] = {}

    def grant(self, *, group: str, permissions: list[Permission]) -> None:
        """Grant a list of permissions to a group."""
        self._policies.append(GroupPolicy(group=group, permissions=permissions))
        log.debug("rbac_grant", group=group, permissions=[p.value for p in permissions])

    def grant_actor(self, *, actor_id: str, permissions: list[Permission]) -> None:
        """Grant permissions directly to a specific actor (override for service accounts)."""
        self._actor_overrides[actor_id] = permissions

    def can(self, actor: Actor, permission: Permission) -> bool:
        """Return True if actor has the given permission."""
        if actor.is_admin():
            return True
        # Actor-level override
        if permission in self._actor_overrides.get(actor.id, []):
            return True
        if Permission.ADMIN in self._actor_overrides.get(actor.id, []):
            return True
        # Group-based
        for policy in self._policies:
            if policy.group in actor.groups:
                if Permission.ADMIN in policy.permissions:
                    return True
                if permission in policy.permissions:
                    return True
        return False

    def require(self, actor: Actor, permission: Permission) -> None:
        """Assert actor has permission — raises PermissionDeniedError if not."""
        if not self.can(actor, permission):
            log.warning(
                "rbac_denied",
                actor_id=actor.id,
                permission=permission,
                groups=actor.groups,
            )
            raise PermissionDeniedError(actor.id, permission)
        log.debug("rbac_allowed", actor_id=actor.id, permission=permission)

    def permissions_for(self, actor: Actor) -> list[Permission]:
        """Return all permissions an actor has."""
        if actor.is_admin():
            return list(Permission)
        perms: set[Permission] = set()
        for p in self._actor_overrides.get(actor.id, []):
            if p == Permission.ADMIN:
                return list(Permission)
            perms.add(p)
        for policy in self._policies:
            if policy.group in actor.groups:
                if Permission.ADMIN in policy.permissions:
                    return list(Permission)
                perms.update(policy.permissions)
        return sorted(perms, key=lambda p: p.value)

    @classmethod
    def default(cls) -> "RBACPolicy":
        """Sensible default policy — engineers deploy, managers approve."""
        policy = cls()
        policy.grant(
            group="wire-engineers",
            permissions=[Permission.DEPLOY, Permission.VIEW_WORKFORCE,
                         Permission.VIEW_AUDIT, Permission.MANAGE_BUDGETS],
        )
        policy.grant(
            group="wire-managers",
            permissions=[Permission.APPROVE_HITL, Permission.REJECT_HITL,
                         Permission.OVERRIDE_SLA, Permission.VIEW_WORKFORCE,
                         Permission.VIEW_AUDIT, Permission.EXPORT_AUDIT],
        )
        policy.grant(
            group="wire-security",
            permissions=[Permission.VIEW_AUDIT, Permission.EXPORT_AUDIT],
        )
        policy.grant(
            group="wire-admins",
            permissions=[Permission.ADMIN],
        )
        return policy
