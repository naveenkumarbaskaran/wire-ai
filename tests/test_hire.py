"""Tests for HIRE engine — parser, templates, WorkforceGraph."""

from __future__ import annotations

import pytest

from wire.hire.parser import HIREParser
from wire.hire.templates import (
    ROLE_TEMPLATES, TEMPLATE_BY_NAME, TEMPLATES_BY_CATEGORY,
    RoleCategory, RoleTemplate, SLADefaults, AuthorityScope,
)
from wire.hire.workforce import WorkforceGraph
from wire.hire_api import hire


class TestTemplateRegistry:
    def test_exactly_20_templates(self) -> None:
        assert len(ROLE_TEMPLATES) == 20

    def test_all_categories_present(self) -> None:
        categories = {t.category for t in ROLE_TEMPLATES}
        assert categories == set(RoleCategory)

    def test_four_templates_per_category(self) -> None:
        for cat in RoleCategory:
            assert len(TEMPLATES_BY_CATEGORY[cat]) == 4, \
                f"Expected 4 templates in {cat}, got {len(TEMPLATES_BY_CATEGORY[cat])}"

    def test_all_names_unique(self) -> None:
        names = [t.name for t in ROLE_TEMPLATES]
        assert len(names) == len(set(names))

    def test_template_by_name_index_complete(self) -> None:
        assert len(TEMPLATE_BY_NAME) == 20

    def test_every_template_has_trigger_phrases(self) -> None:
        for t in ROLE_TEMPLATES:
            assert len(t.trigger_phrases) >= 3, \
                f"{t.name} needs at least 3 trigger phrases"

    def test_idempotent_execution_roles(self) -> None:
        # Side-effecting roles must be idempotent
        must_be_idempotent = {"ticket_creator", "notification_sender",
                               "workflow_trigger", "data_writer"}
        for name in must_be_idempotent:
            t = TEMPLATE_BY_NAME[name]
            assert t.idempotent, f"{name} must be idempotent"

    def test_high_risk_roles_have_escalate(self) -> None:
        from wire.core.models import Risk
        for t in ROLE_TEMPLATES:
            if t.risk_level in (Risk.HIGH, Risk.CRITICAL):
                assert t.authority.can_escalate, \
                    f"{t.name} is {t.risk_level} risk but cannot escalate"


class TestHIREParserRuleBased:
    def setup_method(self) -> None:
        self.parser = HIREParser(llm_fallback=False)

    def test_cost_monitor_matched(self) -> None:
        result = self.parser.parse("monitor AWS costs every hour")
        names = [m.template.name for m in result.matches]
        assert "cost_monitor" in names

    def test_ticket_creator_matched(self) -> None:
        result = self.parser.parse("create a Jira P1 ticket when alert fires")
        names = [m.template.name for m in result.matches]
        assert "ticket_creator" in names

    def test_human_escalator_matched(self) -> None:
        result = self.parser.parse("escalate to human if no response in 30 minutes")
        names = [m.template.name for m in result.matches]
        assert "human_escalator" in names

    def test_multiple_roles_matched(self) -> None:
        result = self.parser.parse(
            "monitor costs, detect anomalies, and create a Jira ticket"
        )
        assert len(result.matches) >= 2

    def test_empty_intent_returns_no_matches(self) -> None:
        result = self.parser.parse("")
        assert result.matches == []

    def test_unrelated_intent_returns_no_matches(self) -> None:
        result = self.parser.parse("the quick brown fox jumped over the lazy dog")
        assert result.matches == []
        assert len(result.warnings) > 0

    def test_confidence_between_0_and_1(self) -> None:
        result = self.parser.parse("monitor AWS costs and create Jira ticket")
        for m in result.matches:
            assert 0.0 <= m.confidence <= 1.0

    def test_source_is_rule(self) -> None:
        result = self.parser.parse("monitor costs")
        assert result.source == "rule"
        for m in result.matches:
            assert m.source == "rule"

    def test_normalisation_handles_punctuation(self) -> None:
        r1 = self.parser.parse("monitor costs!")
        r2 = self.parser.parse("monitor costs")
        assert [m.template.name for m in r1.matches] == \
               [m.template.name for m in r2.matches]

    def test_case_insensitive(self) -> None:
        r1 = self.parser.parse("MONITOR COSTS")
        r2 = self.parser.parse("monitor costs")
        assert [m.template.name for m in r1.matches] == \
               [m.template.name for m in r2.matches]

    def test_full_workflow_intent(self) -> None:
        result = self.parser.parse(
            "Monitor AWS spend every hour. "
            "Flag anomalies over $500. "
            "Open a Jira P1 if spend exceeds budget. "
            "Escalate to ops channel if no human responds."
        )
        names = [m.template.name for m in result.matches]
        assert "cost_monitor" in names
        assert "ticket_creator" in names
        assert "human_escalator" in names

    def test_report_generator_matched(self) -> None:
        result = self.parser.parse("generate a weekly report on system health")
        names = [m.template.name for m in result.matches]
        assert "report_generator" in names

    def test_anomaly_detector_matched(self) -> None:
        result = self.parser.parse("detect anomalies in the metrics stream")
        names = [m.template.name for m in result.matches]
        assert "anomaly_detector" in names

    def test_log_analyser_matched(self) -> None:
        result = self.parser.parse("analyse logs for error patterns")
        names = [m.template.name for m in result.matches]
        assert "log_analyser" in names

    def test_notification_sender_matched(self) -> None:
        result = self.parser.parse("send alert to slack channel")
        names = [m.template.name for m in result.matches]
        assert "notification_sender" in names


