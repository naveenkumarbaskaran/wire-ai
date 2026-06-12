"""Tests for Sprint 4 — CostLedger, DriftDetector, TimeTravel, Dashboard."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest

from wire.visibility.ledger import CostLedger
from wire.visibility.drift import DriftDetector, DriftAlert
from wire.visibility.replay import TimeTravel
from wire.visibility.dashboard import WorkforceDashboard, AgentStatus


# ── CostLedger ────────────────────────────────────────────────────────────────

class TestCostLedger:
    def test_zero_cost_initially(self) -> None:
        assert CostLedger().total_usd == 0.0

    def test_record_returns_cost(self) -> None:
        ledger = CostLedger()
        cost = ledger.record(run_id="r1", role="monitor", tokens_in=1000,
                             tokens_out=200, model="claude-haiku-4-5-20251001")
        assert cost > 0

    def test_total_accumulates(self) -> None:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="monitor", cost_usd=0.01)
        ledger.record(run_id="r1", role="monitor", cost_usd=0.02)
        assert ledger.total_usd == pytest.approx(0.03)

    def test_by_role_breakdown(self) -> None:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="monitor", cost_usd=0.01)
        ledger.record(run_id="r1", role="analyst", cost_usd=0.05)
        by_role = ledger.by_role()
        assert by_role["monitor"] == pytest.approx(0.01)
        assert by_role["analyst"] == pytest.approx(0.05)

    def test_by_run(self) -> None:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="monitor", cost_usd=0.01)
        ledger.record(run_id="r2", role="monitor", cost_usd=0.05)
        assert ledger.by_run("r1") == pytest.approx(0.01)
        assert ledger.by_run("r2") == pytest.approx(0.05)

    def test_explicit_cost_overrides_token_calc(self) -> None:
        ledger = CostLedger()
        cost = ledger.record(run_id="r1", role="monitor",
                             tokens_in=999999, tokens_out=999999,
                             cost_usd=0.001)
        assert cost == pytest.approx(0.001)

    def test_summary_structure(self) -> None:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="monitor", cost_usd=0.01)
        s = ledger.summary()
        assert "total_usd" in s
        assert "by_role" in s
        assert "entry_count" in s
        assert s["entry_count"] == 1

    def test_entries_filtered_by_run(self) -> None:
        ledger = CostLedger()
        ledger.record(run_id="r1", role="a", cost_usd=0.01)
        ledger.record(run_id="r2", role="b", cost_usd=0.02)
        assert len(ledger.entries("r1")) == 1
        assert len(ledger.entries("r2")) == 1
        assert len(ledger.entries()) == 2


# ── DriftDetector ─────────────────────────────────────────────────────────────

class TestDriftDetector:
    def test_first_observation_no_alert(self) -> None:
        d = DriftDetector()
        alert = d.observe(role="agent", run_id="r1", output={"result": "ok"})
        assert alert is None

    def test_identical_output_no_alert(self) -> None:
        d = DriftDetector(threshold=0.30)
        d.observe(role="agent", run_id="r1", output={"result": "ok"})
        alert = d.observe(role="agent", run_id="r2", output={"result": "ok"})
        assert alert is None

    def test_completely_different_output_alerts(self) -> None:
        d = DriftDetector(threshold=0.10)
        d.observe(role="agent", run_id="r1", output={"result": "aaa bbb ccc ddd eee"})
        alert = d.observe(role="agent", run_id="r2", output={"result": "zzz yyy xxx www vvv"})
        assert alert is not None
        assert isinstance(alert, DriftAlert)
        assert alert.role == "agent"

    def test_drift_alert_similarity_between_0_and_1(self) -> None:
        d = DriftDetector(threshold=0.10)
        d.observe(role="a", run_id="r1", output="first output value here")
        alert = d.observe(role="a", run_id="r2", output="completely different text")
        if alert:
            assert 0.0 <= alert.similarity <= 1.0

    def test_independent_roles_tracked_separately(self) -> None:
        d = DriftDetector(threshold=0.10)
        d.observe(role="role_a", run_id="r1", output={"x": 1})
        d.observe(role="role_b", run_id="r1", output={"x": 1})
        alert = d.observe(role="role_a", run_id="r2", output={"x": 1})
        assert alert is None  # identical, no drift for role_a

    def test_history_accumulates(self) -> None:
        d = DriftDetector()
        for i in range(5):
            d.observe(role="agent", run_id=f"r{i}", output={"i": i})
        assert len(d.history_for("agent")) == 5

    def test_window_caps_history(self) -> None:
        d = DriftDetector(window=3)
        for i in range(10):
            d.observe(role="agent", run_id=f"r{i}", output={"i": i})
        assert len(d.history_for("agent")) <= 3

    def test_clear_alerts(self) -> None:
        d = DriftDetector(threshold=0.05)
        d.observe(role="a", run_id="r1", output="aaaaaaaaaaaaaaaa")
        d.observe(role="a", run_id="r2", output="zzzzzzzzzzzzzzzz")
        d.clear_alerts()
        assert d.alerts == []


# ── TimeTravel ────────────────────────────────────────────────────────────────

class TestTimeTravel:
    @pytest.fixture
    def audit_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "audit.jsonl"
        entries = [
            {"run_id": "r1", "event": "workforce_start", "actor": "wire",
             "role": None, "data": {}, "ts": "2026-01-01T00:00:00+00:00",
             "prev_hash": "0"*64, "entry_hash": "a"*64},
            {"run_id": "r1", "event": "node_executed", "actor": "wire",
             "role": "cost_monitor", "data": {"node": "cost_monitor"}, "ts": "2026-01-01T00:00:01+00:00",
             "prev_hash": "a"*64, "entry_hash": "b"*64},
            {"run_id": "r2", "event": "workforce_start", "actor": "wire",
             "role": None, "data": {}, "ts": "2026-01-01T00:01:00+00:00",
             "prev_hash": "b"*64, "entry_hash": "c"*64},
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def test_load_run_filters_correctly(self, audit_file: Path) -> None:
        tt = TimeTravel(audit_file)
        steps = tt.load_run("r1")
        assert len(steps) == 2
        assert all(s.run_id == "r1" for s in steps)

    def test_load_run_returns_empty_for_unknown(self, audit_file: Path) -> None:
        tt = TimeTravel(audit_file)
        steps = tt.load_run("unknown")
        assert steps == []

    def test_list_runs_returns_all(self, audit_file: Path) -> None:
        tt = TimeTravel(audit_file)
        runs = tt.list_runs()
        assert "r1" in runs
        assert "r2" in runs

    def test_list_runs_no_duplicates(self, audit_file: Path) -> None:
        tt = TimeTravel(audit_file)
        runs = tt.list_runs()
        assert len(runs) == len(set(runs))

    def test_step_fields_populated(self, audit_file: Path) -> None:
        tt = TimeTravel(audit_file)
        steps = tt.load_run("r1")
        first = steps[0]
        assert first.event == "workforce_start"
        assert first.actor == "wire"
        assert first.index == 0

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        tt = TimeTravel(tmp_path / "missing.jsonl")
        with pytest.raises(FileNotFoundError):
            tt.load_run("r1")

    def test_render_does_not_raise(self, audit_file: Path) -> None:
        from rich.console import Console
        tt = TimeTravel(audit_file)
        steps = tt.load_run("r1")
        con = Console(file=open("/dev/null", "w"))
        tt.render(steps, console=con)  # just must not raise

    def test_render_empty_steps_graceful(self, audit_file: Path) -> None:
        from rich.console import Console
        tt = TimeTravel(audit_file)
        con = Console(file=open("/dev/null", "w"))
        tt.render([], console=con)


# ── WorkforceDashboard ────────────────────────────────────────────────────────

class TestWorkforceDashboard:
    def test_update_role_creates_entry(self) -> None:
        d = WorkforceDashboard(workforce_name="test")
        d.update_role("monitor", status="running", confidence=0.90)
        assert "monitor" in d._roles
        assert d._roles["monitor"].status == AgentStatus.RUNNING

    def test_cost_accumulates_in_role(self) -> None:
        d = WorkforceDashboard()
        d.update_role("monitor", cost_usd=0.01)
        d.update_role("monitor", cost_usd=0.02)
        assert d._roles["monitor"].cost_usd == pytest.approx(0.03)
        assert d._total_cost == pytest.approx(0.03)

    def test_add_event_appended(self) -> None:
        d = WorkforceDashboard()
        d.add_event("monitor", "cost spike detected", level="warning")
        assert len(d._event_log) == 1
        assert d._event_log[0].level == "warning"

    def test_event_log_capped_at_50(self) -> None:
        d = WorkforceDashboard()
        for i in range(60):
            d.add_event("monitor", f"event {i}")
        assert len(d._event_log) <= 50

    def test_add_hitl_sets_waiting_status(self) -> None:
        d = WorkforceDashboard()
        d.update_role("escalator")
        d.add_hitl(id="h1", role="escalator", message="Approve?", risk="high")
        assert d._roles["escalator"].status == AgentStatus.WAITING
        assert len(d._hitl_queue) == 1

    def test_resolve_hitl_removes_from_queue(self) -> None:
        d = WorkforceDashboard()
        d.add_hitl(id="h1", role="r", message="Approve?")
        d.resolve_hitl("h1")
        assert len(d._hitl_queue) == 0

    def test_print_snapshot_does_not_raise(self) -> None:
        from rich.console import Console
        import io
        d = WorkforceDashboard(workforce_name="test")
        d.update_role("monitor", status="running", confidence=0.94, cost_usd=0.04)
        d.add_event("monitor", "anomaly detected")
        d._console = Console(file=io.StringIO())
        d.print_snapshot()   # must not raise

    def test_build_layout_returns_panel(self) -> None:
        from rich.panel import Panel
        d = WorkforceDashboard()
        panel = d._build_layout()
        assert isinstance(panel, Panel)
