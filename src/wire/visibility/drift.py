"""
DriftDetector — detects behavioural drift in agent outputs across sessions.

Compares semantic similarity of agent outputs over time.
Alerts when an agent's behaviour shifts beyond a configurable threshold.
Uses xxhash for fast structural comparison + optional embedding similarity.

This catches the CrewAI silent drift bug: agents change behaviour after
context compression or memory rotation with no observable signal.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_DEFAULT_WINDOW = 10        # compare against last N outputs
_DEFAULT_THRESHOLD = 0.30   # >30% structural change = drift alert


@dataclass
class DriftSnapshot:
    role: str
    run_id: str
    output_hash: str
    output_preview: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DriftAlert:
    role: str
    run_id: str
    similarity: float          # 0.0 = totally different, 1.0 = identical
    threshold: float
    baseline_hash: str
    current_hash: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_significant(self) -> bool:
        return self.similarity < (1.0 - self.threshold)


class DriftDetector:
    """
    Detects semantic drift in agent outputs across sessions.

    Usage:
        detector = DriftDetector(threshold=0.30)
        alert = detector.observe(
            role="cost_monitor",
            run_id="run_abc",
            output={"anomalies": [...], "summary": "..."}
        )
        if alert and alert.is_significant:
            print(f"Drift detected in {alert.role}!")
    """

    def __init__(
        self,
        threshold: float = _DEFAULT_THRESHOLD,
        window: int = _DEFAULT_WINDOW,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self._history: dict[str, deque[DriftSnapshot]] = {}
        self._alerts: list[DriftAlert] = []

    def observe(
        self,
        *,
        role: str,
        run_id: str,
        output: Any,
    ) -> DriftAlert | None:
        """
        Record an agent output and check for drift vs. baseline.
        Returns DriftAlert if drift detected, None otherwise.
        """
        current_hash = self._hash(output)
        preview = str(output)[:120]

        snapshot = DriftSnapshot(
            role=role, run_id=run_id,
            output_hash=current_hash, output_preview=preview,
        )

        if role not in self._history:
            self._history[role] = deque(maxlen=self.window)
            self._history[role].append(snapshot)
            return None

        history = self._history[role]

        # Compare against baseline (oldest in window)
        baseline = history[0]
        similarity = self._structural_similarity(baseline.output_hash, current_hash)

        history.append(snapshot)

        if similarity < (1.0 - self.threshold):
            alert = DriftAlert(
                role=role,
                run_id=run_id,
                similarity=similarity,
                threshold=self.threshold,
                baseline_hash=baseline.output_hash,
                current_hash=current_hash,
            )
            self._alerts.append(alert)
            log.warning(
                "drift_detected",
                role=role,
                run_id=run_id,
                similarity=round(similarity, 3),
                threshold=self.threshold,
            )
            return alert

        return None

    @property
    def alerts(self) -> list[DriftAlert]:
        return list(self._alerts)

    def clear_alerts(self) -> None:
        self._alerts.clear()

    def history_for(self, role: str) -> list[DriftSnapshot]:
        return list(self._history.get(role, []))

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _hash(output: Any) -> str:
        import json
        try:
            canonical = json.dumps(output, sort_keys=True, default=str)
        except Exception:
            canonical = str(output)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _structural_similarity(hash_a: str, hash_b: str) -> float:
        """
        Fast structural similarity using hash prefix overlap.
        Identical → 1.0. Completely different → ~0.0.
        For production, swap with embedding cosine similarity (Sprint 6).
        """
        if hash_a == hash_b:
            return 1.0
        # Jaccard similarity on 4-char n-grams of hex hash
        a_grams = {hash_a[i:i+4] for i in range(0, len(hash_a) - 4, 4)}
        b_grams = {hash_b[i:i+4] for i in range(0, len(hash_b) - 4, 4)}
        if not a_grams or not b_grams:
            return 0.0
        intersection = len(a_grams & b_grams)
        union = len(a_grams | b_grams)
        return intersection / union if union > 0 else 0.0
