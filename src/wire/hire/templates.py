"""
Role template definitions — the 20 built-in roles shipped with WIRE.

Each template defines:
  - name, description, category
  - trigger phrases the HIRE parser matches against
  - default SLA values
  - authority scope (what the role can read/write/escalate)
  - compatible handoff targets

Rule-based matching checks trigger phrases first.
LLM fallback activates when confidence < 0.70.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from wire.core.models import Risk


class RoleCategory(str, Enum):
    MONITORING   = "monitoring"
    ANALYSIS     = "analysis"
    EXECUTION    = "execution"
    GOVERNANCE   = "governance"
    COORDINATION = "coordination"


class AuthorityScope(BaseModel):
    can_read:    list[str] = Field(default_factory=list)
    can_write:   list[str] = Field(default_factory=list)
    can_escalate: bool = True
    can_approve:  bool = False
    max_spend_usd: float | None = None


class SLADefaults(BaseModel):
    response_seconds: float | None = None
    max_cost_usd: float | None = None
    min_confidence: float | None = None
    max_retries: int = 3


class RoleTemplate(BaseModel):
    name: str
    category: RoleCategory
    description: str
    trigger_phrases: list[str]          # keyword patterns for rule-based matching
    default_sla: SLADefaults = Field(default_factory=SLADefaults)
    authority: AuthorityScope = Field(default_factory=AuthorityScope)
    default_handoffs: list[str] = Field(default_factory=list)  # role names
    risk_level: Risk = Risk.LOW
    idempotent: bool = False


# ── 20 Built-in Role Templates ────────────────────────────────────────────────

ROLE_TEMPLATES: list[RoleTemplate] = [

    # ── MONITORING (4) ────────────────────────────────────────────────────────

    RoleTemplate(
        name="cost_monitor",
        category=RoleCategory.MONITORING,
        description="Monitor cloud/API spend and flag anomalies against a threshold",
        trigger_phrases=[
            "monitor cost", "watch spend", "track cost", "cost alert",
            "budget monitor", "spend monitor", "flag cost", "cost threshold",
            "aws cost", "cloud spend", "api spend", "billing monitor",
            "monitor spend", "monitor aws", "monitor budget",
            "aws spend", "cost exceed", "spend exceed", "flag spend",
        ],
        default_sla=SLADefaults(response_seconds=120, max_cost_usd=0.05),
        authority=AuthorityScope(
            can_read=["billing", "cost_explorer", "metrics"],
            can_write=[],
            can_escalate=True,
        ),
        default_handoffs=["anomaly_detector", "human_escalator"],
        risk_level=Risk.LOW,
    ),

    RoleTemplate(
        name="uptime_monitor",
        category=RoleCategory.MONITORING,
        description="Monitor service health, endpoints, and uptime SLAs",
        trigger_phrases=[
            "monitor uptime", "health check", "watch endpoint", "service monitor",
            "ping check", "availability monitor", "latency monitor", "uptime check",
            "is service up", "monitor health", "check status",
        ],
        default_sla=SLADefaults(response_seconds=30, max_cost_usd=0.02),
        authority=AuthorityScope(
            can_read=["endpoints", "metrics", "logs"],
            can_write=[],
            can_escalate=True,
        ),
        default_handoffs=["incident_analyser", "notification_sender"],
        risk_level=Risk.LOW,
    ),

    RoleTemplate(
        name="anomaly_detector",
        category=RoleCategory.MONITORING,
        description="Detect statistical anomalies in metrics, logs, or data streams",
        trigger_phrases=[
            "detect anomaly", "anomaly detection", "flag anomaly", "spike detection",
            "outlier detection", "unusual pattern", "detect spike", "flag unusual",
            "statistical anomaly", "data anomaly", "metric anomaly",
            "detect anomalies", "flag anomalies", "anomalies",
        ],
        default_sla=SLADefaults(response_seconds=60, max_cost_usd=0.10, min_confidence=0.75),
        authority=AuthorityScope(
            can_read=["metrics", "logs", "timeseries"],
            can_write=[],
            can_escalate=True,
        ),
        default_handoffs=["incident_analyser", "human_escalator"],
        risk_level=Risk.MEDIUM,
    ),

    RoleTemplate(
        name="sla_watcher",
        category=RoleCategory.MONITORING,
        description="Watch SLA compliance across services and alert on breaches",
        trigger_phrases=[
            "watch sla", "sla monitor", "sla compliance", "track sla",
            "response time monitor", "latency sla", "sla breach", "sla alert",
        ],
        default_sla=SLADefaults(response_seconds=60, max_cost_usd=0.03),
        authority=AuthorityScope(
            can_read=["metrics", "sla_definitions", "traces"],
            can_write=[],
            can_escalate=True,
        ),
        default_handoffs=["report_generator", "human_escalator"],
        risk_level=Risk.MEDIUM,
    ),

    # ── ANALYSIS (4) ─────────────────────────────────────────────────────────

    RoleTemplate(
        name="data_analyst",
        category=RoleCategory.ANALYSIS,
        description="Analyse datasets, generate insights, and summarise findings",
        trigger_phrases=[
            "analyse data", "analyze data", "data analysis", "run analysis",
            "generate insights", "data summary", "analyse metrics", "data insights",
            "crunch numbers", "analyse report", "statistical analysis",
        ],
        default_sla=SLADefaults(response_seconds=300, max_cost_usd=0.50, min_confidence=0.70),
        authority=AuthorityScope(
            can_read=["databases", "files", "apis"],
            can_write=["reports"],
            can_escalate=True,
        ),
        default_handoffs=["report_generator"],
        risk_level=Risk.LOW,
        idempotent=True,
    ),

    RoleTemplate(
        name="log_analyser",
        category=RoleCategory.ANALYSIS,
        description="Parse and analyse application/system logs for patterns and errors",
        trigger_phrases=[
            "analyse logs", "analyze logs", "log analysis", "parse logs",
            "search logs", "find errors in logs", "log patterns", "error analysis",
            "trace analysis", "log investigation",
        ],
        default_sla=SLADefaults(response_seconds=120, max_cost_usd=0.20),
        authority=AuthorityScope(
            can_read=["logs", "traces", "cloudwatch", "splunk"],
            can_write=[],
            can_escalate=True,
        ),
        default_handoffs=["incident_analyser", "report_generator"],
        risk_level=Risk.LOW,
    ),

    RoleTemplate(
        name="incident_analyser",
        category=RoleCategory.ANALYSIS,
        description="Perform root cause analysis on incidents and propose remediation",
        trigger_phrases=[
            "analyse incident", "analyze incident", "root cause", "rca",
            "incident analysis", "postmortem", "what caused", "investigate incident",
            "diagnose issue", "find root cause",
        ],
        default_sla=SLADefaults(response_seconds=600, max_cost_usd=1.00, min_confidence=0.75),
        authority=AuthorityScope(
            can_read=["logs", "metrics", "traces", "incidents"],
            can_write=["incident_reports"],
            can_escalate=True,
        ),
        default_handoffs=["report_generator", "ticket_creator", "human_escalator"],
        risk_level=Risk.HIGH,
        idempotent=True,
    ),

    RoleTemplate(
        name="report_generator",
        category=RoleCategory.ANALYSIS,
        description="Generate structured reports from data, analysis, or summaries",
        trigger_phrases=[
            "generate report", "create report", "write report", "make report",
            "summarise findings", "summarize findings", "report on", "weekly report",
            "daily report", "executive summary", "status report",
        ],
        default_sla=SLADefaults(response_seconds=300, max_cost_usd=0.30),
        authority=AuthorityScope(
            can_read=["databases", "files", "apis", "reports"],
            can_write=["reports", "email"],
            can_escalate=False,
        ),
        default_handoffs=["notification_sender"],
        risk_level=Risk.LOW,
        idempotent=True,
    ),

    # ── EXECUTION (4) ─────────────────────────────────────────────────────────

    RoleTemplate(
        name="ticket_creator",
        category=RoleCategory.EXECUTION,
        description="Create tickets in Jira, ServiceNow, Linear, or GitHub Issues",
        trigger_phrases=[
            "create ticket", "open ticket", "create jira", "open jira",
            "raise issue", "create issue", "file ticket", "log ticket",
            "create p1", "create p2", "open bug", "create servicenow",
            "jira ticket", "jira p1", "jira p2", "open a jira",
            "create a ticket", "create a jira", "open a ticket",
        ],
        default_sla=SLADefaults(response_seconds=30, max_cost_usd=0.05),
        authority=AuthorityScope(
            can_read=["jira", "servicenow", "github"],
            can_write=["jira", "servicenow", "github"],
            can_escalate=True,
            max_spend_usd=0.0,
        ),
        default_handoffs=["notification_sender"],
        risk_level=Risk.MEDIUM,
        idempotent=True,  # critical — never create duplicate tickets
    ),

    RoleTemplate(
        name="notification_sender",
        category=RoleCategory.EXECUTION,
        description="Send notifications via Slack, email, Teams, or PagerDuty",
        trigger_phrases=[
            "send notification", "notify team", "send alert", "post to slack",
            "send email", "send message", "alert on call", "page oncall",
            "send to channel", "notify channel", "send update",
        ],
        default_sla=SLADefaults(response_seconds=15, max_cost_usd=0.01),
        authority=AuthorityScope(
            can_read=[],
            can_write=["slack", "email", "teams", "pagerduty"],
            can_escalate=False,
        ),
        default_handoffs=[],
        risk_level=Risk.LOW,
        idempotent=True,
    ),

    RoleTemplate(
        name="workflow_trigger",
        category=RoleCategory.EXECUTION,
        description="Trigger external workflows, webhooks, CI/CD pipelines, or automations",
        trigger_phrases=[
            "trigger workflow", "run pipeline", "trigger ci", "trigger deploy",
            "call webhook", "trigger automation", "run job", "kick off pipeline",
            "start workflow", "execute pipeline",
        ],
        default_sla=SLADefaults(response_seconds=60, max_cost_usd=0.05),
        authority=AuthorityScope(
            can_read=["ci_cd", "github_actions", "webhooks"],
            can_write=["ci_cd", "github_actions", "webhooks"],
            can_escalate=True,
            max_spend_usd=0.0,
        ),
        default_handoffs=["notification_sender"],
        risk_level=Risk.HIGH,
        idempotent=True,
    ),

    RoleTemplate(
        name="data_writer",
        category=RoleCategory.EXECUTION,
        description="Write, update, or delete data in databases, files, or APIs",
        trigger_phrases=[
            "write data", "update database", "insert record", "update record",
            "save to db", "write to file", "update api", "patch record",
            "store data", "write results",
        ],
        default_sla=SLADefaults(response_seconds=30, max_cost_usd=0.05),
        authority=AuthorityScope(
            can_read=["databases", "files", "apis"],
            can_write=["databases", "files", "apis"],
            can_escalate=True,
            max_spend_usd=0.0,
        ),
        default_handoffs=[],
        risk_level=Risk.HIGH,
        idempotent=True,
    ),

    # ── GOVERNANCE (4) ────────────────────────────────────────────────────────

    RoleTemplate(
        name="human_escalator",
        category=RoleCategory.GOVERNANCE,
        description="Escalate decisions to a human via Slack, email, or CLI approval",
        trigger_phrases=[
            "escalate to human", "human approval", "require approval", "ask human",
            "get approval", "human review", "escalate", "wait for approval",
            "notify human", "human in the loop", "hitl",
        ],
        default_sla=SLADefaults(response_seconds=1800),  # 30 min
        authority=AuthorityScope(
            can_read=[],
            can_write=["slack", "email"],
            can_escalate=True,
            can_approve=True,
        ),
        default_handoffs=[],
        risk_level=Risk.HIGH,
    ),

    RoleTemplate(
        name="approval_router",
        category=RoleCategory.GOVERNANCE,
        description="Route decisions to the right approver based on risk level and context",
        trigger_phrases=[
            "route approval", "approval routing", "find approver", "route to approver",
            "conditional approval", "risk-based approval", "escalation routing",
        ],
        default_sla=SLADefaults(response_seconds=300),
        authority=AuthorityScope(
            can_read=["rbac", "org_chart", "approval_policies"],
            can_write=["approval_queue"],
            can_escalate=True,
            can_approve=True,
        ),
        default_handoffs=["human_escalator", "notification_sender"],
        risk_level=Risk.HIGH,
    ),

    RoleTemplate(
        name="compliance_checker",
        category=RoleCategory.GOVERNANCE,
        description="Validate actions against compliance policies before execution",
        trigger_phrases=[
            "compliance check", "policy check", "validate compliance", "check policy",
            "regulatory check", "gdpr check", "hipaa check", "soc2 check",
            "before executing", "pre-execution check", "policy validation",
        ],
        default_sla=SLADefaults(response_seconds=30, min_confidence=0.90),
        authority=AuthorityScope(
            can_read=["policies", "audit_logs", "rbac"],
            can_write=["compliance_reports"],
            can_escalate=True,
        ),
        default_handoffs=["audit_reporter", "human_escalator"],
        risk_level=Risk.CRITICAL,
        idempotent=True,
    ),

    RoleTemplate(
        name="audit_reporter",
        category=RoleCategory.GOVERNANCE,
        description="Generate compliance and audit reports from the AuditChain",
        trigger_phrases=[
            "audit report", "generate audit", "compliance report", "audit log report",
            "audit summary", "generate compliance report", "audit trail report",
        ],
        default_sla=SLADefaults(response_seconds=120, max_cost_usd=0.20),
        authority=AuthorityScope(
            can_read=["audit_chain", "compliance_logs"],
            can_write=["audit_reports"],
            can_escalate=False,
        ),
        default_handoffs=["notification_sender"],
        risk_level=Risk.LOW,
        idempotent=True,
    ),

    # ── COORDINATION (4) ──────────────────────────────────────────────────────

    RoleTemplate(
        name="task_scheduler",
        category=RoleCategory.COORDINATION,
        description="Schedule and dispatch tasks to agents on a time-based or event-based trigger",
        trigger_phrases=[
            "schedule task", "run every", "run hourly", "run daily", "cron job",
            "schedule job", "run at", "periodic task", "run on schedule",
            "every hour", "every day", "every week",
        ],
        default_sla=SLADefaults(response_seconds=10, max_cost_usd=0.02),
        authority=AuthorityScope(
            can_read=["schedule", "task_queue"],
            can_write=["task_queue"],
            can_escalate=True,
        ),
        default_handoffs=["workforce_supervisor"],
        risk_level=Risk.LOW,
    ),

    RoleTemplate(
        name="workforce_supervisor",
        category=RoleCategory.COORDINATION,
        description="Supervise a set of roles — track progress, handle failures, re-dispatch",
        trigger_phrases=[
            "supervise workforce", "manage agents", "coordinate roles", "orchestrate",
            "manage workflow", "oversee agents", "coordinate agents", "workforce manager",
        ],
        default_sla=SLADefaults(response_seconds=600),
        authority=AuthorityScope(
            can_read=["all"],
            can_write=["task_queue", "status"],
            can_escalate=True,
        ),
        default_handoffs=["human_escalator"],
        risk_level=Risk.MEDIUM,
    ),

    RoleTemplate(
        name="handoff_coordinator",
        category=RoleCategory.COORDINATION,
        description="Manage structured handoffs between roles — pass context, transform outputs",
        trigger_phrases=[
            "handoff", "pass to", "then send to", "after that", "hand off",
            "then notify", "then create", "followed by", "then escalate",
        ],
        default_sla=SLADefaults(response_seconds=10, max_cost_usd=0.01),
        authority=AuthorityScope(
            can_read=["all"],
            can_write=["task_queue"],
            can_escalate=True,
        ),
        default_handoffs=[],
        risk_level=Risk.LOW,
    ),

    RoleTemplate(
        name="priority_router",
        category=RoleCategory.COORDINATION,
        description="Route tasks to agents based on priority, load, and capability matching",
        trigger_phrases=[
            "route to", "prioritise", "prioritize", "route based on", "smart routing",
            "load balance", "route by priority", "conditional routing", "route if",
        ],
        default_sla=SLADefaults(response_seconds=5, max_cost_usd=0.01),
        authority=AuthorityScope(
            can_read=["agent_registry", "task_queue", "load_metrics"],
            can_write=["task_queue"],
            can_escalate=True,
        ),
        default_handoffs=[],
        risk_level=Risk.LOW,
    ),
]

# ── Index for fast lookup ─────────────────────────────────────────────────────

TEMPLATE_BY_NAME: dict[str, RoleTemplate] = {t.name: t for t in ROLE_TEMPLATES}

TEMPLATES_BY_CATEGORY: dict[RoleCategory, list[RoleTemplate]] = {
    cat: [t for t in ROLE_TEMPLATES if t.category == cat]
    for cat in RoleCategory
}
