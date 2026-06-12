#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# WIRE Regression Suite
# Runs before every git push. Blocks push if anything fails.
#
# Checks:
#   1. Full pytest suite (409 tests)
#   2. All public exports resolve
#   3. Core primitives instantiate
#   4. wire.hire() assembles workforce
#   5. AuditChain write + tamper-proof verify
#   6. IdempotencyGuard Memory + SQLite backends
#   7. RBAC allow/deny
#   8. All 5 framework adapters import
#   9. CLI commands (version, status, audit, replay)
#  10. pyproject.toml version == __version__
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
PYTHON="$VENV/bin/python"
WIRE_CMD="$VENV/bin/wire"

cd "$REPO"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0

_ok()   { echo -e "  ${GREEN}✓${NC}  $1"; ((PASS++)); }
_fail() { echo -e "  ${RED}✗${NC}  $1"; ((FAIL++)); }
_section() { echo -e "\n${CYAN}━━━  $1  ━━━${NC}"; }

echo ""
echo -e "${CYAN}  ██╗    ██╗██╗██████╗ ███████╗${NC}"
echo -e "${CYAN}  ██║    ██║██║██╔══██╗██╔════╝${NC}"
echo -e "${CYAN}  ██║ █╗ ██║██║██████╔╝█████╗  ${NC}"
echo -e "${CYAN}  ██║███╗██║██║██╔══██╗██╔══╝  ${NC}"
echo -e "${CYAN}  ╚███╔███╔╝██║██║  ██║███████╗${NC}"
echo -e "${CYAN}   ╚══╝╚══╝ ╚═╝╚═╝  ╚═╝╚══════╝${NC}"
echo -e "  ${YELLOW}Regression Suite — pre-push gate${NC}"
echo ""

# ── 1. pytest ─────────────────────────────────────────────────────────────────
_section "1. Full test suite"
PYTEST_OUT=$("$PYTHON" -m pytest tests/ -q --tb=short 2>&1 || true)
PYTEST_SUMMARY=$(echo "$PYTEST_OUT" | grep -E "passed|failed" | tail -1)
if echo "$PYTEST_SUMMARY" | grep -q "failed"; then
    _fail "pytest: $PYTEST_SUMMARY"
    echo "$PYTEST_OUT" | grep -E "FAILED|ERROR" | head -20
elif echo "$PYTEST_SUMMARY" | grep -q "passed"; then
    _ok "pytest: $PYTEST_SUMMARY"
else
    _fail "pytest: no results — $PYTEST_SUMMARY"
fi

# ── 2. Export completeness ────────────────────────────────────────────────────
_section "2. Public API exports"
EXPORT_RESULT=$("$PYTHON" -c "
import sys; sys.path.insert(0,'src')
import wire
missing = [x for x in wire.__all__ if getattr(wire, x, None) is None
           and x not in ('WebDashboard','AgentBlueprint','BlueprintRegistry')]
if missing:
    print('MISSING:' + ','.join(missing))
    sys.exit(1)
print(f'OK:{len(wire.__all__)} symbols')
" 2>/dev/null)
if echo "$EXPORT_RESULT" | grep -q "^OK:"; then
    _ok "All exports resolve ($(echo "$EXPORT_RESULT" | cut -d: -f2))"
else
    _fail "Missing exports: $EXPORT_RESULT"
fi

# ── 3. Core primitives ────────────────────────────────────────────────────────
_section "3. Core primitives"
"$PYTHON" -c "
import sys; sys.path.insert(0,'src')
import wire
wire.LoopGuard(run_id='r', max_iterations=10)
wire.Budget(max_usd=1.0)
wire.HITLGate(trigger=wire.Risk.HIGH)
wire.SLATracker(role='test', response_seconds=60)
wire.RBACPolicy.default()
wire.Tenant(id='t1', name='T')
wire.CompliancePreset.SOC2.config()
" 2>/dev/null && _ok "All core primitives instantiate" || _fail "Core primitive instantiation failed"

# ── 4. HIRE engine ────────────────────────────────────────────────────────────
_section "4. HIRE engine"
HIRE_RESULT=$("$PYTHON" -c "
import sys; sys.path.insert(0,'src')
import logging; logging.disable(logging.CRITICAL)
import structlog
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=open('/dev/null','w')))
import wire
wf = wire.hire('monitor AWS costs and open Jira P1 on breach')
print(f'roles:{len(wf.nodes)}:{wf.role_names()}')
" 2>/dev/null)
if echo "$HIRE_RESULT" | grep -q "^roles:"; then
    _ok "wire.hire() → $HIRE_RESULT"
else
    _fail "wire.hire() failed: $HIRE_RESULT"
fi

# ── 5. AuditChain ─────────────────────────────────────────────────────────────
_section "5. AuditChain write + verify"
AUDIT_RESULT=$("$PYTHON" -c "
import sys, asyncio, tempfile, pathlib; sys.path.insert(0,'src')
import wire
async def run():
    with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False) as f:
        p = f.name
    chain = wire.AuditChain(run_id='regression', path=p)
    for i in range(10): await chain.write(f'e{i}', data={'i':i})
    count = wire.AuditChain.verify(p)
    pathlib.Path(p).unlink()
    return count
n = asyncio.run(run())
print(f'entries:{n}')
" 2>/dev/null)
if echo "$AUDIT_RESULT" | grep -q "^entries:10"; then
    _ok "AuditChain: 10 entries written + chain verified"
else
    _fail "AuditChain failed: $AUDIT_RESULT"
fi

