"""
wire.auto — import this module to auto-govern everything.

The ultimate transitive dependency hook:

    import wire.auto  # that's it — everything is now governed

Equivalent to:
    import wire
    wire.patch(verbose=True)

This pattern is used by:
  - sentry_sdk (import sentry_sdk; sentry_sdk.init())
  - ddtrace (import ddtrace; ddtrace.patch_all())
  - opentelemetry (auto-instrumentation via PYTHONPATH)

With wire.auto, WIRE becomes a transitive dependency for ANY project
that uses it: install wire-ai, add one import, done.

Environment variable control:
  WIRE_AUDIT_PATH=wire-audit.jsonl      # default audit path
  WIRE_MAX_COST_USD=1.0                 # hard cost ceiling
  WIRE_HOURLY_BUDGET_USD=0.50           # hourly budget
  WIRE_STALL_TIMEOUT_S=30               # stream stall timeout
  WIRE_VERBOSE=1                        # print patch summary
  WIRE_PATCH_LANGCHAIN=0                # disable langchain patch
  WIRE_PATCH_LLAMA_INDEX=0              # disable llama_index patch
  WIRE_PATCH_OPENAI=0                   # disable openai patch
  WIRE_PATCH_ANTHROPIC=0               # disable anthropic patch
"""

from __future__ import annotations

import os

from wire.middleware.autopatch import patch

_env = os.environ.get

patched = patch(
    audit_path=_env("WIRE_AUDIT_PATH", "wire-auto-audit.jsonl"),
    max_cost_usd=float(_env("WIRE_MAX_COST_USD", "0")) or None,
    hourly_budget_usd=float(_env("WIRE_HOURLY_BUDGET_USD", "0")) or None,
    stall_timeout_s=float(_env("WIRE_STALL_TIMEOUT_S", "30")),
    langchain=_env("WIRE_PATCH_LANGCHAIN", "1") != "0",
    llama_index=_env("WIRE_PATCH_LLAMA_INDEX", "1") != "0",
    openai=_env("WIRE_PATCH_OPENAI", "1") != "0",
    anthropic=_env("WIRE_PATCH_ANTHROPIC", "1") != "0",
    verbose=_env("WIRE_VERBOSE", "0") == "1",
)
