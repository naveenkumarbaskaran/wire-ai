"""Hire package — HIRE engine, parser, templates, workforce graph."""

from wire.hire.parser import HIREParser, ParseResult, MatchResult
from wire.hire.templates import (
    RoleTemplate, RoleCategory, AuthorityScope, SLADefaults,
    ROLE_TEMPLATES, TEMPLATE_BY_NAME, TEMPLATES_BY_CATEGORY,
)
from wire.hire.workforce import WorkforceGraph, WorkforceNode

__all__ = [
    "HIREParser", "ParseResult", "MatchResult",
    "RoleTemplate", "RoleCategory", "AuthorityScope", "SLADefaults",
    "ROLE_TEMPLATES", "TEMPLATE_BY_NAME", "TEMPLATES_BY_CATEGORY",
    "WorkforceGraph", "WorkforceNode",
]
