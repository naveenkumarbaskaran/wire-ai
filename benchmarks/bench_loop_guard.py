"""
bench_loop_guard.py — LoopGuard containment benchmark.

Measures how many iterations and how much cost a runaway agent accumulates
before being halted, comparing unprotected execution versus WIRE LoopGuard.

Scenario
--------
An agent has a broken termination condition — the exit predicate never fires.
Each iteration calls a mock LLM and costs $0.002 (GPT-4o-mini equivalent).
Without protection the agent runs until an external kill signal or rate-limit.
With LoopGuard the agent halts at max_iterations=50.

The unprotected run is capped at 1000 iterations to bound wall-clock time;
that cap represents a realistic rate-limit cutoff, not a governance guard.

Metrics
-------
- iterations_run         : iterations completed before halt
- cost_incurred_usd      : cumulative spend at halt
- time_to_halt_ms        : wall-clock time from start to halt
- iterations_prevented   : 1000 - iterations_run  (vs rate-limit cap)
- cost_saved_usd         : cost at cap - cost at guard halt
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from wire.core.errors import LoopBreachError
from wire.core.guard import LoopGuard

# ── Constants ─────────────────────────────────────────────────────────────────

COST_PER_ITERATION_USD: float = 0.002          # mock LLM cost per step
LOOP_GUARD_MAX_ITERATIONS: int = 50            # WIRE hard limit
UNPROTECTED_RATE_LIMIT_CAP: int = 1_000        # external kill at rate limit
MOCK_LLM_LATENCY_MS: float = 0.1              # tight loop — latency dominated by Python


# ── Mock agent step ───────────────────────────────────────────────────────────

async def mock_llm_step() -> str:
    """Simulate one LLM API call. Returns a plausible-but-never-terminal response."""
    await asyncio.sleep(MOCK_LLM_LATENCY_MS / 1000)
    return "I need to continue working on this task."


def broken_termination_condition(_response: str) -> bool:
    """
    Broken exit predicate — always returns False.
    This is the production bug: agent never decides it's done.
    """
    return False


# ── Benchmark runs ────────────────────────────────────────────────────────────

@dataclass
class LoopBenchResult:
    label: str
    iterations_run: int
    cost_incurred_usd: float
    time_to_halt_ms: float
    halt_reason: str


async def run_unprotected() -> LoopBenchResult:
    """
    Run the broken agent with no governance — halts only at rate-limit cap.
    This simulates what happens today with LangGraph / AutoGen / Agents SDK.
    """
    iterations = 0
    total_cost = 0.0
    start = time.perf_counter()

    while True:
        response = await mock_llm_step()
        iterations += 1
        total_cost += COST_PER_ITERATION_USD

        if broken_termination_condition(response):
            break

        if iterations >= UNPROTECTED_RATE_LIMIT_CAP:
            # External kill: rate-limit or human intervention
            break

    elapsed_ms = (time.perf_counter() - start) * 1000

    return LoopBenchResult(
        label="Unprotected (no governance)",
        iterations_run=iterations,
        cost_incurred_usd=total_cost,
        time_to_halt_ms=elapsed_ms,
        halt_reason=f"rate-limit cap at {UNPROTECTED_RATE_LIMIT_CAP} iterations",
    )


async def run_with_wire() -> LoopBenchResult:
    """
    Run the same broken agent through WIRE LoopGuard.
    LoopGuard halts immediately when max_iterations is exceeded.
    """
    guard = LoopGuard(
        run_id=str(uuid.uuid4()),
        max_iterations=LOOP_GUARD_MAX_ITERATIONS,
        max_cost_usd=None,   # iteration limit only for this benchmark
    )
    iterations = 0
    total_cost = 0.0
    start = time.perf_counter()

    try:
        while True:
            response = await mock_llm_step()
            iterations += 1
            total_cost += COST_PER_ITERATION_USD
            guard.tick(cost_usd=COST_PER_ITERATION_USD)

            if broken_termination_condition(response):
                break

    except LoopBreachError as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return LoopBenchResult(
            label="WIRE LoopGuard",
            iterations_run=e.iterations,
            cost_incurred_usd=guard.cost_usd,
            time_to_halt_ms=elapsed_ms,
            halt_reason=f"LoopGuard halted at iteration {e.iterations}/{e.limit}",
        )

    elapsed_ms = (time.perf_counter() - start) * 1000
    return LoopBenchResult(
        label="WIRE LoopGuard (clean exit)",
        iterations_run=iterations,
        cost_incurred_usd=total_cost,
        time_to_halt_ms=elapsed_ms,
        halt_reason="clean exit",
    )


# ── Main entry ────────────────────────────────────────────────────────────────

async def run_benchmark() -> dict:
    """Run both scenarios and return structured results for run_all.py."""
    unprotected = await run_unprotected()
    wire = await run_with_wire()

    iterations_prevented = unprotected.iterations_run - wire.iterations_run
    cost_saved_usd = unprotected.cost_incurred_usd - wire.cost_incurred_usd
    time_saved_ms = unprotected.time_to_halt_ms - wire.time_to_halt_ms

    return {
        "benchmark": "loop_guard",
        "unprotected": {
            "iterations_run": unprotected.iterations_run,
            "cost_incurred_usd": round(unprotected.cost_incurred_usd, 4),
            "time_to_halt_ms": round(unprotected.time_to_halt_ms, 2),
            "halt_reason": unprotected.halt_reason,
        },
        "wire": {
            "iterations_run": wire.iterations_run,
            "cost_incurred_usd": round(wire.cost_incurred_usd, 4),
            "time_to_halt_ms": round(wire.time_to_halt_ms, 2),
            "halt_reason": wire.halt_reason,
        },
        "savings": {
            "iterations_prevented": iterations_prevented,
            "cost_saved_usd": round(cost_saved_usd, 4),
            "time_saved_ms": round(time_saved_ms, 2),
            "cost_reduction_pct": round((cost_saved_usd / unprotected.cost_incurred_usd) * 100, 1),
        },
    }


if __name__ == "__main__":
    import json
    result = asyncio.run(run_benchmark())
    print(json.dumps(result, indent=2))
