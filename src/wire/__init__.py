"""
WIRE — Workforce Intelligence & Reasoning Engine
Framework-agnostic governance layer for autonomous enterprise AI agents.
"""

from wire.core.audit import AuditChain, AuditEntry
from wire.core.budget import Budget, BudgetBreachError
from wire.core.errors import WIREError, LoopBreachError
from wire.core.events import WIREEvent, EventBus
from wire.core.guard import LoopGuard
from wire.core.models import Risk, DeployConfig
from wire.deploy import deploy

__version__ = "0.1.0"
__all__ = [
    "deploy",
    "AuditChain",
    "AuditEntry",
    "Budget",
    "BudgetBreachError",
    "LoopGuard",
    "LoopBreachError",
    "WIREError",
    "WIREEvent",
    "EventBus",
    "Risk",
    "DeployConfig",
]
