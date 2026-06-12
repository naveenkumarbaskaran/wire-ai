"""
Semantic matching tier for the HIRE engine.

Uses sentence-transformers (SBERT) to compute cosine similarity between
the parsed intent and each role template's description + trigger phrases.

Sits between rule-based matching and LLM fallback:
  1. Rule-based  (fast, deterministic)
  2. Semantic    (this module — activates if rule confidence < 0.70 AND SBERT available)
  3. LLM fallback (Claude — activates if semantic confidence < 0.65)

sentence-transformers is an optional dependency. When not installed,
is_available() returns False and the pipeline skips gracefully.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from wire.hire.templates import RoleTemplate
    from wire.hire.parser import MatchResult

log = structlog.get_logger(__name__)

# Minimum cosine similarity to include a template in results
_MIN_SIMILARITY = 0.40
# Maximum number of matches to return
_MAX_MATCHES = 5


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _to_float_list(vec: Any) -> list[float]:
    """Convert a vector to a plain Python list of floats (handles numpy arrays and lists)."""
    if hasattr(vec, "tolist"):
        return vec.tolist()  # numpy array
    return [float(x) for x in vec]


class SemanticMatcher:
    """
    Embedding-based semantic matcher using sentence-transformers (SBERT).

    Usage:
        matcher = SemanticMatcher()
        if matcher.is_available():
            results = matcher.match(intent, templates)
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._available: bool | None = None  # None = not yet checked

    def is_available(self) -> bool:
        """Return True if sentence-transformers is installed and loadable."""
        if self._available is not None:
            return self._available

        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]
            self._model = SentenceTransformer(self._model_name)
            self._available = True
            log.debug("semantic_matcher_ready", model=self._model_name)
        except ImportError:
            log.debug("semantic_matcher_unavailable", reason="sentence-transformers not installed")
            self._available = False
        except Exception as exc:
            log.warning("semantic_matcher_load_failed", error=str(exc))
            self._available = False

        return self._available

    def encode(self, texts: list[str]) -> Any:
        """
        Encode a list of texts into embeddings.

        Returns a list of float vectors (one per text).
        Raises RuntimeError if sentence-transformers is not available.
        """
        if not self.is_available():
            raise RuntimeError("sentence-transformers is not installed")
        embeddings = self._model.encode(texts, convert_to_numpy=True)
        return embeddings

    def match(
        self,
        intent: str,
        templates: list[RoleTemplate],
    ) -> list[MatchResult]:
        """
        Compute cosine similarity between the intent and each template.

        Encoding strategy per template:
            description + " " + " ".join(trigger_phrases[:5])

        Returns:
            MatchResult list sorted by similarity score (descending),
            filtered to similarity > _MIN_SIMILARITY, capped at _MAX_MATCHES.
            Each MatchResult has source="semantic".
        """
        from wire.hire.parser import MatchResult  # local import to avoid circular

        if not self.is_available() or not templates:
            return []

        # Build corpus: intent first, then one text per template
        template_texts = [
            t.description + " " + " ".join(t.trigger_phrases[:5])
            for t in templates
        ]
        all_texts = [intent] + template_texts

        try:
            embeddings = self.encode(all_texts)
        except Exception as exc:
            log.warning("semantic_encode_failed", error=str(exc))
            return []

        intent_vec: list[float] = _to_float_list(embeddings[0])

        # Assign workflow order (mirrors rule-based order map)
        _order_map = {
            "monitoring":   0,
            "analysis":     1,
            "execution":    2,
            "governance":   3,
            "coordination": 4,
        }

        scored: list[tuple[float, RoleTemplate]] = []
        for i, template in enumerate(templates):
            template_vec: list[float] = _to_float_list(embeddings[i + 1])
            similarity = _cosine_similarity(intent_vec, template_vec)
            if similarity > _MIN_SIMILARITY:
                scored.append((similarity, template))

        scored.sort(key=lambda x: -x[0])
        scored = scored[:_MAX_MATCHES]

        results: list[MatchResult] = []
        for similarity, template in scored:
            results.append(MatchResult(
                template=template,
                confidence=round(float(similarity), 3),
                matched_phrases=[],
                source="semantic",
                order=_order_map.get(template.category.value, 9),
            ))

        log.info(
            "semantic_match_complete",
            intent=intent[:80],
            matches=[r.template.name for r in results],
            top_confidence=round(results[0].confidence, 3) if results else 0.0,
        )
        return results
