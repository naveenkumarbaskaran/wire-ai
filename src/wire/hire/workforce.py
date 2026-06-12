"""
WorkforceGraph — the assembled workforce from a HIRE parse result.

Represents the directed graph of roles WIRE will run:
  cost_monitor → anomaly_detector → ticket_creator → human_escalator

Provides:
  - describe()    — plain-English summary for non-engineers
  - to_yaml()     — YAML export for auditing / sharing
  - deploy()      — hand off to wire.deploy() with the right config
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from wire.hire.parser import MatchResult, ParseResult
from wire.hire.templates import RoleTemplate
from wire.core.models import Risk


class WorkforceNode(BaseModel):
    role: str
    description: str
    category: str
    confidence: float
    source: str
    idempotent: bool
    risk_level: Risk
    sla_response_s: float | None
    sla_max_cost: float | None
    handoffs: list[str]


class WorkforceGraph:
    """
    The assembled workforce — a directed graph of roles with handoff edges.

    Created by wire.hire() after the HIRE parser runs.
    Passed to wire.deploy() to start execution.
    """

    def __init__(self, intent: str, parse_result: ParseResult) -> None:
        self.intent = intent
        self.parse_result = parse_result
        self.nodes: list[WorkforceNode] = [
            WorkforceNode(
                role=m.template.name,
                description=m.template.description,
                category=m.template.category.value,
                confidence=m.confidence,
                source=m.source,
                idempotent=m.template.idempotent,
                risk_level=m.template.risk_level,
                sla_response_s=m.template.default_sla.response_seconds,
                sla_max_cost=m.template.default_sla.max_cost_usd,
                handoffs=m.template.default_handoffs,
            )
            for m in parse_result.matches
        ]

    # ── Public API ────────────────────────────────────────────────────────────

    def describe(self) -> str:
        """
        Plain-English summary of the assembled workforce.
        Readable by non-engineers — suitable for sharing with management.
        """
        if not self.nodes:
            return "No workforce assembled — no role templates matched the intent."

        lines = [
            "WorkforceGraph",
            f"  Intent : {self.intent}",
            f"  Roles  : {len(self.nodes)}",
            f"  Source : {self.parse_result.source} (confidence {self.parse_result.confidence:.0%})",
            "",
        ]

        for i, node in enumerate(self.nodes):
            prefix = "  └─" if i == len(self.nodes) - 1 else "  ├─"
            sla_parts = []
            if node.sla_response_s:
                sla_parts.append(f"max {node.sla_response_s:.0f}s")
            if node.sla_max_cost:
                sla_parts.append(f"max ${node.sla_max_cost:.2f}")
            sla_str = f"  SLA: {', '.join(sla_parts)}" if sla_parts else ""
            idempotent_str = "  [idempotent]" if node.idempotent else ""
            risk_str = f"  risk={node.risk_level}"

            lines.append(
                f"{prefix} {node.role}"
                f"  ({node.category})"
                f"{sla_str}{idempotent_str}{risk_str}"
            )
            lines.append(f"     {node.description}")

            if node.handoffs:
                lines.append(f"     → hands off to: {', '.join(node.handoffs)}")

        if self.parse_result.warnings:
            lines.append("")
            for w in self.parse_result.warnings:
                lines.append(f"  ⚠  {w}")

        return "\n".join(lines)

    def to_yaml(self) -> str:
        """YAML export for auditing, config management, and sharing."""
        import yaml  # optional dep — only used for export
        data = {
            "intent": self.intent,
            "confidence": self.parse_result.confidence,
            "source": self.parse_result.source,
            "roles": [
                {
                    "name": n.role,
                    "category": n.category,
                    "description": n.description,
                    "idempotent": n.idempotent,
                    "risk_level": n.risk_level,
                    "sla": {
                        "response_seconds": n.sla_response_s,
                        "max_cost_usd": n.sla_max_cost,
                    },
                    "handoffs": n.handoffs,
                }
                for n in self.nodes
            ],
        }
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def role_names(self) -> list[str]:
        return [n.role for n in self.nodes]

    def highest_risk(self) -> Risk:
        if not self.nodes:
            return Risk.LOW
        order = [Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]
        return max(self.nodes, key=lambda n: order.index(n.risk_level)).risk_level

    def __repr__(self) -> str:
        roles = " → ".join(self.role_names())
        return f"WorkforceGraph({roles})"
