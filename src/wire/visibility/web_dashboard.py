"""
WebDashboard — FastAPI + Server-Sent Events browser dashboard.

Provides a live, dark-themed executive dashboard at http://localhost:8080
that mirrors the terminal WorkforceDashboard in a web browser.

Usage::

    from wire.visibility.web_dashboard import WebDashboard
    from wire.visibility.dashboard import WorkforceDashboard

    terminal = WorkforceDashboard(workforce_name="aws-cost-monitor")
    web = WebDashboard(dashboard=terminal, port=8080)

    async def main() -> None:
        await web.start()
        # … do work, update terminal dashboard …
        await web.stop()
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import structlog

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    import uvicorn
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "WebDashboard requires the 'web' extra: pip install 'wire-ai[web]'"
    ) from exc

from wire.visibility.dashboard import WorkforceDashboard, AgentStatus

log = structlog.get_logger(__name__)

# ── Inline HTML page ──────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>WIRE Workforce Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:       #0d1117;
      --surface:  #161b22;
      --border:   #30363d;
      --text:     #e6edf3;
      --dim:      #8b949e;
      --cyan:     #39c5cf;
      --green:    #3fb950;
      --yellow:   #d29922;
      --red:      #f85149;
      --blue:     #58a6ff;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", ui-monospace,
                   "Courier New", monospace;
      font-size: 13px;
      line-height: 1.5;
      padding: 16px;
      min-height: 100vh;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      margin-bottom: 16px;
    }

    .logo { color: var(--cyan); font-size: 18px; font-weight: 700; letter-spacing: 2px; }
    .subtitle { color: var(--dim); font-size: 11px; margin-top: 2px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 4px 12px;
      font-size: 11px;
      color: var(--dim);
    }
    .dot-live { width: 8px; height: 8px; border-radius: 50%; background: var(--green);
                animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }

    /* ── Grid layout ── */
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      grid-template-rows: auto auto auto;
      gap: 16px;
    }
    .span-full { grid-column: 1 / -1; }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }
    .card-title {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 1px;
      text-transform: uppercase;
      color: var(--dim);
      padding: 10px 14px 8px;
      border-bottom: 1px solid var(--border);
    }
    .card-body { padding: 14px; }

    /* ── Cost summary ── */
    .cost-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    .cost-total { font-size: 28px; font-weight: 700; color: var(--green); }
    .cost-label { color: var(--dim); font-size: 11px; }
    .budget-wrap { flex: 1; min-width: 160px; }
    .budget-bar-track {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 4px;
      height: 10px;
      overflow: hidden;
      margin-top: 4px;
    }
    .budget-bar-fill { height: 100%; border-radius: 4px; transition: width .4s ease; }
    .budget-pct { font-size: 11px; color: var(--dim); margin-top: 4px; }

    /* ── Roles table ── */
    table { width: 100%; border-collapse: collapse; }
    th {
      text-align: left;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .8px;
      text-transform: uppercase;
      color: var(--dim);
      padding: 0 8px 8px;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 8px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
      white-space: nowrap;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(56,139,253,.05); }

    .status-dot {
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      margin-right: 6px;
      flex-shrink: 0;
    }
    .status-idle     { background: var(--dim); }
    .status-running  { background: var(--green); animation: pulse 1.5s infinite; }
    .status-waiting  { background: var(--yellow); }
    .status-complete { background: var(--cyan); }
    .status-error    { background: var(--red); }

    .badge {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-green { background: rgba(63,185,80,.15); color: var(--green); }
    .badge-yellow { background: rgba(210,153,34,.15); color: var(--yellow); }
    .badge-red { background: rgba(248,81,73,.15); color: var(--red); }
    .badge-dim { background: rgba(139,148,158,.1); color: var(--dim); }

    /* ── HITL queue ── */
    .hitl-item {
      border: 1px solid var(--yellow);
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 8px;
      background: rgba(210,153,34,.06);
    }
    .hitl-item:last-child { margin-bottom: 0; }
    .hitl-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
    .hitl-message { color: var(--text); font-size: 12px; flex: 1; }
    .hitl-meta { color: var(--dim); font-size: 11px; margin-top: 4px; }
    .hitl-empty { color: var(--dim); font-style: italic; text-align: center; padding: 16px 0; }

    /* ── Events feed ── */
    .event-feed { max-height: 260px; overflow-y: auto; }
    .event-item {
      display: flex;
      gap: 10px;
      padding: 5px 0;
      border-bottom: 1px solid rgba(48,54,61,.6);
      align-items: baseline;
    }
    .event-item:last-child { border-bottom: none; }
    .event-ts { color: var(--dim); font-size: 11px; flex-shrink: 0; width: 60px; }
    .event-role { color: var(--cyan); font-size: 11px; flex-shrink: 0; width: 120px;
                  overflow: hidden; text-overflow: ellipsis; }
    .event-msg { color: var(--text); font-size: 12px; flex: 1; word-break: break-word; }
    .event-msg.warning { color: var(--yellow); }
    .event-msg.error   { color: var(--red); }
    .event-msg.hitl    { color: var(--yellow); font-weight: 600; }

    /* ── Timestamp ── */
    #last-updated { color: var(--dim); font-size: 11px; text-align: right; margin-top: 12px; }

    /* ── Empty state ── */
    .empty { color: var(--dim); font-style: italic; padding: 16px 0; text-align: center; }

    /* ── Scrollbar styling ── */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    /* ── Responsive ── */
    @media (max-width: 640px) {
      .grid { grid-template-columns: 1fr; }
      .cost-total { font-size: 22px; }
      th, td { padding: 6px 4px; font-size: 11px; }
    }
  </style>
</head>
<body>

<header>
  <div>
    <div class="logo">&#x25A0; WIRE</div>
    <div class="subtitle" id="workforce-name">Workforce Dashboard</div>
  </div>
  <div class="status-pill">
    <span class="dot-live" id="conn-dot"></span>
    <span id="conn-label">Connecting…</span>
  </div>
</header>

<div class="grid">

  <!-- Cost summary -->
  <div class="card span-full">
    <div class="card-title">Cost &amp; Budget</div>
    <div class="card-body">
      <div class="cost-row">
        <div>
          <div class="cost-label">Total spent</div>
          <div class="cost-total" id="cost-total">$0.0000</div>
        </div>
        <div class="budget-wrap" id="budget-section" style="display:none">
          <div class="cost-label">Daily budget: <span id="budget-daily">—</span></div>
          <div class="budget-bar-track">
            <div class="budget-bar-fill" id="budget-bar" style="width:0%"></div>
          </div>
          <div class="budget-pct" id="budget-pct"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Roles table -->
  <div class="card span-full">
    <div class="card-title">Active Roles</div>
    <div class="card-body" style="padding:0">
      <table>
        <thead>
          <tr>
            <th>Role</th>
            <th>Status</th>
            <th>Confidence</th>
            <th>Cost</th>
            <th>SLA</th>
            <th>Last Event</th>
          </tr>
        </thead>
        <tbody id="roles-tbody">
          <tr><td colspan="6" class="empty">No active roles.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- HITL queue -->
  <div class="card">
    <div class="card-title">HITL Queue <span id="hitl-count" style="color:var(--yellow)"></span></div>
    <div class="card-body" id="hitl-body">
      <div class="hitl-empty">No pending approvals.</div>
    </div>
  </div>

  <!-- Events feed -->
  <div class="card">
    <div class="card-title">Recent Events</div>
    <div class="card-body" style="padding:0 14px">
      <div class="event-feed" id="events-feed">
        <div class="empty">No events yet.</div>
      </div>
    </div>
  </div>

</div>

<div id="last-updated">Never updated</div>

<script>
(function () {
  "use strict";

  const $ = id => document.getElementById(id);

  // ── SSE connection ──────────────────────────────────────────────────────────
  let evtSource = null;

  function connect() {
    evtSource = new EventSource("/stream");

    evtSource.addEventListener("state", e => {
      try { render(JSON.parse(e.data)); } catch (_) {}
    });

    evtSource.onopen = () => {
      $("conn-dot").style.background = "var(--green)";
      $("conn-label").textContent = "Live";
    };

    evtSource.onerror = () => {
      $("conn-dot").style.background = "var(--red)";
      $("conn-label").textContent = "Reconnecting…";
      evtSource.close();
      setTimeout(connect, 3000);
    };
  }

  connect();

  // ── Render ──────────────────────────────────────────────────────────────────
  function render(state) {
    $("workforce-name").textContent = state.workforce_name || "Workforce Dashboard";

    // Cost
    $("cost-total").textContent = "$" + (state.total_cost || 0).toFixed(4);

    // Budget bar
    if (state.budget_daily != null) {
      const pct = Math.min((state.total_cost / state.budget_daily) * 100, 100);
      const color = pct < 60 ? "var(--green)" : pct < 85 ? "var(--yellow)" : "var(--red)";
      $("budget-section").style.display = "";
      $("budget-daily").textContent = "$" + state.budget_daily.toFixed(2);
      $("budget-bar").style.width = pct.toFixed(1) + "%";
      $("budget-bar").style.background = color;
      $("budget-pct").textContent = pct.toFixed(1) + "% used";
    }

    // Roles table
    const tbody = $("roles-tbody");
    const roles = state.roles || [];
    if (roles.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No active roles.</td></tr>';
    } else {
      tbody.innerHTML = roles.map(r => {
        const statusDot = `<span class="status-dot status-${r.status}"></span>`;
        const statusLabel = r.status.charAt(0).toUpperCase() + r.status.slice(1);

        let confBadge;
        if (r.confidence == null) {
          confBadge = '<span class="badge badge-dim">—</span>';
        } else {
          const pct = (r.confidence * 100).toFixed(0) + "%";
          confBadge = r.confidence >= 0.80
            ? `<span class="badge badge-green">${pct}</span>`
            : `<span class="badge badge-yellow">${pct}</span>`;
        }

        const slaBadge = r.sla_ok
          ? '<span class="badge badge-green">OK</span>'
          : `<span class="badge badge-red">&#x26A0; ${r.sla_elapsed_s.toFixed(0)}s</span>`;

        const lastEv = r.last_event
          ? `<span style="color:var(--dim)">${escHtml(r.last_event_ts)}</span> ${escHtml(r.last_event.substring(0,40))}`
          : '<span style="color:var(--dim)">—</span>';

        return `<tr>
          <td style="color:var(--text);font-weight:600">${escHtml(r.name)}</td>
          <td>${statusDot}${escHtml(statusLabel)}</td>
          <td>${confBadge}</td>
          <td>$${r.cost_usd.toFixed(4)}</td>
          <td>${slaBadge}</td>
          <td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${lastEv}</td>
        </tr>`;
      }).join("");
    }

    // HITL queue
    const hitlItems = state.hitl_queue || [];
    $("hitl-count").textContent = hitlItems.length > 0 ? `(${hitlItems.length})` : "";
    const hitlBody = $("hitl-body");
    if (hitlItems.length === 0) {
      hitlBody.innerHTML = '<div class="hitl-empty">No pending approvals.</div>';
    } else {
      hitlBody.innerHTML = hitlItems.map(h => `
        <div class="hitl-item">
          <div class="hitl-header">
            <div class="hitl-message">${escHtml(h.message)}</div>
            <span class="badge badge-yellow">${escHtml(h.risk)}</span>
          </div>
          <div class="hitl-meta">
            Role: <strong>${escHtml(h.role)}</strong> &nbsp;·&nbsp;
            ID: <code>${escHtml(h.id.substring(0,8))}</code> &nbsp;·&nbsp;
            Options: ${h.options.map(o => `<code>${escHtml(o)}</code>`).join(" / ")}
            ${h.expires_at ? `&nbsp;·&nbsp; Expires: ${escHtml(h.expires_at.substring(11,19))} UTC` : ""}
          </div>
        </div>
      `).join("");
    }

    // Events feed
    const events = (state.events || []).slice().reverse();
    const feed = $("events-feed");
    if (events.length === 0) {
      feed.innerHTML = '<div class="empty">No events yet.</div>';
    } else {
      feed.innerHTML = events.map(ev => `
        <div class="event-item">
          <span class="event-ts">${escHtml(ev.ts)}</span>
          <span class="event-role">${escHtml(ev.role)}</span>
          <span class="event-msg ${ev.level !== "info" ? ev.level : ""}">${escHtml(ev.message)}</span>
        </div>
      `).join("");
    }

    $("last-updated").textContent = "Last updated: " + new Date().toLocaleTimeString();
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();
</script>
</body>
</html>
"""


