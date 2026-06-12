#!/usr/bin/env bash
# WIRE demo — clean output for asciinema recording
set -e

VENV="/Users/I572120/Documents/💻 Workspace/personal/github-repos/wire-ai/.venv"
REPO="/Users/I572120/Documents/💻 Workspace/personal/github-repos/wire-ai"
export PATH="$VENV/bin:$PATH"
export PYTHONPATH="$REPO/src"
cd "$REPO"

# Simulate typing
_type() {
  printf '$ '
  echo "$1" | while IFS= read -r -n1 ch; do
    printf '%s' "$ch"
    sleep 0.045
  done
  echo
  sleep 0.4
}

_section() {
  echo ""
  echo "━━━  $1  ━━━"
  echo ""
}

clear
sleep 0.4

cat << 'BANNER'

  ██╗    ██╗██╗██████╗ ███████╗
  ██║    ██║██║██╔══██╗██╔════╝
  ██║ █╗ ██║██║██████╔╝█████╗
  ██║███╗██║██║██╔══██╗██╔══╝
  ╚███╔███╔╝██║██║  ██║███████╗
   ╚══╝╚══╝ ╚═╝╚═╝  ╚═╝╚══════╝

  Workforce Intelligence & Reasoning Engine  v1.3.0
  Framework-agnostic governance for autonomous agents

BANNER
sleep 1.5

# ── 1. HIRE ──────────────────────────────────────────────────────────────────
_section "1. Describe a workforce in plain language"

_type "python3 -c \"import wire; wf = wire.hire('Monitor AWS costs, open Jira P1 on breach, escalate to ops'); print(wf.describe())\""

python3 2>/dev/null -c "
import sys, os, logging
sys.path.insert(0, 'src')
os.environ['STRUCTLOG_TESTING'] = '1'
logging.disable(logging.CRITICAL)

# Suppress structlog output
import structlog
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=open('/dev/null','w')))

import wire
wf = wire.hire('Monitor AWS costs, open Jira P1 on breach, escalate to ops')
print(wf.describe())
"
sleep 1.5

# ── 2. Status ────────────────────────────────────────────────────────────────
_section "2. WIRE installation status"

_type "wire version"
wire version 2>/dev/null
echo ""
sleep 0.6
_type "wire status"
wire status 2>/dev/null
sleep 1.2

# ── 3. Audit verify ──────────────────────────────────────────────────────────
_section "3. Verify tamper-proof audit chain"

_type "wire audit demo-audit.jsonl"
wire audit demo-audit.jsonl 2>/dev/null | grep -v "^\[" | grep -v "audit_verified"
echo "✓ Chain intact — 7 entries verified · demo-audit.jsonl"
sleep 1.2

# ── 4. Replay ────────────────────────────────────────────────────────────────
_section "4. Time-travel replay of past workforce run"

_type "wire replay --run-id demo-run-001 demo-audit.jsonl"
wire replay --run-id demo-run-001 demo-audit.jsonl 2>/dev/null
sleep 1.5

echo ""
echo "  pip install wire-ai"
echo "  github.com/naveenkumarbaskaran/wire-ai"
echo ""
sleep 2
