"""Tests for PolicyEnforcer — authority scopes, read-only enforcement, write scopes."""

from __future__ import annotations

import pytest

from wire.core.models import Risk
from wire.core.policy import PolicyEnforcer, PolicyViolationError, ToolCallContext
from wire.hire.templates import AuthorityScope, RoleTemplate, RoleCategory, TEMPLATE_BY_NAME


def _ctx(role: str, tool: str, args: dict | None = None, cost: float = 0.0) -> ToolCallContext:
    return ToolCallContext(role=role, tool_name=tool, args=args or {}, run_id="r1", estimated_cost_usd=cost)


def _make_template(
    name: str = "test_role",
    can_read: list[str] | None = None,
    can_write: list[str] | None = None,
    max_spend: float | None = None,
    risk: Risk = Risk.LOW,
    idempotent: bool = False,
) -> RoleTemplate:
    return RoleTemplate(
        name=name,
        category=RoleCategory.MONITORING,
        description="test",
        trigger_phrases=["test"],
        authority=AuthorityScope(
            can_read=can_read or [],
            can_write=can_write or [],
            max_spend_usd=max_spend,
        ),
        risk_level=risk,
        idempotent=idempotent,
    )


class TestReadOnlyEnforcement:
    def test_read_tool_on_readonly_role_passes(self) -> None:
        t = _make_template(can_read=["metrics"], can_write=[])
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("monitor", "get_metrics"))  # must not raise

    def test_write_tool_on_readonly_role_raises(self) -> None:
        t = _make_template(can_read=["metrics"], can_write=[])
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError) as exc_info:
            enforcer.check(_ctx("monitor", "create_record"))
        assert "read-only" in str(exc_info.value)
        assert exc_info.value.role == "monitor"
        assert exc_info.value.tool == "create_record"

    def test_delete_tool_on_readonly_role_raises(self) -> None:
        t = _make_template(can_read=["logs"], can_write=[])
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError):
            enforcer.check(_ctx("log_reader", "delete_log_entry"))

    def test_send_tool_on_readonly_role_raises(self) -> None:
        t = _make_template(can_read=["metrics"], can_write=[])
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError):
            enforcer.check(_ctx("monitor", "send_alert"))

    def test_monitoring_templates_are_readonly(self) -> None:
        for name in ["cost_monitor", "uptime_monitor", "anomaly_detector", "sla_watcher"]:
            t = TEMPLATE_BY_NAME[name]
            enforcer = PolicyEnforcer(t)
            with pytest.raises(PolicyViolationError):
                enforcer.check(_ctx(name, "create_jira_ticket"))

    def test_fetch_tool_passes_on_readonly_role(self) -> None:
        t = _make_template(can_read=["logs"], can_write=[])
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("monitor", "fetch_logs"))  # must not raise

    def test_search_tool_passes_on_readonly_role(self) -> None:
        t = _make_template(can_read=["metrics"], can_write=[])
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("monitor", "search_metrics"))  # must not raise


class TestWriteScopeEnforcement:
    def test_write_to_allowed_destination_passes(self) -> None:
        t = _make_template(can_write=["jira"])
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("executor", "create_jira_issue"))  # must not raise

    def test_write_to_disallowed_destination_raises(self) -> None:
        t = _make_template(can_write=["jira"])
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError) as exc_info:
            enforcer.check(_ctx("executor", "create_github_issue"))
        assert "github" in str(exc_info.value).lower()

    def test_execution_templates_can_write_correct_destinations(self) -> None:
        t = TEMPLATE_BY_NAME["ticket_creator"]
        enforcer = PolicyEnforcer(t)
        # Should pass — jira is in can_write
        enforcer.check(_ctx("ticket_creator", "create_jira_ticket"))

    def test_notification_sender_can_write_slack(self) -> None:
        t = TEMPLATE_BY_NAME["notification_sender"]
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("notification_sender", "send_slack_message"))  # must not raise


class TestSpendingCap:
    def test_under_cap_passes(self) -> None:
        t = _make_template(can_write=["db"], max_spend=1.0)
        enforcer = PolicyEnforcer(t)
        enforcer.check(_ctx("writer", "write_database", cost=0.50))
        enforcer.check(_ctx("writer", "write_database", cost=0.40))  # total 0.90

    def test_over_cap_raises(self) -> None:
        t = _make_template(can_write=["db"], max_spend=0.50)
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError) as exc_info:
            enforcer.check(_ctx("writer", "write_database", cost=0.60))
        assert "spending cap" in str(exc_info.value)

    def test_zero_cap_blocks_all_spend(self) -> None:
        t = _make_template(can_write=["jira"], max_spend=0.0)
        enforcer = PolicyEnforcer(t)
        with pytest.raises(PolicyViolationError):
            enforcer.check(_ctx("writer", "create_jira", cost=0.001))

    def test_no_cap_never_blocks(self) -> None:
        t = _make_template(can_write=["db"], max_spend=None)
        enforcer = PolicyEnforcer(t)
        for _ in range(100):
            enforcer.check(_ctx("writer", "write_database", cost=99.99))


class TestBuiltinTemplatesPolicies:
    def test_compliance_checker_is_critical_risk(self) -> None:
        t = TEMPLATE_BY_NAME["compliance_checker"]
        assert t.risk_level == Risk.CRITICAL

    def test_high_risk_templates_have_escalation(self) -> None:
        for name in ["data_writer", "workflow_trigger", "human_escalator", "approval_router"]:
            t = TEMPLATE_BY_NAME[name]
            assert t.authority.can_escalate, f"{name} should be able to escalate"

    def test_ticket_creator_is_idempotent(self) -> None:
        assert TEMPLATE_BY_NAME["ticket_creator"].idempotent

    def test_notification_sender_is_idempotent(self) -> None:
        assert TEMPLATE_BY_NAME["notification_sender"].idempotent

    def test_cost_monitor_no_write(self) -> None:
        t = TEMPLATE_BY_NAME["cost_monitor"]
        assert t.authority.can_write == []

    def test_report_generator_can_write_reports(self) -> None:
        t = TEMPLATE_BY_NAME["report_generator"]
        assert "reports" in t.authority.can_write