# ── State serialisation ───────────────────────────────────────────────────────

def _dashboard_to_dict(dash: WorkforceDashboard) -> dict[str, Any]:
    """Serialise WorkforceDashboard state to a plain dict for JSON."""
    return {
        "workforce_name": dash.workforce_name,
        "backend": dash.backend,
        "total_cost": dash._total_cost,
        "budget_daily": dash.budget_daily,
        "started_at": dash._started_at,
        "roles": [
            {
                "name": r.name,
                "status": r.status.value if isinstance(r.status, AgentStatus) else r.status,
                "confidence": r.confidence,
                "cost_usd": r.cost_usd,
                "sla_ok": r.sla_ok,
                "sla_elapsed_s": r.sla_elapsed_s,
                "sla_limit_s": r.sla_limit_s,
                "last_event": r.last_event,
                "last_event_ts": r.last_event_ts,
                "iteration": r.iteration,
            }
            for r in dash._roles.values()
        ],
        "hitl_queue": [
            {
                "id": h.id,
                "role": h.role,
                "message": h.message,
                "risk": h.risk,
                "expires_at": h.expires_at,
                "options": h.options,
            }
            for h in dash._hitl_queue
        ],
        "events": [
            {
                "ts": e.ts,
                "role": e.role,
                "message": e.message,
                "level": e.level,
            }
            for e in dash._event_log[-10:]
        ],
    }


