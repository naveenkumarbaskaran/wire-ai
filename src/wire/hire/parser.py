"""
HIRE parser — converts plain-language intent into a list of matched RoleTemplates.

Three-stage pipeline:
  1. Rule-based matcher — keyword/phrase scoring against template trigger_phrases.
     Fast, deterministic, zero cost. Confidence = overlap score (0.0–1.0).
  2. Semantic matching (SBERT) — activates when rule confidence < 0.70 AND
     sentence-transformers is installed. Cosine similarity between intent
     embedding and template embeddings.
  3. LLM fallback (Claude) — activates when semantic confidence < 0.65 or
     when the user sets force_llm=True. Returns structured MatchResult list.

The parser never hallucinates roles — it only returns templates from the
built-in registry (or user-registered custom templates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from wire.hire.templates import ROLE_TEMPLATES, RoleTemplate, TEMPLATE_BY_NAME

log = structlog.get_logger(__name__)

# Confidence threshold below which semantic / LLM fallback is triggered
_RULE_CONFIDENCE_THRESHOLD = 0.70
# Confidence threshold below which LLM fallback is triggered (after semantic)
_SEMANTIC_CONFIDENCE_THRESHOLD = 0.65


@dataclass
class MatchResult:
    template: RoleTemplate
    confidence: float              # 0.0–1.0
    matched_phrases: list[str]
    source: str = "rule"           # "rule" | "llm" | "semantic"
    order: int = 0                 # position in the assembled WorkforceGraph


@dataclass
class ParseResult:
    intent: str
    matches: list[MatchResult]
    confidence: float              # overall parse confidence
    source: str = "rule"           # "rule" | "llm" | "semantic" | "hybrid"
    warnings: list[str] = field(default_factory=list)


class HIREParser:
    """
    Parses a plain-language workforce description into matched role templates.

    Usage:
        parser = HIREParser()
        result = parser.parse("Monitor AWS costs every hour and open a Jira P1 on breach")
        for match in result.matches:
            print(match.template.name, match.confidence)
    """

    def __init__(
        self,
        extra_templates: list[RoleTemplate] | None = None,
        llm_fallback: bool = True,
        llm_model: str = "claude-haiku-4-5-20251001",
        use_semantic: bool = True,
    ) -> None:
        self._templates = ROLE_TEMPLATES + (extra_templates or [])
        self._llm_fallback = llm_fallback
        self._llm_model = llm_model

        # Lazy-initialised; is_available() handles missing sentence-transformers
        from wire.hire.semantic import SemanticMatcher
        self._semantic = SemanticMatcher() if use_semantic else _NullSemanticMatcher()

    # ── Public API ────────────────────────────────────────────────────────────

    def parse(self, intent: str, force_llm: bool = False) -> ParseResult:
        """
        Synchronous parse — rule-based only.
        Use parse_async() for LLM fallback.
        """
        normalised = self._normalise(intent)
        matches = self._rule_match(normalised)
        confidence = max((m.confidence for m in matches), default=0.0)

        result = ParseResult(
            intent=intent,
            matches=sorted(matches, key=lambda m: m.order),
            confidence=confidence,
            source="rule",
        )

        if not matches:
            result.warnings.append(
                "No role templates matched. Try parse_async() for LLM-assisted matching, "
                "or describe the intent using action verbs (monitor, analyse, create, escalate)."
            )

        log.debug(
            "hire_parsed",
            intent=intent[:80],
            matches=[m.template.name for m in matches],
            confidence=round(confidence, 2),
            source="rule",
        )
        return result

    async def parse_async(self, intent: str, force_llm: bool = False) -> ParseResult:
        """
        Async parse — rule-based first, semantic tier second, LLM fallback last.
        """
        result = self.parse(intent)

        if force_llm or result.confidence < _RULE_CONFIDENCE_THRESHOLD:
            # ── Tier 2: Semantic matching ─────────────────────────────────────
            if self._semantic.is_available():
                log.info(
                    "hire_semantic_fallback",
                    rule_confidence=round(result.confidence, 2),
                    threshold=_RULE_CONFIDENCE_THRESHOLD,
                )
                semantic_result = self._semantic_match(intent, result)
                if semantic_result and semantic_result.confidence >= _SEMANTIC_CONFIDENCE_THRESHOLD:
                    return semantic_result
                # If semantic ran but was below threshold, pass its result to
                # LLM evaluation (keep original rule result if semantic found nothing)
                if semantic_result:
                    result = semantic_result

            # ── Tier 3: LLM fallback ──────────────────────────────────────────
            if self._llm_fallback:
                log.info(
                    "hire_llm_fallback",
                    rule_confidence=round(result.confidence, 2),
                    threshold=_RULE_CONFIDENCE_THRESHOLD,
                )
                llm_result = await self._llm_match(intent, result)
                if llm_result:
                    return llm_result

        return result

    # ── Rule-based matcher ────────────────────────────────────────────────────

    def _rule_match(self, normalised: str) -> list[MatchResult]:
        scored: list[tuple[float, list[str], RoleTemplate]] = []

        for template in self._templates:
            matched: list[str] = []
            for phrase in template.trigger_phrases:
                if phrase in normalised:
                    matched.append(phrase)

            if not matched:
                continue

            intent_words = max(len(normalised.split()), 1)
            matched_words = sum(len(p.split()) for p in matched)
            longest_phrase_words = max(len(p.split()) for p in matched)

            # Base score: fraction of intent words matched
            base = min(matched_words / intent_words, 1.0)
            # Specificity bonus: longer individual phrases are more specific
            # Capped at 0.20 to avoid short generic phrases appearing too confident
            specificity = min(longest_phrase_words * 0.04, 0.20)
            # Coverage bonus: more phrases matched = more confident
            coverage = min(len(matched) * 0.02, 0.10)
            score = min(base + specificity + coverage, 1.0)
            scored.append((score, matched, template))

        if not scored:
            return []

        # Deduplicate overlapping templates — keep highest score per category
        scored.sort(key=lambda x: -x[0])

        # Assign workflow order based on natural dependency:
        # monitoring → analysis → execution → governance → coordination
        _order_map = {
            "monitoring":   0,
            "analysis":     1,
            "execution":    2,
            "governance":   3,
            "coordination": 4,
        }

        results: list[MatchResult] = []
        seen_names: set[str] = set()

        for score, phrases, template in scored:
            if template.name in seen_names:
                continue
            seen_names.add(template.name)
            results.append(MatchResult(
                template=template,
                confidence=round(score, 3),
                matched_phrases=phrases,
                source="rule",
                order=_order_map.get(template.category.value, 9),
            ))

        return results

    # ── Semantic matching ─────────────────────────────────────────────────────

    def _semantic_match(
        self, intent: str, rule_result: ParseResult
    ) -> ParseResult | None:
        """Run SemanticMatcher and wrap results in a ParseResult."""
        sem_matches = self._semantic.match(intent, self._templates)
        if not sem_matches:
            return None

        overall = sum(m.confidence for m in sem_matches) / len(sem_matches)
        return ParseResult(
            intent=intent,
            matches=sorted(sem_matches, key=lambda m: m.order),
            confidence=round(max(m.confidence for m in sem_matches), 3),
            source="semantic",
        )

    # ── LLM fallback ─────────────────────────────────────────────────────────

    async def _llm_match(
        self, intent: str, rule_result: ParseResult
    ) -> ParseResult | None:
        try:
            import anthropic
        except ImportError:
            log.warning("llm_fallback_unavailable", reason="anthropic not installed")
            return None

        template_list = "\n".join(
            f"- {t.name}: {t.description}"
            for t in self._templates
        )

        prompt = f"""You are WIRE's HIRE engine. Match the user's intent to role templates.

