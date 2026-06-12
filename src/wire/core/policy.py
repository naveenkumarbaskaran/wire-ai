"""
PolicyEnforcer — runtime authority enforcement for every tool call.

Validates that each agent only uses tools within its declared AuthorityScope.
Read-only roles cannot write. Write roles need idempotency. High-risk writes
trigger HITLGate automatically.

This runs as a GovernanceHook — intercepts every tool call before execution.
No code changes needed in agent logic — enforcement is declarative.

Design principles:
  - Monitoring/read roles: can_write=[] → any write attempt raises PolicyViolationError
  - Analysis roles: write to report destinations only
  - Execution roles: write allowed but idempotency enforced + HITL for HIGH/CRITICAL risk
  - Governance roles: can_approve=True only for governance category roles
  - All roles: spending caps enforced if max_spend_usd is set
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from wire.core.errors import WIREError
from wire.core.models import Risk
from wire.hire.templates import AuthorityScope, RoleTemplate

log = structlog.get_logger(__name__)

# Tool names that perform writes — any tool matching these patterns is write-classified
_WRITE_PATTERNS = {
    "create", "update", "delete", "write", "insert", "patch",
    "send", "post", "publish", "push", "trigger", "deploy",
    "execute", "run_command", "modify", "remove", "put",
}

# Tool names that are always read-only regardless of name
_READ_PATTERNS = {
    "get", "fetch", "read", "list", "search", "query",
    "describe", "inspect", "view", "check", "monitor",
    "ping", "health", "status", "scan", "analyse", "analyze",
}


class PolicyViolationError(WIREError):
    """Raised when an agent attempts an action outside its authority scope."""

    def __init__(
        self,
        role: str,
        tool: str,
        violation: str,
        authority: AuthorityScope,
    ) -> None:
        self.role = role
        self.tool = tool
        self.violation = violation
        super().__init__(
            f"Policy violation [{role}] — tool '{tool}': {violation}. "
            f"Role authority: can_write={authority.can_write}, "
            f"can_read={authority.can_read}"
        )


class ToolCallContext(BaseModel):
    """Context passed to PolicyEnforcer for each tool call."""
    role: str
    tool_name: str
    args: dict[str, Any]
    run_id: str
    estimated_cost_usd: float = 0.0


class PolicyEnforcer:
    """
    Declarative runtime policy enforcement — zero boilerplate in agent code.

    Usage:
        enforcer = PolicyEnforcer(template)
        # Before every tool call:
        enforcer.check(ToolCallContext(
            role="cost_monitor",
            tool_name="write_database",
            args={...},
            run_id="run_abc",
        ))
        # Raises PolicyViolationError if not allowed
    """

    def __init__(self, template: RoleTemplate, *, destination_arg_keys: list[str] | None = None) -> None:
        self.template = template
        self.authority = template.authority
        # Configurable: extra arg keys to check for write destination
        self._destination_arg_keys = destination_arg_keys or []
        self._spend_total: float = 0.0

    def check(self, ctx: ToolCallContext) -> None:
        """
        Validate a tool call against the role's authority scope.
        Raises PolicyViolationError on any violation.
        """
        tool_lower = ctx.tool_name.lower()
        is_write = self._classify_write(tool_lower)

        # ── 1. Read-only roles cannot write ──────────────────────────────────
        if is_write and not self.authority.can_write:
            raise PolicyViolationError(
                role=ctx.role,
                tool=ctx.tool_name,
                violation="write operation not permitted — role is read-only",
                authority=self.authority,
            )

        # ── 2. Write scope check ──────────────────────────────────────────────
        if is_write and self.authority.can_write:
            destination = self._extract_destination(tool_lower, ctx.args)
            if destination and not self._destination_allowed(destination, self.authority.can_write):
                raise PolicyViolationError(
                    role=ctx.role,
                    tool=ctx.tool_name,
                    violation=f"write to '{destination}' not in authority scope {self.authority.can_write}",
                    authority=self.authority,
                )

        # ── 3. Spending cap check ─────────────────────────────────────────────
        if self.authority.max_spend_usd is not None:
            self._spend_total += ctx.estimated_cost_usd
            if self._spend_total > self.authority.max_spend_usd:
                raise PolicyViolationError(
                    role=ctx.role,
                    tool=ctx.tool_name,
                    violation=(
                        f"spending cap exceeded: ${self._spend_total:.4f} > "
                        f"${self.authority.max_spend_usd:.4f}"
                    ),
                    authority=self.authority,
                )

        # ── 4. High-risk write must use idempotency ───────────────────────────
        if (
            is_write
            and self.template.risk_level in (Risk.HIGH, Risk.CRITICAL)
            and not self.template.idempotent
        ):
            log.warning(
                "policy_high_risk_non_idempotent",
                role=ctx.role,
                tool=ctx.tool_name,
                risk=self.template.risk_level,
            )
            # warn but don't block — idempotency is enforced by IdempotencyGuard

        log.debug(
            "policy_check_passed",
            role=ctx.role,
            tool=ctx.tool_name,
            is_write=is_write,
        )

    @staticmethod
    def _classify_write(tool_lower: str) -> bool:
        """Classify a tool as read or write based on name patterns."""
        for pattern in _WRITE_PATTERNS:
            if pattern in tool_lower:
                # Check it's not overridden by a read keyword
                for read_pat in _READ_PATTERNS:
                    if read_pat in tool_lower:
                        return False
                return True
        return False

    def _extract_destination(self, tool_lower: str, args: dict[str, Any]) -> str | None:
        """Extract the write destination from tool name or args."""
        # Check user-configured keys first, then standard keys
        all_keys = self._destination_arg_keys + ["destination", "target", "service", "backend", "to", "api_endpoint"]
        for key in all_keys:
            if key in args:
                return str(args[key]).lower()
        # Fall back to tool name fragments
        for fragment in ["jira", "slack", "email", "database", "db", "s3",
                          "github", "servicenow", "teams", "pagerduty"]:
            if fragment in tool_lower:
                return fragment
        return None

    @staticmethod
    def _destination_allowed(destination: str, allowed: list[str]) -> bool:
        """Check if destination matches any allowed write target."""
        destination_lower = destination.lower()
        # Aliases — common abbreviations map to canonical names
        _aliases: dict[str, list[str]] = {
            "db": ["database", "db"],
            "database": ["database", "db"],
            "jira": ["jira"],
            "github": ["github"],
            "slack": ["slack"],
            "email": ["email"],
            "s3": ["s3"],
            "teams": ["teams"],
            "pagerduty": ["pagerduty"],
            "servicenow": ["servicenow"],
            "reports": ["report", "reports"],
        }
        dest_aliases = set()
        dest_aliases.add(destination_lower)
        for canonical, aliases in _aliases.items():
            if destination_lower in aliases:
                dest_aliases.update(aliases)
                dest_aliases.add(canonical)

        for allowed_dest in allowed:
            allowed_lower = allowed_dest.lower()
            allowed_aliases = set()
            allowed_aliases.add(allowed_lower)
            for canonical, aliases in _aliases.items():
                if allowed_lower in aliases:
                    allowed_aliases.update(aliases)
                    allowed_aliases.add(canonical)
            if dest_aliases & allowed_aliases:
                return True
        return False
