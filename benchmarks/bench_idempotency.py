"""
bench_idempotency.py — IdempotencyGuard deduplication benchmark.

Measures how many times a side-effecting tool is actually invoked when a
task fails and retries, comparing unprotected execution versus WIRE
IdempotencyGuard across all durable backends.

Scenario
--------
A task creates a Jira ticket, then reports a transient network failure.
The orchestrator retries the full task 3 times (total = 1 original + 2 retries).
Each retry calls the same "jira_create" tool with identical arguments.

Without protection: 3 tickets created.  (The CrewAI production bug.)
With WIRE:          1 ticket created, 2 duplicate calls deduplicated.

Backends tested
---------------
- MemoryBackend  : in-process, zero deps, lost on restart
- SQLiteBackend  : survives restarts, zero external deps

Metrics
-------
- actual_tool_executions : how many times the tool function really ran
- duplicate_calls_prevented : calls intercepted by idempotency guard
- idempotency_hit_rate : duplicates / total_calls
- overhead_per_call_us : microseconds added by the guard per call
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from wire.core.idempotency import IdempotencyGuard
from wire.core.idempotency_backends import MemoryBackend, SQLiteBackend

# ── Constants ─────────────────────────────────────────────────────────────────

TASK_RETRIES: int = 3          # 1 original + 2 retries (3 total attempts)
MOCK_TOOL_LATENCY_MS: float = 5.0   # simulate Jira API round-trip

# ── Mock side-effecting tool ──────────────────────────────────────────────────

@dataclass
class MockJiraState:
    """Tracks actual Jira API calls — the ground truth we're protecting."""
    tickets_created: list[str] = field(default_factory=list)
    api_calls: int = 0

    def reset(self) -> None:
        self.tickets_created.clear()
        self.api_calls = 0


_jira = MockJiraState()


async def jira_create_issue(title: str, project: str, priority: str) -> dict:
    """
    Mock Jira issue creation.  Simulates the real API: every call is a
    distinct network round-trip with a side effect (ticket created).
    """
    await asyncio.sleep(MOCK_TOOL_LATENCY_MS / 1000)
    _jira.api_calls += 1
    ticket_id = f"{project}-{len(_jira.tickets_created) + 1000}"
    _jira.tickets_created.append(ticket_id)
    return {"id": ticket_id, "title": title, "project": project}


# ── Unprotected run ───────────────────────────────────────────────────────────

async def run_unprotected() -> dict:
    """
    Simulate 3 retry attempts with no idempotency guard.
    All 3 attempts call jira_create_issue directly — 3 tickets are created.
    """
    _jira.reset()
    tool_args = {"title": "P1 — Service degradation", "project": "OPS", "priority": "highest"}

    start = time.perf_counter()
    results = []
    for attempt in range(TASK_RETRIES):
        result = await jira_create_issue(**tool_args)
        results.append(result)

    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "label": "Unprotected (no governance)",
        "actual_tool_executions": _jira.api_calls,
        "tickets_created": list(_jira.tickets_created),
        "duplicate_calls_prevented": 0,
        "idempotency_hit_rate": 0.0,
        "total_elapsed_ms": round(elapsed_ms, 2),
    }


# ── WIRE run ──────────────────────────────────────────────────────────────────

async def run_with_wire(backend_name: str, backend) -> dict:
    """
    Simulate 3 retry attempts with IdempotencyGuard.
    The guard deduplicates calls 2 and 3 — only 1 ticket is created.
    """
    _jira.reset()
    guard = IdempotencyGuard(backend=backend)
    run_id = str(uuid.uuid4())
    tool_args = {"title": "P1 — Service degradation", "project": "OPS", "priority": "highest"}
    idem_key = guard.make_key("jira_create", tool_args)

    duplicate_count = 0
    start = time.perf_counter()

    for attempt in range(TASK_RETRIES):
        result, was_duplicate = await guard.call(
            key=idem_key,
            fn=lambda: jira_create_issue(**tool_args),
            run_id=run_id,
            tool="jira_create",
        )
        if was_duplicate:
            duplicate_count += 1

    elapsed_ms = (time.perf_counter() - start) * 1000
    hit_rate = duplicate_count / TASK_RETRIES

    return {
        "label": f"WIRE IdempotencyGuard ({backend_name})",
        "actual_tool_executions": _jira.api_calls,
        "tickets_created": list(_jira.tickets_created),
        "duplicate_calls_prevented": duplicate_count,
        "idempotency_hit_rate": round(hit_rate, 3),
        "total_elapsed_ms": round(elapsed_ms, 2),
    }


# ── Overhead measurement ──────────────────────────────────────────────────────

async def measure_guard_overhead(iterations: int = 500) -> dict:
    """
    Measure microseconds added per call by the IdempotencyGuard itself.
    Uses MemoryBackend (fastest) for a lower-bound overhead measurement.

    Methodology: time N calls with guard vs N direct calls, compute mean delta.
    """
    tool_args = {"key": "val"}

    # Baseline: direct async call (no guard)
    async def noop_tool() -> dict:
        return {"ok": True}

    baseline_start = time.perf_counter()
    for _ in range(iterations):
        await noop_tool()
    baseline_elapsed = time.perf_counter() - baseline_start

    # With guard (fresh guard each time to avoid hit-path on dup check)
    guard_elapsed_total = 0.0
    for i in range(iterations):
        guard = IdempotencyGuard(backend=MemoryBackend())
        key = guard.make_key(f"tool_{i}", tool_args)
        guard_start = time.perf_counter()
        await guard.call(key=key, fn=noop_tool, run_id="bench", tool=f"tool_{i}")
        guard_elapsed_total += time.perf_counter() - guard_start

    overhead_per_call_us = (
        (guard_elapsed_total - baseline_elapsed) / iterations
    ) * 1_000_000

    return {
        "iterations": iterations,
        "baseline_per_call_us": round((baseline_elapsed / iterations) * 1_000_000, 2),
        "guard_per_call_us": round((guard_elapsed_total / iterations) * 1_000_000, 2),
        "overhead_per_call_us": round(max(overhead_per_call_us, 0), 2),
    }


# ── Main entry ────────────────────────────────────────────────────────────────

async def run_benchmark() -> dict:
    """Run all scenarios and return structured results for run_all.py."""
    unprotected = await run_unprotected()

    wire_memory = await run_with_wire("Memory", MemoryBackend())

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "wire-idem-bench.db")
        sqlite_backend = SQLiteBackend(path=db_path)
        wire_sqlite = await run_with_wire("SQLite", sqlite_backend)
        await sqlite_backend.close()

    overhead = await measure_guard_overhead(iterations=500)

    return {
        "benchmark": "idempotency_guard",
        "scenario": {
            "retries": TASK_RETRIES,
            "tool": "jira_create",
            "side_effect": "Jira ticket creation",
            "description": "Task retries 3x with identical args — how many tickets are created?",
        },
        "unprotected": unprotected,
        "wire_memory": wire_memory,
        "wire_sqlite": wire_sqlite,
        "overhead": overhead,
        "summary": {
            "tickets_without_wire": unprotected["actual_tool_executions"],
            "tickets_with_wire": wire_memory["actual_tool_executions"],
            "duplicates_prevented": wire_memory["duplicate_calls_prevented"],
            "hit_rate_pct": round(wire_memory["idempotency_hit_rate"] * 100, 1),
            "overhead_per_call_us": overhead["overhead_per_call_us"],
        },
    }


if __name__ == "__main__":
    import json
    result = asyncio.run(run_benchmark())
    print(json.dumps(result, indent=2))
