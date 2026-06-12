"""Shared Pydantic models and enums used across WIRE."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Risk(str, Enum):
    """Risk level for agent decisions and tool calls."""
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Backend(str, Enum):
    """Supported agent framework backends."""
    LANGGRAPH = "langgraph"
    CREWAI    = "crewai"
    AUTOGEN   = "autogen"
    OPENAI    = "openai"
    FOUNDRY   = "foundry"


class AuditBackend(str, Enum):
    """Storage backend for the AuditChain."""
    LOCAL    = "local"     # JSONL file (default, zero deps)
    SQLITE   = "sqlite"    # SQLite via sqlmodel
    POSTGRES = "postgres"  # PostgreSQL via asyncpg
    S3       = "s3"        # AWS S3 (enterprise)


class DeployConfig(BaseModel):
    """Full configuration for a wire.deploy() call."""

    backend: Backend = Backend.LANGGRAPH
    audit_backend: AuditBackend = AuditBackend.LOCAL
    audit_path: str = "wire-audit.jsonl"

    max_iterations: int = Field(default=50, ge=1, le=10_000)
    max_cost_usd: float | None = Field(default=None, ge=0)
    hourly_budget_usd: float | None = Field(default=None, ge=0)
    daily_budget_usd: float | None = Field(default=None, ge=0)

    # OTel tracing
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    otel_service_name: str = "wire-ai"

    # Prometheus metrics
    metrics_enabled: bool = False
    metrics_port: int = Field(default=9090, ge=1024, le=65535)

    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_budgets(self) -> "DeployConfig":
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be > 0")
        return self
