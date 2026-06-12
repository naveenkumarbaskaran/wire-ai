"""
Compliance presets — SOC-2, HIPAA, GDPR, NIST AI RMF.

Each preset auto-configures:
  - AuditChain retention + encryption requirements
  - HITL requirements for high-risk decisions
  - Data residency constraints
  - Minimum confidence thresholds
  - Mandatory fields in every audit entry

Apply as a single decorator — zero compliance boilerplate in agent code.

Usage:
    import wire
    from wire.enterprise.compliance import CompliancePreset

    workforce = wire.deploy(
        graph,
        backend="langgraph",
        compliance=CompliancePreset.SOC2,
    )
    # All SOC-2 requirements auto-applied:
    # - Tamper-proof audit (already standard in WIRE)
    # - 90-day audit retention
    # - HITL required for CRITICAL risk
    # - All tool calls logged with actor identity
    # - Spending controls documented
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RetentionPolicy(BaseModel):
    days: int
    encrypt_at_rest: bool = True
    encrypt_in_transit: bool = True
    geo_restriction: str | None = None   # e.g. "EU", "US"
    immutable: bool = True


class HITLRequirement(BaseModel):
    require_for_risk: list[str] = Field(default_factory=list)
    require_actor_id: bool = True
    require_notes: bool = False
    timeout_minutes: int = 30


class AuditRequirement(BaseModel):
    log_all_tool_calls: bool = True
    log_actor_identity: bool = True
    log_data_access: bool = True
    hash_chain_verified: bool = True
    signed_entries: bool = False       # Sprint 6+ — requires PKI
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)


class ComplianceConfig(BaseModel):
    name: str
    description: str
    audit: AuditRequirement = Field(default_factory=AuditRequirement)
    hitl: HITLRequirement = Field(default_factory=HITLRequirement)
    min_confidence: float = 0.0
    require_role_contracts: bool = True
    require_policy_enforcement: bool = True
    max_cost_per_decision_usd: float | None = None
    prohibited_data_categories: list[str] = Field(default_factory=list)
    required_fields_in_audit: list[str] = Field(default_factory=list)


# ── Built-in presets ──────────────────────────────────────────────────────────

SOC2_CONFIG = ComplianceConfig(
    name="SOC-2 Type II",
    description=(
        "SOC-2 Trust Services Criteria — Security, Availability, "
        "Processing Integrity, Confidentiality, Privacy."
    ),
    audit=AuditRequirement(
        log_all_tool_calls=True,
        log_actor_identity=True,
        log_data_access=True,
        hash_chain_verified=True,
        retention=RetentionPolicy(days=365, encrypt_at_rest=True),
    ),
    hitl=HITLRequirement(
        require_for_risk=["high", "critical"],
        require_actor_id=True,
        timeout_minutes=60,
    ),
    min_confidence=0.75,
    require_role_contracts=True,
    require_policy_enforcement=True,
    required_fields_in_audit=["actor", "run_id", "event", "ts"],
)

HIPAA_CONFIG = ComplianceConfig(
    name="HIPAA",
    description=(
        "Health Insurance Portability and Accountability Act — "
        "PHI handling, access controls, audit trails."
    ),
    audit=AuditRequirement(
        log_all_tool_calls=True,
        log_actor_identity=True,
        log_data_access=True,
        hash_chain_verified=True,
        retention=RetentionPolicy(
            days=2190,  # 6 years
            encrypt_at_rest=True,
            encrypt_in_transit=True,
            geo_restriction="US",
        ),
    ),
    hitl=HITLRequirement(
        require_for_risk=["medium", "high", "critical"],
        require_actor_id=True,
        require_notes=True,
        timeout_minutes=120,
    ),
    min_confidence=0.90,
    require_role_contracts=True,
    require_policy_enforcement=True,
    prohibited_data_categories=["phi", "pii", "ssn", "dob", "medical_record"],
    required_fields_in_audit=["actor", "run_id", "event", "ts", "role", "data"],
)

GDPR_CONFIG = ComplianceConfig(
    name="GDPR",
    description=(
        "EU General Data Protection Regulation — "
        "lawful basis, data minimisation, right to erasure, DPA notification."
    ),
    audit=AuditRequirement(
        log_all_tool_calls=True,
        log_actor_identity=True,
        log_data_access=True,
        hash_chain_verified=True,
        retention=RetentionPolicy(
            days=1095,  # 3 years
            encrypt_at_rest=True,
            geo_restriction="EU",
        ),
    ),
    hitl=HITLRequirement(
        require_for_risk=["high", "critical"],
        require_actor_id=True,
        require_notes=True,
        timeout_minutes=30,
    ),
    min_confidence=0.80,
    require_role_contracts=True,
    require_policy_enforcement=True,
    prohibited_data_categories=["pii", "sensitive_personal_data"],
    required_fields_in_audit=["actor", "run_id", "event", "ts"],
)

NIST_AI_RMF_CONFIG = ComplianceConfig(
    name="NIST AI RMF",
    description=(
        "NIST AI Risk Management Framework 1.0 — "
        "Govern, Map, Measure, Manage. AI trustworthiness and accountability."
    ),
    audit=AuditRequirement(
        log_all_tool_calls=True,
        log_actor_identity=True,
        log_data_access=True,
        hash_chain_verified=True,
        retention=RetentionPolicy(days=730, encrypt_at_rest=True),
    ),
    hitl=HITLRequirement(
        require_for_risk=["high", "critical"],
        require_actor_id=True,
        timeout_minutes=60,
    ),
    min_confidence=0.80,
    require_role_contracts=True,
    require_policy_enforcement=True,
    required_fields_in_audit=["actor", "run_id", "event", "ts", "role"],
)


class CompliancePreset(str, Enum):
    SOC2     = "soc2"
    HIPAA    = "hipaa"
    GDPR     = "gdpr"
    NIST_AI  = "nist_ai"

    def config(self) -> ComplianceConfig:
        _map = {
            CompliancePreset.SOC2:    SOC2_CONFIG,
            CompliancePreset.HIPAA:   HIPAA_CONFIG,
            CompliancePreset.GDPR:    GDPR_CONFIG,
            CompliancePreset.NIST_AI: NIST_AI_RMF_CONFIG,
        }
        cfg = _map.get(self)
        if cfg is None:
            raise ValueError(
                f"No ComplianceConfig registered for preset '{self}'. "
                "Add an entry to the _map in CompliancePreset.config()."
            )
        return cfg

    def summary(self) -> str:
        cfg = self.config()
        lines = [
            f"Compliance Preset: {cfg.name}",
            f"  {cfg.description}",
            f"  Audit retention  : {cfg.audit.retention.days} days",
            f"  Encrypt at rest  : {cfg.audit.retention.encrypt_at_rest}",
            f"  HITL required for: {', '.join(cfg.hitl.require_for_risk) or 'none'}",
            f"  Min confidence   : {cfg.min_confidence:.0%}",
            f"  Role contracts   : {'required' if cfg.require_role_contracts else 'optional'}",
            f"  Policy enforce   : {'required' if cfg.require_policy_enforcement else 'optional'}",
        ]
        if cfg.audit.retention.geo_restriction:
            lines.append(f"  Geo restriction  : {cfg.audit.retention.geo_restriction}")
        if cfg.prohibited_data_categories:
            lines.append(f"  Prohibited data  : {', '.join(cfg.prohibited_data_categories)}")
        return "\n".join(lines)
