#!/usr/bin/env bash
# WIRE demo script — recorded with asciinema
set -e

VENV="/Users/I572120/Documents/💻 Workspace/personal/github-repos/wire-ai/.venv"
REPO="/Users/I572120/Documents/💻 Workspace/personal/github-repos/wire-ai"
export PATH="$VENV/bin:$PATH"
export PYTHONPATH="$REPO/src"
cd "$REPO"

# Simulate typing with natural speed
_type() {
  local cmd="$1"
  printf '\e[1;32m$\e[0m '
  echo "$cmd" | while IFS= read -r -n1 ch; do
    printf '%s' "$ch"
    sleep 0.045
  done
  echo
  sleep 0.4
}

clear
sleep 0.5
cat << 'BANNER'

  ██╗    ██╗██╗██████╗ ███████╗
  ██║    ██║██║██╔══██╗██╔════╝
  ██║ █╗ ██║██║██████╔╝█████╗
  ██║███╗██║██║██╔══██╗██╔══╝
  ╚███╔███╔╝██║██║  ██║███████╗
   ╚══╝╚══╝ ╚═╝╚═╝  ╚═╝╚══════╝

  Workforce Intelligence & Reasoning Engine v1.3.0
  Framework-agnostic governance for autonomous agents

BANNER
sleep 1.5

# ── 1. HIRE ──────────────────────────────────────────────────────────────────
echo -e '\e[1;36m━━━  1. Describe a workforce in plain language  ━━━\e[0m'
echo
_type "python3 -c \"import wire; wf = wire.hire('Monitor AWS costs, open Jira P1 on breach, escalate to ops'); print(wf.describe())\""

python3 -c "
import sys; sys.path.insert(0, 'src')
import logging; logging.disable(logging.CRITICAL)
import wire
wf = wire.hire('Monitor AWS costs, open Jira P1 on breach, escalate to ops')
print(wf.describe())
" 2>/dev/null
sleep 1.5

# ── 2. Status ────────────────────────────────────────────────────────────────
echo
echo -e '\e[1;36m━━━  2. WIRE installation status  ━━━\e[0m'
echo
_type "wire version"
wire version 2>/dev/null
sleep 0.5
_type "wire status"
wire status 2>/dev/null
sleep 1.2

# ── 3. Audit verify ──────────────────────────────────────────────────────────
echo
echo -e '\e[1;36m━━━  3. Verify tamper-proof audit chain  ━━━\e[0m'
echo
_type "wire audit demo-audit.jsonl"
wire audit demo-audit.jsonl 2>/dev/null
sleep 1.2

# ── 4. Time-travel replay ────────────────────────────────────────────────────
echo
echo -e '\e[1;36m━━━  4. Time-travel replay of past workforce run  ━━━\e[0m'
echo
_type "wire replay --run-id demo-run-001 demo-audit.jsonl"
wire replay --run-id demo-run-001 demo-audit.jsonl 2>/dev/null
sleep 1.5

# ── Done ─────────────────────────────────────────────────────────────────────
echo
echo -e '\e[1;32m✓  WIRE — production-grade governance for AI agents\e[0m'
echo -e '\e[2m  pip install wire-ai\e[0m'
echo -e '\e[2m  github.com/naveenkumarbaskaran/wire-ai\e[0m'
echo
sleep 2