class TestWorkforceGraph:
    def test_describe_returns_string(self) -> None:
        workforce = hire("monitor AWS costs and create Jira ticket on breach")
        desc = workforce.describe()
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_describe_contains_role_names(self) -> None:
        workforce = hire("monitor costs and open a ticket")
        desc = workforce.describe()
        for node in workforce.nodes:
            assert node.role in desc

    def test_role_names_list(self) -> None:
        workforce = hire("monitor costs")
        assert isinstance(workforce.role_names(), list)

    def test_repr_shows_roles(self) -> None:
        workforce = hire("monitor AWS costs")
        r = repr(workforce)
        assert "WorkforceGraph" in r

    def test_highest_risk_returns_risk(self) -> None:
        from wire.core.models import Risk
        workforce = hire("escalate to human for approval")
        risk = workforce.highest_risk()
        assert isinstance(risk, Risk)

    def test_empty_workforce_describe_graceful(self) -> None:
        workforce = hire("the quick brown fox")
        desc = workforce.describe()
        assert "No workforce assembled" in desc

    def test_nodes_match_parse_result(self) -> None:
        workforce = hire("monitor costs and create jira ticket")
        assert len(workforce.nodes) == len(workforce.parse_result.matches)


class TestHireTopLevel:
    def test_hire_returns_workforce_graph(self) -> None:
        from wire.hire.workforce import WorkforceGraph
        workforce = hire("monitor AWS costs")
        assert isinstance(workforce, WorkforceGraph)

    def test_hire_import_from_wire(self) -> None:
        import wire
        assert callable(wire.hire)

    def test_hire_complex_intent(self) -> None:
        workforce = hire(
            "Every hour, monitor our AWS costs. "
            "If there's an anomaly, analyse the logs. "
            "Create a Jira P1 ticket and notify the ops team on Slack. "
            "Escalate to a human if nobody responds within 30 minutes."
        )
        assert len(workforce.nodes) >= 3

    def test_custom_template_registration(self) -> None:
        from wire.core.models import Risk
        custom = RoleTemplate(
            name="custom_role",
            category=RoleCategory.EXECUTION,
            description="Custom test role",
            trigger_phrases=["do the custom thing", "custom action"],
            risk_level=Risk.LOW,
        )
        workforce = hire("do the custom thing now", extra_templates=[custom])
        assert "custom_role" in workforce.role_names()