Available templates:
{template_list}

User intent: "{intent}"

Return a JSON array of matched roles, ordered by execution sequence:
[
  {{"name": "template_name", "confidence": 0.85, "reason": "brief reason"}},
  ...
]

Rules:
- Only use template names from the list above. Never invent new ones.
- Include every role genuinely needed to fulfil the intent.
- Confidence: 0.9+ = clear match, 0.7–0.9 = likely, 0.5–0.7 = possible.
- Order by natural workflow sequence (monitor → analyse → execute → escalate).
- Return [] if no templates match.
- Return only the JSON array, no other text."""

        try:
            client = anthropic.Anthropic()
            message = client.messages.create(
                model=self._llm_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()

            import json
            parsed = json.loads(raw)

            matches: list[MatchResult] = []
            for i, item in enumerate(parsed):
                name = item.get("name", "")
                template = TEMPLATE_BY_NAME.get(name)
                if template:
                    matches.append(MatchResult(
                        template=template,
                        confidence=float(item.get("confidence", 0.75)),
                        matched_phrases=[item.get("reason", "")],
                        source="llm",
                        order=i,
                    ))

            if not matches:
                return None

            overall = sum(m.confidence for m in matches) / len(matches)
            return ParseResult(
                intent=intent,
                matches=matches,
                confidence=round(overall, 3),
                source="llm",
            )

        except Exception as e:
            log.warning("llm_fallback_failed", error=str(e))
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise(text: str) -> str:
        """Lowercase, collapse whitespace, strip punctuation for matching."""
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


# ── Null object for use_semantic=False ───────────────────────────────────────

class _NullSemanticMatcher:
    """Drop-in replacement that always reports unavailable. Used when use_semantic=False."""

    def is_available(self) -> bool:
        return False

    def match(self, intent: str, templates: list[RoleTemplate]) -> list[MatchResult]:
        return []
