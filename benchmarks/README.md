# WIRE Benchmark Suite

Measures governance impact across four dimensions that no existing agent framework quantifies.

Each benchmark compares an unprotected agent (LangGraph / AutoGen / Agents SDK baseline) against the same agent running through WIRE governance guards.

---

## What is being measured — and why it matters

### 1. LoopGuard — loop containment (`bench_loop_guard.py`)

**The production failure:** An agent with a broken termination condition runs indefinitely. The exit predicate never fires. Without a hard limit, execution continues until a rate limit is hit or a human kills the process.

**What is measured:**
- Iterations executed before halt
- Cost incurred at halt (using $0.002/iteration, GPT-4o-mini equivalent)
- Time from start to halt
- Iterations prevented and cost saved versus a rate-limit cap at 1,000 iterations

**WIRE response:** `LoopGuard(max_iterations=50)` halts the agent at the 51st iteration, fires a `LOOP_BREACH` event, and raises `LoopBreachError`. The unprotected agent runs 20× longer and incurs 20× the cost before hitting the external cap.

---

### 2. IdempotencyGuard — deduplication (`bench_idempotency.py`)

**The production failure:** This is the CrewAI production bug. A side-effecting tool (Jira ticket creation, email send, payment initiation) is called inside a task that fails and retries. On each retry the orchestrator re-executes the full task — calling the tool again with identical arguments. The result: 3 Jira tickets created, 3 emails sent, 3 payments charged.

**What is measured:**
- Actual tool executions across 3 retry attempts (1 original + 2 retries)
- Tickets created without versus with deduplication
- Idempotency key hit rate (duplicates / total calls)
- Per-call overhead added by the guard (microseconds)
- Comparison across backends: Memory and SQLite

**WIRE response:** `IdempotencyGuard` content-addresses the call (`SHA-256(tool_name + args)`). The second and third calls return the cached result immediately — the tool function is never invoked again. Result: 1 ticket, 2 duplicates blocked, `(retries-1)/retries` hit rate.

---

### 3. AuditChain — integrity and throughput (`bench_audit_chain.py`)

**The compliance gap:** No leading agent framework produces a tamper-proof audit trail. Log files can be modified after the fact. Regulatory requirements (SOC 2, HIPAA, financial audit) demand evidence that logs cannot be silently altered.

**What is measured:**
- Write throughput: entries per second to a fresh chain
- Verification throughput: entries per second during a full chain scan
- Write overhead versus raw file write (no hashing)
- Tamper detection latency: milliseconds to detect a mutated entry mid-chain

**WIRE response:** `AuditChain` SHA-256-hashes each entry and chains it to the previous entry's hash (like a blockchain). `AuditChain.verify()` scans the full chain and raises `AuditChainError` at the first broken link. Injecting a tampered entry (changing a field value) is detected in the first verification pass with zero false negatives.

---

### 4. SLATracker — breach enforcement (`bench_sla_tracker.py`)

**The operational gap:** Agent response times vary. When an agent exceeds its SLA, existing frameworks are silent — no event, no escalation, no record. Engineers discover breaches via downstream monitoring, hours or days later.

**What is measured:**
- 100 agent invocations with normally distributed latency (μ=80ms, σ=30ms)
- SLA threshold: 100ms — approximately 20% of invocations breach by design
- Breach detection rate: percentage of actual breaches caught
- False positive rate: percentage of compliant invocations incorrectly flagged
- Pure instrumentation overhead: microseconds per call with zero-sleep workload

**WIRE response:** `SLATracker` wraps each invocation in an `asynccontextmanager`. On exit it measures elapsed time and raises `SLABreachError` if any threshold is exceeded. Detection rate is 100% by construction — every invocation above the threshold is caught. False positive rate is 0% because the guard measures real wall-clock time.

---

## How to run

```bash
pip install wire-ai
python benchmarks/run_all.py
```

Rich progress bars and result tables are rendered automatically if `rich` is installed (included in `wire-ai` base dependencies).

**Options:**

```bash
# Raw JSON output — no rich UI
python benchmarks/run_all.py --json-only

# Custom output path
python benchmarks/run_all.py --output my-results.json
```

**Run individual benchmarks:**

```bash
python benchmarks/bench_loop_guard.py
python benchmarks/bench_idempotency.py
python benchmarks/bench_audit_chain.py
python benchmarks/bench_sla_tracker.py
```

---

## Expected results

Numbers from a 2026 MacBook Pro M-series, Python 3.14. Wall-clock times scale with system load — the savings ratios are what matter.

| Guard | Without WIRE | With WIRE | Impact |
|---|---|---|---|
| LoopGuard | 1,000 iterations · $2.00 | 51 iterations · $0.102 | −94.9% cost · 949 iterations prevented |
| IdempotencyGuard | 3 tool calls · 3 Jira tickets | 1 tool call · 1 Jira ticket | 2 duplicates blocked · 66.7% hit rate · ~2.5 µs overhead |
| AuditChain | ~800K entries/sec (raw) | ~650 writes/sec · ~150K verifies/sec | Tamper detected in 5 ms · 0 false negatives |
| SLATracker | 0% detection · 29 silent failures | 100% detection · 0 silent failures | ~5 µs overhead/call · 0% false positive |

**AuditChain note:** Write throughput reflects SHA-256 hashing + chain-linking + fsync per entry. The raw baseline (800K entries/sec) writes unstructured JSON with no integrity. The correct comparison for compliance use cases is verification throughput (~150K entries/sec) and tamper detection latency (~5ms), which have no equivalent in unprotected frameworks.

**Benchmark axioms:**
- All LLM calls are mocked with `asyncio.sleep()` — no real API keys required.
- Latency distributions use a fixed seed (`RANDOM_SEED=42`) for reproducibility.
- Results are saved to `benchmarks/results.json` for diffing across runs.

---

## Files

| File | Purpose |
|---|---|
| `run_all.py` | Orchestrator — runs all 4, renders table, saves JSON |
| `bench_loop_guard.py` | LoopGuard iteration + cost containment |
| `bench_idempotency.py` | IdempotencyGuard deduplication across backends |
| `bench_audit_chain.py` | AuditChain write throughput + tamper detection |
| `bench_sla_tracker.py` | SLATracker breach detection + overhead measurement |
| `results.json` | Last run output (committed for baseline comparison) |

---

## Methodology notes

**No real LLM calls.** Every "agent step" is `asyncio.sleep(N)` with a fixed cost constant. This isolates the governance guard overhead from network variance.

**Unprotected baseline.** The "unprotected" scenario is how the same agent behaves in LangGraph, AutoGen, or the OpenAI Agents SDK today — none ship LoopGuard, IdempotencyGuard, AuditChain, or SLATracker as built-in primitives (verified in [WIRE research report](../design.md)).

**Overhead measurement.** For IdempotencyGuard and SLATracker, overhead is measured as the difference between `time_with_guard - simulated_latency`, giving pure instrumentation cost. Median is used (not mean) to exclude Python GIL outliers.

**Reproducibility.** All benchmarks write to `benchmarks/results.json`. Commit this file alongside any WIRE version bump to track performance regressions.
