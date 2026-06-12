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

__version__ = "0.2.0"
__all__ = [
    # Entry point
    "deploy",
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
]
