"""
Tests for the HIRE engine semantic matching tier (SBERT).

All tests work without sentence-transformers installed — sentence-transformers
is mocked throughout. Tests that need real embeddings are marked with
pytest.mark.skipif and skipped unless the package is available.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from wire.hire.parser import HIREParser, MatchResult, ParseResult, _SEMANTIC_CONFIDENCE_THRESHOLD
from wire.hire.semantic import SemanticMatcher, _cosine_similarity
from wire.hire.templates import ROLE_TEMPLATES, TEMPLATE_BY_NAME, RoleTemplate, RoleCategory
from wire.hire_api import hire_async
from wire.core.models import Risk


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_template(name: str = "test_role") -> RoleTemplate:
    return RoleTemplate(
        name=name,
        category=RoleCategory.EXECUTION,
        description="Test role for unit tests",
        trigger_phrases=["do the test", "run test"],
        risk_level=Risk.LOW,
    )


def _make_embedding(dim: int = 4, value: float = 1.0) -> list[float]:
    """Return a unit-ish vector."""
    vec = [value] * dim
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


# ── Unit: cosine similarity ────────────────────────────────────────────────────

class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        v = [1.0, 0.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_known_similarity(self) -> None:
        # [1, 1] vs [1, 0]: cos(45°) = 1/sqrt(2) ≈ 0.7071
        a = [1.0, 1.0]
        b = [1.0, 0.0]
        result = _cosine_similarity(a, b)
        assert result == pytest.approx(1.0 / math.sqrt(2), rel=1e-5)

    def test_zero_vector_returns_zero(self) -> None:
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_both_zero_vectors(self) -> None:
        assert _cosine_similarity([0.0], [0.0]) == 0.0

    def test_3d_vectors(self) -> None:
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0, 3.0]
        assert _cosine_similarity(a, b) == pytest.approx(1.0)


# ── Unit: SemanticMatcher.is_available() ─────────────────────────────────────

class TestSemanticMatcherAvailability:
    def test_returns_false_when_sentence_transformers_missing(self) -> None:
        matcher = SemanticMatcher()
        # Reset cached state
        matcher._available = None
        matcher._model = None

        with patch.dict("sys.modules", {"sentence_transformers": None}):
            # Force ImportError path by patching the import inside is_available
            with patch("builtins.__import__", side_effect=_import_blocker("sentence_transformers")):
                result = matcher.is_available()

        assert result is False

    def test_returns_true_when_sentence_transformers_installed(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = None
        matcher._model = None

        mock_st = MagicMock()
        mock_model = MagicMock()
        mock_st.SentenceTransformer.return_value = mock_model

        with patch.dict("sys.modules", {"sentence_transformers": mock_st}):
            result = matcher.is_available()

        assert result is True
        assert matcher._model is mock_model

    def test_is_available_caches_result(self) -> None:
        """Second call returns cached value without re-importing."""
        matcher = SemanticMatcher()
        matcher._available = True  # pre-cached

        # Even if sentence-transformers is missing, cached True is returned
        with patch("builtins.__import__", side_effect=ImportError("blocked")):
            result = matcher.is_available()

        assert result is True

    def test_returns_false_on_load_error(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = None
        matcher._model = None

        mock_st = MagicMock()
        mock_st.SentenceTransformer.side_effect = RuntimeError("CUDA not available")

        with patch.dict("sys.modules", {"sentence_transformers": mock_st}):
            result = matcher.is_available()

        assert result is False


def _import_blocker(blocked_module: str):
    """Return a side_effect function that raises ImportError for one specific module."""
    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

    def _blocker(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == blocked_module or name.startswith(blocked_module + "."):
            raise ImportError(f"Mocked: {name} not installed")
        return original_import(name, *args, **kwargs)

    return _blocker


# ── Unit: SemanticMatcher.match() ─────────────────────────────────────────────

class TestSemanticMatcherMatch:
    def _build_matcher_with_mock_embeddings(
        self,
        intent_vec: list[float],
        template_vecs: list[list[float]],
    ) -> SemanticMatcher:
        """Return a SemanticMatcher whose encode() returns controlled vectors (plain lists)."""
        all_embeddings = [intent_vec] + template_vecs

        matcher = SemanticMatcher()
        matcher._available = True

        mock_model = MagicMock()
        mock_model.encode.return_value = all_embeddings  # list of lists
        matcher._model = mock_model
        return matcher

    def test_match_returns_list_of_match_results(self) -> None:
        intent_vec = _make_embedding(4, 1.0)
        tpl_vecs = [_make_embedding(4, 1.0)] + [[0.0, 0.0, 0.0, 0.0]] * (len(ROLE_TEMPLATES) - 1)

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("monitor aws costs", ROLE_TEMPLATES)

        assert isinstance(results, list)
        assert len(results) >= 1
        assert all(isinstance(r, MatchResult) for r in results)

    def test_match_result_source_is_semantic(self) -> None:
        intent_vec = _make_embedding(4, 1.0)
        tpl_vecs = [_make_embedding(4, 1.0)] + [[0.0, 0.0, 0.0, 1.0]] * (len(ROLE_TEMPLATES) - 1)

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("monitor aws costs", ROLE_TEMPLATES)

        for r in results:
            assert r.source == "semantic"

    def test_match_confidence_is_cosine_similarity(self) -> None:
        # [1,1] vs [1,0]: cos = 1/sqrt(2) ≈ 0.707
        intent_vec = [1.0, 1.0, 0.0, 0.0]
        tpl_vecs = [[1.0, 0.0, 0.0, 0.0]] + [[0.0, 0.0, 0.0, 0.0]] * (len(ROLE_TEMPLATES) - 1)

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("test", ROLE_TEMPLATES)

        # First result should be ROLE_TEMPLATES[0] with cosine ≈ 0.707
        assert len(results) >= 1
        assert results[0].confidence == pytest.approx(
            _cosine_similarity([1.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]),
            abs=0.001,
        )

    def test_results_sorted_descending_by_confidence(self) -> None:
        n = len(ROLE_TEMPLATES)
        # Intent vec aligned with dim 0
        intent_vec = [1.0] + [0.0] * (n - 1)
        # Templates with varying similarity (different components on dim 0)
        tpl_vecs = []
        for i in range(n):
            vec = [0.0] * n
            # Decreasing component on dim 0 so similarity decreases
            vec[0] = max(0.5 - i * 0.02, 0.0)
            tpl_vecs.append(vec)

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("test", ROLE_TEMPLATES)

        confidences = [r.confidence for r in results]
        assert confidences == sorted(confidences, reverse=True)

    def test_max_5_matches_returned(self) -> None:
        # All templates highly similar to intent
        n = len(ROLE_TEMPLATES)
        intent_vec = [1.0, 0.0, 0.0, 0.0]
        # cos([1,0,0,0], [0.9,0,0,0]) = 1.0 (same direction)
        tpl_vecs = [[0.9, 0.0, 0.0, 0.0]] * n

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("test", ROLE_TEMPLATES)

        assert len(results) <= 5

    def test_low_similarity_templates_excluded(self) -> None:
        # Intent on dim 0, templates on dim 1 (orthogonal → similarity = 0.0, below 0.40)
        n = len(ROLE_TEMPLATES)
        intent_vec = [1.0, 0.0, 0.0, 0.0]
        tpl_vecs = [[0.0, 1.0, 0.0, 0.0]] * n

        matcher = self._build_matcher_with_mock_embeddings(intent_vec, tpl_vecs)
        results = matcher.match("test", ROLE_TEMPLATES)

        # All orthogonal → similarity = 0.0 → all filtered
        assert results == []

    def test_match_empty_templates_returns_empty(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = True
        results = matcher.match("monitor costs", [])
        assert results == []

    def test_match_unavailable_returns_empty(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = False
        results = matcher.match("monitor costs", ROLE_TEMPLATES)
        assert results == []


# ── Integration: full pipeline fallback chain ─────────────────────────────────

class TestPipelineFallback:
    """
    Tests the three-tier fallback chain: rule → semantic → LLM.
    All async; sentence-transformers and anthropic are mocked.
    """

    @pytest.mark.asyncio
    async def test_semantic_tier_activates_when_rule_low(self) -> None:
        """When rule confidence is low, semantic tier should activate."""
        parser = HIREParser(llm_fallback=False, use_semantic=True)

        # Inject a mock SemanticMatcher that returns a high-confidence result
        mock_result = MatchResult(
            template=TEMPLATE_BY_NAME["cost_monitor"],
            confidence=0.80,
            matched_phrases=[],
            source="semantic",
            order=0,
        )
        mock_semantic = MagicMock()
        mock_semantic.is_available.return_value = True
        mock_semantic.match.return_value = [mock_result]
        parser._semantic = mock_semantic

        result = await parser.parse_async("obscure intent that rule wont match")

        mock_semantic.is_available.assert_called()
        mock_semantic.match.assert_called_once()
        assert result.source == "semantic"
        assert result.matches[0].source == "semantic"

    @pytest.mark.asyncio
    async def test_llm_activates_when_semantic_low(self) -> None:
        """When semantic confidence is below threshold, LLM should be called."""
        parser = HIREParser(llm_fallback=True, use_semantic=True)

        # Semantic returns low confidence
        mock_semantic_match = MatchResult(
            template=TEMPLATE_BY_NAME["cost_monitor"],
            confidence=0.50,  # below _SEMANTIC_CONFIDENCE_THRESHOLD (0.65)
            matched_phrases=[],
            source="semantic",
            order=0,
        )
        mock_semantic = MagicMock()
        mock_semantic.is_available.return_value = True
        mock_semantic.match.return_value = [mock_semantic_match]
        parser._semantic = mock_semantic

        # LLM returns a ParseResult
        expected_llm_result = ParseResult(
            intent="test",
            matches=[MatchResult(
                template=TEMPLATE_BY_NAME["cost_monitor"],
                confidence=0.88,
                matched_phrases=["LLM matched"],
                source="llm",
                order=0,
            )],
            confidence=0.88,
            source="llm",
        )

        with patch.object(parser, "_llm_match", return_value=expected_llm_result):
            result = await parser.parse_async("obscure intent")

        assert result.source == "llm"
        assert result.confidence == 0.88

    @pytest.mark.asyncio
    async def test_llm_skipped_when_semantic_high(self) -> None:
        """When semantic confidence >= threshold, LLM should NOT be called."""
        parser = HIREParser(llm_fallback=True, use_semantic=True)

        mock_semantic_match = MatchResult(
            template=TEMPLATE_BY_NAME["cost_monitor"],
            confidence=0.75,  # above _SEMANTIC_CONFIDENCE_THRESHOLD
            matched_phrases=[],
            source="semantic",
            order=0,
        )
        mock_semantic = MagicMock()
        mock_semantic.is_available.return_value = True
        mock_semantic.match.return_value = [mock_semantic_match]
        parser._semantic = mock_semantic

        llm_called = False

        async def _llm_not_called(*args: Any, **kwargs: Any) -> ParseResult | None:
            nonlocal llm_called
            llm_called = True
            return None

        with patch.object(parser, "_llm_match", side_effect=_llm_not_called):
            result = await parser.parse_async("obscure intent")

        assert not llm_called
        assert result.source == "semantic"

    @pytest.mark.asyncio
    async def test_use_semantic_false_skips_semantic_tier(self) -> None:
        """use_semantic=False should bypass the semantic tier entirely."""
        parser = HIREParser(llm_fallback=False, use_semantic=False)

        # The _NullSemanticMatcher always returns is_available=False
        result = await parser.parse_async("obscure intent no rule match")

        # Source should be "rule" (no fallback upgraded it)
        assert result.source == "rule"

    @pytest.mark.asyncio
    async def test_semantic_skipped_when_unavailable(self) -> None:
        """If sentence-transformers not installed, semantic is skipped gracefully."""
        parser = HIREParser(llm_fallback=False, use_semantic=True)

        mock_semantic = MagicMock()
        mock_semantic.is_available.return_value = False
        parser._semantic = mock_semantic

        result = await parser.parse_async("obscure intent")

        # match() should never have been called
        mock_semantic.match.assert_not_called()
        assert result.source == "rule"

    @pytest.mark.asyncio
    async def test_high_rule_confidence_skips_all_fallbacks(self) -> None:
        """When rule confidence >= 0.70, neither semantic nor LLM should activate."""
        parser = HIREParser(llm_fallback=True, use_semantic=True)

        mock_semantic = MagicMock()
        mock_semantic.is_available.return_value = True
        parser._semantic = mock_semantic

        llm_called = False

        async def _noop(*args: Any, **kwargs: Any) -> None:
            nonlocal llm_called
            llm_called = True

        with patch.object(parser, "_llm_match", side_effect=_noop):
            # "monitor costs" triggers cost_monitor with high rule confidence
            result = await parser.parse_async("monitor AWS costs every hour")

        mock_semantic.match.assert_not_called()
        assert not llm_called
        assert result.source == "rule"

    @pytest.mark.asyncio
    async def test_hire_async_use_semantic_false(self) -> None:
        """hire_async(use_semantic=False) propagates to HIREParser."""
        # Should complete without error; semantic tier bypassed
        result = await hire_async(
            "monitor costs",
            use_semantic=False,
        )
        # Rule-based is sufficient for this intent
        assert "cost_monitor" in result.role_names()


# ── Integration: SemanticMatcher.encode() ────────────────────────────────────

class TestSemanticMatcherEncode:
    def test_encode_raises_when_unavailable(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = False

        with pytest.raises(RuntimeError, match="sentence-transformers is not installed"):
            matcher.encode(["hello world"])

    def test_encode_calls_model_encode(self) -> None:
        matcher = SemanticMatcher()
        matcher._available = True
        mock_model = MagicMock()
        # Return a plain list of lists (no numpy required)
        mock_model.encode.return_value = [[0.1, 0.2, 0.3]]
        matcher._model = mock_model

        result = matcher.encode(["hello world"])

        mock_model.encode.assert_called_once_with(
            ["hello world"], convert_to_numpy=True
        )
        assert result == [[0.1, 0.2, 0.3]]


# ── Real embedding tests (skipped when sentence-transformers not installed) ───

try:
    from sentence_transformers import SentenceTransformer as _ST  # type: ignore[import]
    _SBERT_AVAILABLE = True
except ImportError:
    _SBERT_AVAILABLE = False


@pytest.mark.skipif(not _SBERT_AVAILABLE, reason="sentence-transformers not installed")
class TestRealEmbeddings:
    def test_real_matcher_is_available(self) -> None:
        matcher = SemanticMatcher()
        assert matcher.is_available() is True

    def test_real_match_returns_results(self) -> None:
        matcher = SemanticMatcher()
        results = matcher.match("monitor AWS cloud spending and alert on cost anomalies", ROLE_TEMPLATES)
        assert len(results) > 0
        assert results[0].source == "semantic"
        assert 0.0 < results[0].confidence <= 1.0

    def test_real_match_finds_cost_monitor(self) -> None:
        matcher = SemanticMatcher()
        results = matcher.match("track cloud spending and alert when budget is exceeded", ROLE_TEMPLATES)
        names = [r.template.name for r in results]
        assert "cost_monitor" in names

    def test_real_match_confidence_ordering(self) -> None:
        matcher = SemanticMatcher()
        results = matcher.match("detect unusual patterns in metric streams", ROLE_TEMPLATES)
        confidences = [r.confidence for r in results]
        assert confidences == sorted(confidences, reverse=True)