# ── 6. Idempotency backends ───────────────────────────────────────────────────
_section "6. IdempotencyGuard backends"
"$PYTHON" -c "
import sys, asyncio; sys.path.insert(0,'src')
import wire
from wire.core.idempotency_backends import MemoryBackend, SQLiteBackend

async def run():
    for BackendCls, kwargs in [(MemoryBackend, {}), (SQLiteBackend, {':memory:': True})]:
        if ':memory:' in kwargs:
            backend = SQLiteBackend(':memory:')
        else:
            backend = MemoryBackend()
        guard = wire.IdempotencyGuard(backend=backend)
        key = wire.IdempotencyGuard.make_key('tool', {'x': 1})
        calls = []
        async def fn(): calls.append(1); return 'ok'
        r1, d1 = await guard.call(key=key, fn=fn, run_id='r', tool='t')
        r2, d2 = await guard.call(key=key, fn=fn, run_id='r', tool='t')
        assert r1 == 'ok' and not d1
        assert r2 == 'ok' and d2
        assert len(calls) == 1

asyncio.run(run())
print('OK')
" 2>/dev/null && _ok "IdempotencyGuard: Memory + SQLite dedup correctly" || _fail "IdempotencyGuard backend test failed"

# ── 7. RBAC ───────────────────────────────────────────────────────────────────
_section "7. RBAC"
"$PYTHON" -c "
import sys; sys.path.insert(0,'src')
import logging; logging.disable(logging.CRITICAL)
import structlog
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=open('/dev/null','w')))
import wire
policy = wire.RBACPolicy.default()
eng = wire.Actor(id='e@co.com', groups=['wire-engineers'])
mgr = wire.Actor(id='m@co.com', groups=['wire-managers'])
adm = wire.Actor(id='a@co.com', groups=['wire-admins'])
assert policy.can(eng, wire.Permission.DEPLOY)
assert not policy.can(eng, wire.Permission.APPROVE_HITL)
assert policy.can(mgr, wire.Permission.APPROVE_HITL)
assert policy.can(adm, wire.Permission.ADMIN)
try:
    policy.require(eng, wire.Permission.APPROVE_HITL)
    assert False
except wire.PermissionDeniedError:
    pass
print('OK')
" 2>/dev/null && _ok "RBAC: allow/deny/error all correct" || _fail "RBAC test failed"

# ── 8. All adapters import ────────────────────────────────────────────────────
_section "8. Framework adapters"
"$PYTHON" -c "
import sys; sys.path.insert(0,'src')
from wire.adapters.langgraph import LangGraphAdapter
from wire.adapters.crewai import CrewAIAdapter
from wire.adapters.autogen import AutoGenAdapter
from wire.adapters.openai import OpenAIAdapter
from wire.adapters.foundry import FoundryAdapter
print('OK')
" 2>/dev/null && _ok "All 5 adapters (LangGraph/CrewAI/AutoGen/OpenAI/Foundry) import" || _fail "Adapter import failed"

# ── 9. CLI smoke tests ────────────────────────────────────────────────────────
_section "9. CLI"
VER=$("$WIRE_CMD" version 2>/dev/null)
if echo "$VER" | grep -q "wire-ai"; then
    _ok "wire version: $VER"
else
    _fail "wire version failed: $VER"
fi

STATUS=$("$WIRE_CMD" status 2>/dev/null)
if echo "$STATUS" | grep -q "Version"; then
    _ok "wire status: OK"
else
    _fail "wire status failed"
fi

# Create a temp audit + verify + replay
TMPAUDIT=$(mktemp /tmp/wire-regression-XXXXXX.jsonl)
"$PYTHON" -c "
import sys, asyncio; sys.path.insert(0,'src')
import wire
async def run():
    chain = wire.AuditChain(run_id='cli-test', path='$TMPAUDIT')
    await chain.write('start', data={'test': True})
    await chain.write('end',   data={'ok': True})
asyncio.run(run())
" 2>/dev/null

AUDIT_OUT=$("$WIRE_CMD" audit "$TMPAUDIT" 2>/dev/null)
if echo "$AUDIT_OUT" | grep -q "Chain intact"; then
    _ok "wire audit: chain intact"
else
    _fail "wire audit: $AUDIT_OUT"
fi

REPLAY_OUT=$("$WIRE_CMD" replay --run-id cli-test "$TMPAUDIT" 2>/dev/null)
if echo "$REPLAY_OUT" | grep -q "cli-test"; then
    _ok "wire replay: run reconstructed"
else
    _fail "wire replay: $REPLAY_OUT"
fi
rm -f "$TMPAUDIT"

# ── 10. Version consistency ───────────────────────────────────────────────────
_section "10. Version consistency"
PYPROJECT_VER=$(grep '^version = ' "$REPO/pyproject.toml" | sed 's/version = "\(.*\)"/\1/')
INIT_VER=$("$PYTHON" -c "import sys; sys.path.insert(0,'src'); import wire; print(wire.__version__)" 2>/dev/null)
if [ "$PYPROJECT_VER" = "$INIT_VER" ]; then
    _ok "Version consistent: pyproject.toml=$PYPROJECT_VER == __version__=$INIT_VER"
else
    _fail "Version mismatch: pyproject.toml=$PYPROJECT_VER != __version__=$INIT_VER"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$FAIL" -eq 0 ]; then
    echo -e "  ${GREEN}✓ ALL CHECKS PASSED${NC}  ($PASS passed, $FAIL failed)"
    echo "  Safe to push."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
else
    echo -e "  ${RED}✗ REGRESSION FAILURES${NC}  ($PASS passed, $FAIL failed)"
    echo "  Push blocked. Fix failures before pushing."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi
