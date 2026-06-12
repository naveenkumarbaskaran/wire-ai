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
from wire.core.idempotency_backends import (
    MemoryBackend, SQLiteBackend, RedisBackend, PostgresBackend, IdempotencyStore,
)
from wire.core.models import Risk, DeployConfig
from wire.core.sla import SLATracker, SLABreachError
from wire.deploy import deploy
from wire.hire_api import hire, hire_async
from wire.hire.templates import RoleTemplate, RoleCategory, ROLE_TEMPLATES
from wire.visibility.dashboard import WorkforceDashboard, AgentStatus
from wire.visibility.drift import DriftDetector, DriftAlert
from wire.visibility.ledger import CostLedger
from wire.visibility.replay import TimeTravel

try:
    from wire.visibility.web_dashboard import WebDashboard
except ImportError:
    WebDashboard = None  # type: ignore[assignment,misc]

from wire.core.stream import StreamGuard, GuardedStream, StreamStallError, StreamCapExceededError, StreamStats
from wire.core.durable_events import DurableEventBus, DeadLetter
from wire.core.policy import PolicyEnforcer, PolicyViolationError, ToolCallContext
from wire.integrations.langchain import wrap_chain, GovernedChain
from wire.integrations.llama_index import wrap_query_engine, GovernedQueryEngine
from wire.integrations.tool_registry import tool, tools, WIRETool, ToolRegistry
from wire.observability.event_store import EventStore, EventQuery, RunSummary
from wire.observability.metrics import MetricsCollector, wire_metrics
from wire.middleware.autopatch import patch, unpatch, is_patched, patch_status
from wire.enterprise.compliance import CompliancePreset, ComplianceConfig
from wire.enterprise.rbac import RBACPolicy, Permission, Actor, PermissionDeniedError
from wire.enterprise.multitenancy import Tenant, TenantRegistry
from wire.enterprise.backends import S3AuditBackend, PostgresAuditBackend

try:
    from wire.enterprise.blueprints import AgentBlueprint, BlueprintRegistry
except ImportError:
    AgentBlueprint = None  # type: ignore[assignment,misc]
    BlueprintRegistry = None  # type: ignore[assignment,misc]

from wire.channels import SlackHITLChannel
from wire.plugins import WIREPlugin, PluginRegistry, get_plugin_registry
from wire.plugins.agentlens_plugin import AgentLensPlugin
from wire.plugins.tokmon_plugin import TokmonPlugin

__version__ = "1.7.3"
__all__ = [
    # Entry points
    "deploy", "hire", "hire_async",
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
    "MemoryBackend", "SQLiteBackend", "RedisBackend", "PostgresBackend", "IdempotencyStore",
    "SLATracker", "SLABreachError",
    # Sprint 3
    "RoleTemplate", "RoleCategory", "ROLE_TEMPLATES",
    # Sprint 4
    "WorkforceDashboard", "AgentStatus",
    "DriftDetector", "DriftAlert",
    "CostLedger",
    "WebDashboard",
    "TimeTravel",
    "PolicyEnforcer", "PolicyViolationError", "ToolCallContext",
    # Sprint 7 — Streaming governance
    "StreamGuard", "GuardedStream", "StreamStallError", "StreamCapExceededError", "StreamStats",
    "DurableEventBus", "DeadLetter",
    # Sprint 8 — Integrations
    "wrap_chain", "GovernedChain",
    "wrap_query_engine", "GovernedQueryEngine",
    "tool", "tools", "WIRETool", "ToolRegistry",
    # Sprint 9 — Observability
    "EventStore", "EventQuery", "RunSummary",
    "MetricsCollector", "wire_metrics",
    # Sprint 10 — Auto-patch
    "patch", "unpatch", "is_patched", "patch_status",
    # Sprint 6 — Enterprise
    "CompliancePreset", "ComplianceConfig",
    "RBACPolicy", "Permission", "Actor", "PermissionDeniedError",
    "Tenant", "TenantRegistry",
    "S3AuditBackend", "PostgresAuditBackend",
    # Blueprints
    "AgentBlueprint", "BlueprintRegistry",
    # Channels
    "SlackHITLChannel",
    # Plugins
    "WIREPlugin",
    "PluginRegistry",
    "get_plugin_registry",
    "AgentLensPlugin",
    "TokmonPlugin",
]
