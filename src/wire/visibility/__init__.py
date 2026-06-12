"""Visibility package — dashboard, ledger, drift detection, replay."""

from wire.visibility.dashboard import WorkforceDashboard, AgentStatus, RoleState
from wire.visibility.drift import DriftDetector, DriftAlert
from wire.visibility.ledger import CostLedger
from wire.visibility.replay import TimeTravel, ReplayStep

try:
    from wire.visibility.web_dashboard import WebDashboard
    _web_available = True
except ImportError:
    WebDashboard = None  # type: ignore[assignment,misc]
    _web_available = False

__all__ = [
    "WorkforceDashboard", "AgentStatus", "RoleState",
    "DriftDetector", "DriftAlert",
    "CostLedger",
    "TimeTravel", "ReplayStep",
    "WebDashboard",
]

