"""
wire.hire() — primary HIRE interface.

Synchronous: wire.hire("...")     → WorkforceGraph (rule-based only)
Async:       await wire.hire_async("...")  → WorkforceGraph (rule + semantic + LLM fallback)
"""

from __future__ import annotations

from wire.hire.parser import HIREParser
from wire.hire.templates import RoleTemplate
from wire.hire.workforce import WorkforceGraph


def hire(
    intent: str,
    *,
    extra_templates: list[RoleTemplate] | None = None,
    llm_fallback: bool = False,
) -> WorkforceGraph:
    """
    Assemble a workforce from a plain-language intent description.

    Rule-based matching only (synchronous, zero cost, zero latency).
    For LLM-assisted matching use hire_async().

    Args:
        intent:           Plain-language description of what the workforce should do.
        extra_templates:  Custom role templates to add to the built-in registry.
        llm_fallback:     Reserved — use hire_async() for LLM fallback.

    Returns:
        WorkforceGraph — call .describe() to see what was assembled.

    Example:
        workforce = wire.hire(
            "Monitor AWS costs every hour. "
            "Open a Jira P1 if spend exceeds budget. "
            "Escalate to #ops-channel if no human responds in 30 minutes."
        )
        print(workforce.describe())
    """
    parser = HIREParser(extra_templates=extra_templates, llm_fallback=False)
    result = parser.parse(intent)
    return WorkforceGraph(intent=intent, parse_result=result)


async def hire_async(
    intent: str,
    *,
    extra_templates: list[RoleTemplate] | None = None,
    llm_model: str = "claude-haiku-4-5-20251001",
    force_llm: bool = False,
    use_semantic: bool = True,
) -> WorkforceGraph:
    """
    Assemble a workforce with semantic matching and LLM fallback for low-confidence matches.

    Pipeline:
      1. Rule-based — fast, zero cost.
      2. Semantic (SBERT) — activates when rule confidence < 0.70 and
         sentence-transformers is installed. Skipped when use_semantic=False.
      3. LLM fallback (Claude) — activates when semantic confidence < 0.65.
         Requires ANTHROPIC_API_KEY in environment.

    Args:
        intent:           Plain-language description.
        extra_templates:  Custom role templates.
        llm_model:        Claude model for LLM fallback.
        force_llm:        Always use LLM regardless of rule confidence.
        use_semantic:     Enable/disable the semantic matching tier (default True).
    """
    parser = HIREParser(
        extra_templates=extra_templates,
        llm_fallback=True,
        llm_model=llm_model,
        use_semantic=use_semantic,
    )
    result = await parser.parse_async(intent, force_llm=force_llm)
    return WorkforceGraph(intent=intent, parse_result=result)