# ── WebDashboard ──────────────────────────────────────────────────────────────

class WebDashboard:
    """
    FastAPI web server that exposes a live browser dashboard backed by
    a WorkforceDashboard instance.

    Routes
    ------
    GET /          — HTML page (dark-themed, SSE-driven)
    GET /api/state — current dashboard state as JSON
    GET /stream    — Server-Sent Events, pushes state every 2 s
    GET /health    — {"status": "ok"}
    """

    def __init__(
        self,
        *,
        dashboard: WorkforceDashboard,
        port: int = 8080,
        host: str = "0.0.0.0",
    ) -> None:
        self._dashboard = dashboard
        self._port = port
        self._host = host
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._app = self._build_app()

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start uvicorn in a background asyncio task."""
        config = uvicorn.Config(
            app=self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        log.info("web_dashboard.started", url=self.url())

    async def stop(self) -> None:
        """Gracefully shut down the uvicorn server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        log.info("web_dashboard.stopped")

    def url(self) -> str:
        """Return the public URL of the dashboard (uses localhost regardless of bind host)."""
        return f"http://localhost:{self._port}"

    # ── FastAPI app ───────────────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="WIRE Workforce Dashboard", docs_url=None, redoc_url=None)

        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(content=_DASHBOARD_HTML)

        @app.get("/health")
        async def health() -> JSONResponse:
            return JSONResponse({"status": "ok"})

        @app.get("/api/state")
        async def state() -> JSONResponse:
            return JSONResponse(_dashboard_to_dict(self._dashboard))

        @app.get("/stream")
        async def stream() -> StreamingResponse:
            return StreamingResponse(
                self._sse_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        return app

    # ── SSE generator ─────────────────────────────────────────────────────────

    async def _sse_generator(self) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted state updates every 2 seconds."""
        try:
            while True:
                payload = json.dumps(_dashboard_to_dict(self._dashboard))
                yield f"event: state\ndata: {payload}\n\n"
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass
