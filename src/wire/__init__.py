"""
WIRE — Workforce Intelligence & Reasoning Engine
Framework-agnostic governance layer for autonomous enterprise AI agents.
"""

from wire.core.audit import AuditChain, AuditEntry
from wire.core.budget import Budget, BudgetBreachError
from wire.core.errors import WIREError, LoopBreachError
from wire.core.events import WIREEvent, EventBus, EventKind
from wire.core.guard import LoopGuard
from wire.core.hitl import (
    HITLGate, HITLChannel, HITLAction, HITLDecision,
    HITLTimeoutError, HITLRejectedError, TimeoutAction,
)
from wire.core.idempotency import IdempotencyGuard
from wire.core.models import Risk, DeployConfig
from wire.core.sla import SLATracker, SLABreachError
from wire.deploy import deploy
from wire.hire_api import hire, hire_async
from wire.hire.templates import RoleTemplate, RoleCategory, ROLE_TEMPLATES

__version__ = "0.3.0"
__all__ = [
    # Entry points
    "deploy",
    "hire",
    "hire_async",
    # Sprint 1
    "AuditChain", "AuditEntry",
    "Budget", "BudgetBreachError",
    "LoopGuard", "LoopBreachError",
    "WIREError", "WIREEvent", "EventBus", "EventKind",
    "Risk", "DeployConfig",
    # Sprint 2
    "HITLGate", "HITLChannel", "HITLAction", "HITLDecision",
    "HITLTimeoutError", "HITLRejectedError", "TimeoutAction",
    "IdempotencyGuard",
    "SLATracker", "SLABreachError",
    # Sprint 3
    "RoleTemplate", "RoleCategory", "ROLE_TEMPLATES",
]
