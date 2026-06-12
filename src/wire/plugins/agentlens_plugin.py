"""
AgentLens plugin for WIRE — runtime profiling and schema token reduction.

AgentLens (github.com/naveenkumarbaskaran/agentlens) is a zero-code-change
observability and optimization engine for AI agents using MCP. It offers
80–95% schema token reduction via smart tool routing.

This plugin integrates WIRE workforce events with AgentLens either via:
  1. Direct Python import  — when `agentlens` is installed in the same env.
  2. HTTP API              — when AgentLens runs as a standalone server.

Falls back to structured logging (structlog) when neither is available,
so WIRE governance always continues uninterrupted.

Install:
    pip install agentlens           # direct integration
    # or run the AgentLens server and point api_url at it
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from wire.plugins import WIREPlugin

log = structlog.get_logger(__name__)


class AgentLensPlugin(WIREPlugin):
    """
    AgentLens integration — runtime profiling and schema token reduction.

    Args:
        api_url:  Base URL of the AgentLens server (used when the Python
                  package is not installed).  Default: http://localhost:8080
        api_key:  Optional bearer token for the AgentLens API.

    Graceful degradation:
        - agentlens installed  → uses SDK directly (``agentlens.trace`` API).
        - agentlens server up  → sends HTTP spans via httpx.
        - neither available    → falls back to structlog; no exception raised.
    """

    name = "agentlens"
    version = "1.0.0"

    def __init__(
        self,
        *,
        api_url: str = "http://localhost:8080",
        api_key: str | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        # Per-run span bookkeeping: run_id -> {role: start_time_ns}
        self._spans: dict[str, dict[str, float]] = {}

    # ── Availability check ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """
        Return True if agentlens can be used.

        Tries direct Python import first, then a lightweight HTTP ping.
        Does NOT raise; returns False on any failure.
        """
        # 1. Try Python package
        try:
            import agentlens  # noqa: F401  # type: ignore[import-not-found]
            return True
        except ImportError:
            pass

        # 2. Try HTTP ping (synchronous because is_available() is sync)
        try:
            import httpx  # httpx is already a core WIRE dep
            resp = httpx.get(f"{self._api_url}/health", timeout=2.0)
            return resp.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    async def on_step_start(self, run_id: str, role: str, iteration: int) -> None:
        """Record the start of a span for this (run_id, role) pair."""
        if run_id not in self._spans:
            self._spans[run_id] = {}
        self._spans[run_id][role] = time.perf_counter()

        if self._try_sdk_step_start(run_id=run_id, role=role, iteration=iteration):
            return
        if await self._try_http_step_start(run_id=run_id, role=role, iteration=iteration):
            return
        log.debug(
            "agentlens.step_start",
            run_id=run_id,
            role=role,
            iteration=iteration,
        )

    async def on_step_end(
        self,
        run_id: str,
        role: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Close the span and record token/cost metrics."""
        start = self._spans.get(run_id, {}).pop(role, None)
        duration_ms = (time.perf_counter() - start) * 1000 if start is not None else None

        if self._try_sdk_step_end(
            run_id=run_id,
            role=role,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
        ):
            return
        if await self._try_http_step_end(
            run_id=run_id,
            role=role,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
        ):
            return
        log.info(
            "agentlens.step_end",
            run_id=run_id,
            role=role,
            cost_usd=round(cost_usd, 8),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
        )

    async def on_tool_call(
        self, run_id: str, tool: str, args: dict[str, Any]
    ) -> None:
        """Record a tool invocation for schema-token-reduction telemetry."""
        if self._try_sdk_tool_call(run_id=run_id, tool=tool, args=args):
            return
        if await self._try_http_tool_call(run_id=run_id, tool=tool, args=args):
            return
        log.debug("agentlens.tool_call", run_id=run_id, tool=tool)

    async def on_workforce_end(
        self, run_id: str, total_cost: float, iterations: int
    ) -> None:
        """Flush session summary to AgentLens."""
        self._spans.pop(run_id, None)  # clean up any orphaned spans

        if self._try_sdk_workforce_end(
            run_id=run_id, total_cost=total_cost, iterations=iterations
        ):
            return
        if await self._try_http_workforce_end(
            run_id=run_id, total_cost=total_cost, iterations=iterations
        ):
            return
        log.info(
            "agentlens.workforce_end",
            run_id=run_id,
            total_cost_usd=round(total_cost, 8),
            iterations=iterations,
        )

    # ── SDK integration (direct import) ──────────────────────────────────────

    def _try_sdk_step_start(self, **kwargs: Any) -> bool:
        try:
            import agentlens  # type: ignore[import-not-found]
            if hasattr(agentlens, "record_step_start"):
                agentlens.record_step_start(**kwargs)
                return True
        except (ImportError, Exception):  # noqa: BLE001
            pass
        return False

    def _try_sdk_step_end(self, **kwargs: Any) -> bool:
        try:
            import agentlens  # type: ignore[import-not-found]
            if hasattr(agentlens, "record_step_end"):
                agentlens.record_step_end(**kwargs)
                return True
        except (ImportError, Exception):  # noqa: BLE001
            pass
        return False

    def _try_sdk_tool_call(self, **kwargs: Any) -> bool:
        try:
            import agentlens  # type: ignore[import-not-found]
            if hasattr(agentlens, "record_tool_call"):
                agentlens.record_tool_call(**kwargs)
                return True
        except (ImportError, Exception):  # noqa: BLE001
            pass
        return False

    def _try_sdk_workforce_end(self, **kwargs: Any) -> bool:
        try:
            import agentlens  # type: ignore[import-not-found]
            if hasattr(agentlens, "flush_session"):
                agentlens.flush_session(**kwargs)
                return True
        except (ImportError, Exception):  # noqa: BLE001
            pass
        return False

    # ── HTTP integration (remote server) ─────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _try_http_step_start(self, **kwargs: Any) -> bool:
        return await self._post("/v1/spans/start", kwargs)

    async def _try_http_step_end(self, **kwargs: Any) -> bool:
        return await self._post("/v1/spans/end", kwargs)

    async def _try_http_tool_call(self, **kwargs: Any) -> bool:
        return await self._post("/v1/tool-calls", kwargs)

    async def _try_http_workforce_end(self, **kwargs: Any) -> bool:
        return await self._post("/v1/sessions/end", kwargs)

    async def _post(self, path: str, payload: dict[str, Any]) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self._api_url}{path}",
                    json=payload,
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
            return True
        except Exception:  # noqa: BLE001
            return False
